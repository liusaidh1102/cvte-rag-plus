#
#  Copyright 2025 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
"""知识库文件分片直传（浏览器 -> MinIO multipart）。

四个接口：
    POST /init                创建 multipart 任务，落 file_upload 记录
    POST /get-presigned-part  为单个分片生成绑定 Content-MD5 的预签名 PUT URL
    GET  /list-missing-chunk  断点续传：以 MinIO ListParts 为准，返回缺失分片
    POST /merge               校验完整性 -> complete -> 写知识库文档

设计要点见 docs/multipart_upload.md。
"""

import base64
import binascii
import logging
import math
import mimetypes
import os
import re
from types import SimpleNamespace

from flask import request
from flask_login import login_required, current_user

from api import settings
from api.auth.token_session import extract_bearer_token, get_token_session
from api.db import FileType, UploadStatus
from api.db.services import duplicate_name
from api.db.services.document_service import DocumentService
from api.db.services.file_service import FileService
from api.db.services.file_upload_service import FileUploadService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.utils import current_timestamp, get_uuid
from api.utils.api_utils import get_json_result, server_error_response, validate_request
from api.utils.file_utils import filename_type
from rag.utils.minio_multipart import (
    DEFAULT_PRESIGN_EXPIRES,
    MAX_PARTS,
    MIN_PART_SIZE,
    MINIO_MULTIPART,
    normalize_etag,
)
from rag.utils.storage_factory import STORAGE_IMPL
DEFAULT_CHUNK_SIZE = 5 * 1024 * 1024
# 任务存活时间：与 MinIO stale uploads 的清理周期保持一致
DEFAULT_TASK_TTL_SECONDS = 24 * 60 * 60
TASK_TTL_SECONDS = int(os.environ.get("KB_UPLOAD_TASK_TTL", DEFAULT_TASK_TTL_SECONDS))
# MERGING 状态超过这个时间视为“上次合并的进程已挂”，允许接管
MERGE_STALE_MS = 10 * 60 * 1000
_FILE_MD5_RE = re.compile(r"^[0-9a-fA-F]{32}$")


# --------------------------------------------------------------------- helpers
def _error(message, code=settings.RetCode.ARGUMENT_ERROR):
    return get_json_result(data=False, message=message, code=code)


def _is_management_admin_upload():
    if getattr(current_user, "id", None) != "admin":
        return False
    payload = get_token_session(extract_bearer_token(request.headers.get("Authorization")))
    return bool(payload and payload.get("role") == "admin")


def _load_task(upload_id):
    """取任务 + 归属校验，返回 (task, error_response, owner_id)."""
    if not upload_id:
        return None, _error("`uploadId` is required"), None
    if _is_management_admin_upload():
        task = FileUploadService.get_by_upload_id(upload_id)
        owner_id = task.created_by if task else None
    else:
        task = FileUploadService.get_by_upload_id(upload_id, created_by=current_user.id)
        owner_id = current_user.id
    if not task:
        return None, _error("Upload task not found!", settings.RetCode.NOT_FOUND), None
    if not owner_id:
        return None, _error("Upload task has no owner", settings.RetCode.SERVER_ERROR), None
    e, _ = KnowledgebaseService.get_by_id(task.kb_id)
    if not e:
        return None, _error("Knowledgebase not found!", settings.RetCode.NOT_FOUND), None
    return task, None, owner_id


def _parse_part_number(value, total_parts):
    try:
        pn = int(value)
    except (TypeError, ValueError):
        return None
    if pn < 1 or pn > total_parts:
        return None
    return pn


def _valid_content_md5(value):
    """Content-MD5 必须是 base64 编码的 16 字节 MD5。"""
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None
    return value if len(raw) == 16 else None


def _valid_file_md5(value):
    """整文件 MD5 使用 32 位十六进制，统一转小写入库。"""
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if _FILE_MD5_RE.match(value) else None


def _uploaded_parts(task):
    """返回 (已上传分片集合, 来源)。

    分片是否真实存在以 MinIO ListParts 为准；如果 multipart 已经 complete，
    ListParts 会失效，此时用对象是否存在判断“所有分片都已合并”。
    """
    if task.status == UploadStatus.DONE.value:
        return set(range(1, task.total_parts + 1)), "object"
    try:
        parts = MINIO_MULTIPART.list_parts(task.bucket, task.location, task.minio_upload_id)
    except Exception:
        if MINIO_MULTIPART.stat_object_or_none(task.bucket, task.location) is not None:
            return set(range(1, task.total_parts + 1)), "object"
        raise
    return set(parts.keys()), "minio"


def _try_instant_upload(kb, name, size, filetype, file_md5, chunk_size, owner_id):
    """同知识库内命中相同 MD5 时，用服务端复制代替浏览器上传。

    不能直接让两个 document 共享同一个 object key：现有删除接口会按
    document.location 删除 MinIO 对象，共享会导致删一个影响另一个。
    """
    if not file_md5:
        return None
    hit = FileUploadService.find_completed_by_md5(kb.id, file_md5, size)
    if not hit or not hit.doc_id:
        return None
    found, source_doc = DocumentService.get_by_id(hit.doc_id)
    if not found or source_doc.kb_id != kb.id:
        return None
    if not STORAGE_IMPL.obj_exist(hit.bucket, hit.location):
        return None

    filename = duplicate_name(DocumentService.query, name=name, kb_id=kb.id)
    location = filename
    while STORAGE_IMPL.obj_exist(kb.id, location):
        location += "_"

    MINIO_MULTIPART.ensure_bucket(kb.id)
    copied = MINIO_MULTIPART.copy_object(hit.bucket, hit.location, kb.id, location)
    etag = normalize_etag(getattr(copied, "etag", None))

    upload_id = get_uuid()
    task = SimpleNamespace(
        id=upload_id,
        name=filename,
        location=location,
        type=filetype,
        size=size,
    )
    doc = _create_knowledgebase_document(task, kb, size, owner_id)
    now = current_timestamp()
    FileUploadService.insert(
        id=upload_id,
        minio_upload_id="",
        tenant_id=kb.tenant_id,
        created_by=owner_id,
        kb_id=kb.id,
        bucket=kb.id,
        name=filename,
        location=location,
        type=filetype,
        size=size,
        file_md5=file_md5,
        chunk_size=chunk_size,
        total_parts=0,
        status=UploadStatus.DONE.value,
        etag=etag,
        doc_id=doc["id"],
        expire_at=now + TASK_TTL_SECONDS * 1000,
    )
    return {
        "uploadId": upload_id,
        "kbId": kb.id,
        "bucket": kb.id,
        "name": filename,
        "location": location,
        "size": size,
        "chunkSize": chunk_size,
        "totalParts": 0,
        "partExpiresIn": DEFAULT_PRESIGN_EXPIRES,
        "status": UploadStatus.DONE.value,
        "docId": doc["id"],
        "etag": etag,
        "fileMd5": file_md5,
        "instant": True,
        "sourceUploadId": hit.id,
    }


# ------------------------------------------------------------------------ init
@manager.route("/init", methods=["POST"])  # noqa: F821
@login_required
@validate_request("kb_id", "name", "size")
def init_upload():
    req = request.json
    kb_id = req["kb_id"]
    name = str(req["name"]).strip()
    try:
        size = int(req["size"])
        chunk_size = int(req.get("chunk_size") or DEFAULT_CHUNK_SIZE)
    except (TypeError, ValueError):
        return _error("`size` / `chunk_size` must be integers")
    file_md5 = None
    if req.get("fileMd5") is not None:
        file_md5 = _valid_file_md5(req.get("fileMd5"))
        if not file_md5:
            return _error("`fileMd5` must be 32 hexadecimal characters")

    if not name or len(name) >= 128:
        return _error("File name must be 1~127 characters")
    if size <= 0:
        return _error("`size` must be positive")
    if chunk_size <= 0:
        return _error("`chunk_size` must be positive")
    if size > chunk_size and chunk_size < MIN_PART_SIZE:
        return _error(f"`chunk_size` must be >= {MIN_PART_SIZE} bytes when the file has more than one part")
    total_parts = max(1, math.ceil(size / chunk_size))
    if total_parts > MAX_PARTS:
        return _error(f"Too many parts ({total_parts} > {MAX_PARTS}), please increase `chunk_size`")

    e, kb = KnowledgebaseService.get_by_id(kb_id)
    if not e:
        return _error("Knowledgebase not found!", settings.RetCode.NOT_FOUND)

    if _is_management_admin_upload():
        owner_id = kb.created_by or kb.tenant_id
        if not owner_id:
            return _error("Knowledgebase has no owner", settings.RetCode.SERVER_ERROR)
    else:
        if not KnowledgebaseService.accessible(kb_id, current_user.id):
            return _error("You don't have permission to access this knowledgebase!", settings.RetCode.PERMISSION_ERROR)
        owner_id = current_user.id

    filetype = filename_type(name)
    if filetype == FileType.OTHER.value:
        return _error("This type of file has not been supported yet!")

    try:
        # MD5 秒传：命中同知识库已完成任务时，在服务端复制对象并直接建文档
        if file_md5:
            try:
                instant = _try_instant_upload(
                    kb, name, size, filetype, file_md5, chunk_size, owner_id
                )
                if instant:
                    logging.info(
                        "file_upload instant hit kb=%s md5=%s source=%s doc=%s",
                        kb.id, file_md5, instant.get("sourceUploadId"), instant.get("docId"),
                    )
                    return get_json_result(data=instant)
            except Exception:
                # 秒传只是优化路径，复制失败时继续走正常分片上传
                logging.exception("file_upload instant copy failed, fallback to multipart")

        # 对象 key 在 init 时定死，merge 时不再改名（否则签名/分片全部作废）
        filename = duplicate_name(DocumentService.query, name=name, kb_id=kb.id)
        location = filename
        while STORAGE_IMPL.obj_exist(kb.id, location):
            location += "_"

        MINIO_MULTIPART.ensure_bucket(kb.id)
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        minio_upload_id = MINIO_MULTIPART.create_multipart_upload(kb.id, location, content_type)

        upload_id = get_uuid()
        FileUploadService.insert(
            id=upload_id,
            minio_upload_id=minio_upload_id,
            tenant_id=kb.tenant_id,
            created_by=owner_id,
            kb_id=kb.id,
            bucket=kb.id,
            name=filename,
            location=location,
            type=filetype,
            size=size,
            file_md5=file_md5,
            chunk_size=chunk_size,
            total_parts=total_parts,
            status=UploadStatus.INIT.value,
            expire_at=current_timestamp() + TASK_TTL_SECONDS * 1000,
        )
        return get_json_result(
            data={
                "uploadId": upload_id,
                "kbId": kb.id,
                "bucket": kb.id,
                "name": filename,
                "location": location,
                "size": size,
                "chunkSize": chunk_size,
                "totalParts": total_parts,
                "partExpiresIn": DEFAULT_PRESIGN_EXPIRES,
                "status": UploadStatus.INIT.value,
                "fileMd5": file_md5,
            }
        )
    except Exception as e:
        return server_error_response(e)


# ----------------------------------------------------------- presigned part url
@manager.route("/get-presigned-part", methods=["POST"])  # noqa: F821
@login_required
@validate_request("uploadId", "partNumber", "chunkMd5")
def get_presigned_part():
    req = request.json
    task, err, _owner_id = _load_task(req.get("uploadId"))
    if err is not None:
        return err
    if task.status in (UploadStatus.DONE.value, UploadStatus.ABORTED.value):
        return _error(f"Upload task is {task.status}, no more parts can be uploaded", settings.RetCode.OPERATING_ERROR)

    part_number = _parse_part_number(req.get("partNumber"), task.total_parts)
    if part_number is None:
        return _error(f"`partNumber` must be in 1..{task.total_parts}")

    chunk_md5 = _valid_content_md5(req.get("chunkMd5"))
    if not chunk_md5:
        return _error("`chunkMd5` must be base64 encoded MD5 (16 bytes)")

    try:
        url = MINIO_MULTIPART.presign_put_part(
            bucket=task.bucket,
            object_name=task.location,
            minio_upload_id=task.minio_upload_id,
            part_number=part_number,
            content_md5_b64=chunk_md5,
            expires=DEFAULT_PRESIGN_EXPIRES,
        )
        return get_json_result(
            data={
                "uploadId": task.id,
                "partNumber": part_number,
                "uploaded": False,
                "url": url,
                "method": "PUT",
                # Content-MD5 已进签名，前端必须原样带上，否则 SignatureDoesNotMatch
                "headers": {"Content-MD5": chunk_md5},
                "expiresIn": DEFAULT_PRESIGN_EXPIRES,
            }
        )
    except Exception as e:
        return server_error_response(e)


# ----------------------------------------------------------- missing chunk list
@manager.route("/list-missing-chunk", methods=["GET"])  # noqa: F821
@login_required
def list_missing_chunk():
    task, err, _owner_id = _load_task(request.args.get("uploadId"))
    if err is not None:
        return err

    try:
        uploaded, source = _uploaded_parts(task)
        missing = [pn for pn in range(1, task.total_parts + 1) if pn not in uploaded]

        return get_json_result(
            data={
                "uploadId": task.id,
                "status": task.status,
                "name": task.name,
                "size": task.size,
                "chunkSize": task.chunk_size,
                "totalParts": task.total_parts,
                "uploadedParts": task.total_parts - len(missing),
                "missingParts": missing,
                "source": source,
            }
        )
    except Exception as e:
        # multipart 已被合并/清理：让前端清掉本地 uploadId 重新 init
        if getattr(e, "code", "") == "NoSuchUpload":
            return _error("Upload task expired or was aborted", settings.RetCode.NOT_FOUND)
        return server_error_response(e)


# ----------------------------------------------------------------------- merge
@manager.route("/merge", methods=["POST"])  # noqa: F821
@login_required
@validate_request("uploadId")
def merge():
    req = request.json
    task, err, owner_id = _load_task(req.get("uploadId"))
    if err is not None:
        return err

    if task.status == UploadStatus.DONE.value:
        # 幂等：重复 merge 直接回已有文档
        return get_json_result(
            data={
                "uploadId": task.id,
                "docId": task.doc_id,
                "name": task.name,
                "location": task.location,
                "size": task.size,
                "etag": task.etag,
                "status": task.status,
                "merged": True,
            }
        )

    # 抢状态：只有从 INIT/UPLOADING/FAILED 抢到 MERGING 的请求才真正合并
    if not FileUploadService.cas_status(
        task.id,
        [UploadStatus.INIT.value, UploadStatus.UPLOADING.value, UploadStatus.FAILED.value],
        UploadStatus.MERGING.value,
        error_msg=None,
    ):
        latest = FileUploadService.get_by_upload_id(task.id, created_by=owner_id)
        if latest and latest.status == UploadStatus.DONE.value:
            return get_json_result(
                data={
                    "uploadId": latest.id,
                    "docId": latest.doc_id,
                    "name": latest.name,
                    "location": latest.location,
                    "size": latest.size,
                    "etag": latest.etag,
                    "status": latest.status,
                    "merged": True,
                }
            )
        # 上一次 merge 的进程挂了（状态卡在 MERGING）时允许接管，否则任务永远卡死
        if not (latest and FileUploadService.touch_stale_merging(task.id, MERGE_STALE_MS)):
            return _error("Upload task is being merged, please retry later", settings.RetCode.OPERATING_ERROR)

    try:
        e, kb = KnowledgebaseService.get_by_id(task.kb_id)
        if not e:
            FileUploadService.mark_failed(task.id, "Knowledgebase not found")
            return _error("Knowledgebase not found!", settings.RetCode.NOT_FOUND)

        # 1) 分片清单只认 MinIO ListParts：它同时用于校验和 complete 入参
        try:
            minio_parts = MINIO_MULTIPART.list_parts(task.bucket, task.location, task.minio_upload_id)
        except Exception:
            # 上次已经 complete 成功、但写知识库文档失败（例如 DB 抖动）：
            # 此时 uploadId 已失效，对象却真实存在，直接补写文档即可。
            stat = MINIO_MULTIPART.stat_object_or_none(task.bucket, task.location)
            if stat is None:
                raise
            doc = _create_knowledgebase_document(task, kb, int(stat.size), owner_id)
            FileUploadService.update_by_id(
                task.id,
                {
                    "status": UploadStatus.DONE.value,
                    "doc_id": doc["id"],
                    "etag": normalize_etag(getattr(stat, "etag", None)),
                    "size": int(stat.size),
                },
            )
            return get_json_result(
                data={
                    "uploadId": task.id,
                    "docId": doc["id"],
                    "name": doc["name"],
                    "location": doc["location"],
                    "size": int(stat.size),
                    "etag": normalize_etag(getattr(stat, "etag", None)),
                    "status": UploadStatus.DONE.value,
                    "merged": True,
                }
            )

        # 2) 完整性校验：分片号必须恰好覆盖 1..totalParts，不能只看数量
        missing = [pn for pn in range(1, task.total_parts + 1) if pn not in minio_parts]
        over_limit = [pn for pn in minio_parts if pn > task.total_parts]
        if missing:
            FileUploadService.cas_status(task.id, [UploadStatus.MERGING.value], UploadStatus.UPLOADING.value)
            return get_json_result(
                code=settings.RetCode.DATA_ERROR,
                message=f"{len(missing)} part(s) missing, upload them before merge",
                data={"uploadId": task.id, "missingParts": missing, "merged": False},
            )

        if over_limit:
            FileUploadService.cas_status(task.id, [UploadStatus.MERGING.value], UploadStatus.UPLOADING.value)
            return _error(
                f"MinIO has unexpected part number(s) {sorted(over_limit)[:10]}, declared totalParts={task.total_parts}"
            )

        uploaded_size = sum(int(v["size"] or 0) for v in minio_parts.values())
        if uploaded_size != task.size and not req.get("force"):
            FileUploadService.cas_status(task.id, [UploadStatus.MERGING.value], UploadStatus.UPLOADING.value)
            return _error(
                f"Uploaded size {uploaded_size} != declared size {task.size}. "
                "Pass `force: true` to merge anyway if the declared size was wrong."
            )

        # 中间分片必须 >= 5MiB，否则 complete 会直接报 EntityTooSmall，提前给出可读错误
        too_small = [
            pn for pn, info in minio_parts.items()
            if pn < task.total_parts and int(info["size"] or 0) < MIN_PART_SIZE
        ]
        if too_small:
            FileUploadService.cas_status(task.id, [UploadStatus.MERGING.value], UploadStatus.UPLOADING.value)
            return _error(f"Part(s) {sorted(too_small)[:10]} are smaller than {MIN_PART_SIZE} bytes")

        # 3) 合并
        result = MINIO_MULTIPART.complete_multipart_upload(
            task.bucket, task.location, task.minio_upload_id, minio_parts
        )
        stat = MINIO_MULTIPART.stat_object(task.bucket, task.location)
        actual_size = int(getattr(stat, "size", uploaded_size))

        # 4) 写知识库文档（Document + file 树 + file2document）
        doc = _create_knowledgebase_document(task, kb, actual_size, owner_id)

        FileUploadService.update_by_id(
            task.id,
            {
                "status": UploadStatus.DONE.value,
                "doc_id": doc["id"],
                "etag": normalize_etag(getattr(result, "etag", None) or getattr(stat, "etag", None)),
                "size": actual_size,
            },
        )
        return get_json_result(
            data={
                "uploadId": task.id,
                "docId": doc["id"],
                "name": doc["name"],
                "location": doc["location"],
                "size": actual_size,
                "etag": normalize_etag(getattr(stat, "etag", None)),
                "status": UploadStatus.DONE.value,
                "merged": True,
            }
        )
    except Exception as e:
        logging.exception(f"merge {task.id} failed")
        FileUploadService.mark_failed(task.id, e)
        return server_error_response(e)


def _create_knowledgebase_document(task, kb, actual_size=None, owner_id=None):
    """merge 成功后把文件挂到知识库：Document + 文件树 + 关联表。

    幂等：如果同名文档已经写进去了（上次 merge 在写库阶段失败），直接复用，
    避免重试时产生重复文档。
    """
    owner_id = owner_id or current_user.id
    size = int(actual_size if actual_size is not None else task.size)
    existing = DocumentService.query(kb_id=kb.id, name=task.name)
    if existing:
        doc = existing[0].to_dict()
        if int(doc.get("size") or 0) != size:
            DocumentService.update_by_id(doc["id"], {"size": size})
            doc["size"] = size
    else:
        doc = {
            "id": get_uuid(),
            "kb_id": kb.id,
            "parser_id": FileService.get_parser(task.type, task.name, kb.parser_id),
            "parser_config": kb.parser_config,
            "created_by": owner_id,
            "type": task.type,
            "name": task.name,
            "location": task.location,
            "size": size,
            "thumbnail": None,
        }
        DocumentService.insert(doc)

    root_folder = FileService.get_root_folder(owner_id)
    FileService.init_knowledgebase_docs(root_folder["id"], owner_id)
    kb_root_folder = FileService.get_kb_folder(owner_id)
    kb_folder = FileService.new_a_file_from_kb(kb.tenant_id, kb.name, kb_root_folder["id"])
    # add_file_from_kb 内部会检查 file2document 是否已存在，天然幂等
    FileService.add_file_from_kb(doc, kb_folder["id"], kb.tenant_id)
    return doc
