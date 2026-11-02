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
"""MinIO 分片直传（multipart upload）能力封装。

这里只做四件事：
1. 创建 / 列举 / 合并 / 放弃 multipart upload（走 minio-py）；
2. 为单个分片生成**绑定 Content-MD5** 的预签名 PUT URL（自研 SigV4，见下方说明）；
3. 桶级生命周期与 CORS 的一次性配置；
4. ETag 归一化。

为什么预签名要自己实现？
    minio-py 的 ``presign_v4`` 把 ``SignedHeaders`` 写死成 ``host``（见
    minio/signer.py:_get_presign_canonical_request_hash），SDK 没有入口把
    ``Content-MD5`` 放进签名头。因此这里按 AWS SigV4 query-string 方式自己签，
    把 ``SignedHeaders`` 设为 ``content-md5;host``。效果：
      * 客户端必须原样带 ``Content-MD5`` 头，否则 MinIO 返回 SignatureDoesNotMatch；
      * MinIO 在 UploadPart 时会校验 body 的 MD5 与 ``Content-MD5`` 一致，
        不一致返回 BadDigest。
    两者叠加，URL 就只能用来上传“那一片、且内容字节完全一致”的数据。
"""

import base64
import hashlib
import hmac
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

from minio import Minio
from minio.commonconfig import CopySource, ENABLED, Filter
from minio.datatypes import Part
from minio.error import S3Error
from minio.lifecycleconfig import AbortIncompleteMultipartUpload, Expiration, LifecycleConfig, Rule

from rag import settings

# S3 规定：单次 multipart upload 最多 10000 个分片
MAX_PARTS = 10000
# S3 规定：除最后一片外，每片至少 5MiB
MIN_PART_SIZE = 5 * 1024 * 1024
# 预签名 URL 默认有效期（秒）
DEFAULT_PRESIGN_EXPIRES = int(os.environ.get("MINIO_PRESIGN_EXPIRES", 900))

_UNRESERVED = "-_.~"


def normalize_etag(etag: Optional[str]) -> str:
    """MinIO 返回的 ETag 带双引号（弱校验时还有 W/ 前缀），统一存裸值。"""
    if not etag:
        return ""
    etag = etag.strip()
    if etag.startswith("W/"):
        etag = etag[2:]
    return etag.strip('"')


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret_key: str, date_stamp: str, region: str) -> bytes:
    k_date = _hmac_sha256(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, "s3")
    return _hmac_sha256(k_service, "aws4_request")


def _uri_encode(value: str, safe: str = _UNRESERVED) -> str:
    return quote(value, safe=safe)


def _canonical_uri(bucket: str, object_name: str) -> str:
    """S3 的 canonical URI：path-style，每段只编码一次，保留 '/' 分隔。"""
    segments = [_uri_encode(seg) for seg in object_name.split("/")]
    return "/" + _uri_encode(bucket, safe="") + "/" + "/".join(segments)


def _canonical_query(params: dict) -> str:
    # 按编码后的 key、再按 value 排序
    items = sorted((_uri_encode(str(k), safe=""), _uri_encode(str(v), safe="")) for k, v in params.items())
    return "&".join(f"{k}={v}" for k, v in items)


def build_presigned_url(
    *,
    endpoint: str,
    access_key: str,
    secret_key: str,
    region: str,
    bucket: str,
    object_name: str,
    method: str = "PUT",
    query_params: Optional[dict] = None,
    signed_headers: Optional[dict] = None,
    expires: int = DEFAULT_PRESIGN_EXPIRES,
    now: Optional[datetime] = None,
) -> str:
    """生成 SigV4 query-string 预签名 URL。

    ``signed_headers`` 里的每个 header 都会被写进 ``X-Amz-SignedHeaders``，
    客户端请求时必须原样携带，否则签名校验失败。
    """
    now = now or datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    scope = f"{date_stamp}/{region}/s3/aws4_request"

    host = endpoint.split("://", 1)[-1].rstrip("/")
    scheme = "https" if endpoint.startswith("https://") else "http"

    # 参与签名的 header（host 必签）
    headers = {"host": host}
    for k, v in (signed_headers or {}).items():
        headers[k.lower()] = v
    canonical_headers = "\n".join(f"{k}:{v}" for k, v in sorted(headers.items()))
    signed_header_names = ";".join(sorted(headers.keys()))

    params = dict(query_params or {})
    params.update(
        {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": f"{access_key}/{scope}",
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": str(int(expires)),
            "X-Amz-SignedHeaders": signed_header_names,
        }
    )
    canonical_query = _canonical_query(params)

    # 预签名请求没有 body 摘要，payload hash 固定为 UNSIGNED-PAYLOAD
    canonical_request = "\n".join(
        [
            method,
            _canonical_uri(bucket, object_name),
            canonical_query,
            canonical_headers,
            "",
            signed_header_names,
            "UNSIGNED-PAYLOAD",
        ]
    )
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    signature = hmac.new(
        _signing_key(secret_key, date_stamp, region),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    query_with_sig = f"{canonical_query}&X-Amz-Signature={signature}"
    return f"{scheme}://{host}{_canonical_uri(bucket, object_name)}?{query_with_sig}"


class MinioMultipart:
    """multipart upload 专用门面（进程内单例，懒加载 + 失败重连）。"""

    def __init__(self):
        self._client: Optional[Minio] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ client
    @property
    def client(self) -> Minio:
        if self._client is None:
            with self._lock:
                if self._client is None:
                    self._client = Minio(
                        settings.MINIO["host"],
                        access_key=settings.MINIO["user"],
                        secret_key=settings.MINIO["password"],
                        secure=str(settings.MINIO.get("secure", "false")).lower() == "true",
                    )
        return self._client

    def _reset(self):
        with self._lock:
            self._client = None

    @property
    def access_key(self) -> str:
        return settings.MINIO["user"]

    @property
    def secret_key(self) -> str:
        return settings.MINIO["password"]

    @property
    def region(self) -> str:
        # MinIO 默认 region 就是 us-east-1；不需要每次 GetBucketLocation
        return os.environ.get("MINIO_REGION") or settings.MINIO.get("region") or "us-east-1"

    @property
    def public_endpoint(self) -> str:
        """浏览器可达的 endpoint（容器内 host 通常是 localhost/minio，外网不可达）。"""
        endpoint = os.environ.get("MINIO_PUBLIC_ENDPOINT")
        if endpoint:
            return endpoint if endpoint.startswith("http") else f"http://{endpoint}"
        visit_host = os.environ.get("MINIO_VISIT_HOST")
        if visit_host:
            port = os.environ.get("MINIO_PORT", "9000")
            return visit_host if visit_host.startswith("http") else f"http://{visit_host}:{port}"
        host = settings.MINIO["host"]
        return host if host.startswith("http") else f"http://{host}"

    # --------------------------------------------------------------- multipart
    def ensure_bucket(self, bucket: str):
        """桶不存在时创建，并顺手把生命周期 / CORS 配好。

        cvte-rag-plus 里 bucket 就是 kb_id，所以新知识库第一次直传时这里会自建桶；
        已经存在的桶（历史数据）需要跑一次 `flask init-storage` 补配置。
        """
        try:
            if self.client.bucket_exists(bucket):
                return
            self.client.make_bucket(bucket)
            origins = [o.strip() for o in os.environ.get("MINIO_CORS_ORIGINS", "").split(",") if o.strip()]
            self.configure_bucket(bucket, origins, int(os.environ.get("MINIO_ABORT_INCOMPLETE_DAYS", 1)))
        except Exception:
            logging.exception(f"ensure_bucket {bucket} failed")
            raise

    def create_multipart_upload(self, bucket: str, object_name: str, content_type: str = "application/octet-stream") -> str:
        for attempt in range(3):
            try:
                return self.client._create_multipart_upload(
                    bucket, object_name, {"Content-Type": content_type or "application/octet-stream"}
                )
            except Exception:
                logging.exception(f"create_multipart_upload {bucket}/{object_name} failed, attempt={attempt}")
                self._reset()
        raise RuntimeError("Fail to create multipart upload on MinIO")

    def presign_put_part(
        self,
        bucket: str,
        object_name: str,
        minio_upload_id: str,
        part_number: int,
        content_md5_b64: str,
        expires: int = DEFAULT_PRESIGN_EXPIRES,
    ) -> str:
        """生成绑定 Content-MD5 的分片 PUT 预签名 URL。"""
        return build_presigned_url(
            endpoint=self.public_endpoint,
            access_key=self.access_key,
            secret_key=self.secret_key,
            region=self.region,
            bucket=bucket,
            object_name=object_name,
            method="PUT",
            query_params={
                "partNumber": part_number,
                "uploadId": minio_upload_id,
            },
            signed_headers={"content-md5": content_md5_b64},
            expires=expires,
        )

    def list_parts(self, bucket: str, object_name: str, minio_upload_id: str) -> dict[int, dict]:
        """返回 {part_number: {"etag": 裸 etag, "size": bytes}}，自动翻页。"""
        parts: dict[int, dict] = {}
        marker = None
        while True:
            resp = self.client._list_parts(
                bucket,
                object_name,
                minio_upload_id,
                max_parts=1000,
                part_number_marker=marker,
            )
            for p in resp.parts:
                parts[p.part_number] = {"etag": normalize_etag(p.etag), "size": p.size}
            if not resp.is_truncated:
                break
            marker = resp.next_part_number_marker
            if not marker:
                break
        return parts

    def complete_multipart_upload(self, bucket: str, object_name: str, minio_upload_id: str, parts: dict[int, dict]):
        # S3 要求按 PartNumber 升序
        part_list = [Part(pn, info["etag"]) for pn, info in sorted(parts.items())]
        return self.client._complete_multipart_upload(bucket, object_name, minio_upload_id, part_list)

    def abort_multipart_upload(self, bucket: str, object_name: str, minio_upload_id: str):
        self.client._abort_multipart_upload(bucket, object_name, minio_upload_id)

    def stat_object(self, bucket: str, object_name: str):
        return self.client.stat_object(bucket, object_name)

    def copy_object(self, src_bucket: str, src_object: str, dst_bucket: str, dst_object: str):
        """服务端复制对象，用于 MD5 秒传（浏览器不再上传文件字节）。"""
        return self.client.copy_object(
            dst_bucket,
            dst_object,
            CopySource(src_bucket, src_object),
        )

    def stat_object_or_none(self, bucket: str, object_name: str):
        """对象存在则返回 stat，不存在返回 None（用于“已经合并过”的幂等判断）。"""
        try:
            return self.client.stat_object(bucket, object_name)
        except S3Error as e:
            if e.code in ("NoSuchKey", "NoSuchBucket", "ResourceNotFound"):
                return None
            raise
        except Exception:
            logging.exception(f"stat_object_or_none {bucket}/{object_name}")
            return None

    # ------------------------------------------------------------- bucket conf
    def set_abort_incomplete_lifecycle(self, bucket: str, days: int = 1):
        """让 MinIO 自动清掉超过 N 天未 complete 的分片垃圾。

        两个坑（都在 pgsty/silo RELEASE.2026-09-16 上实测过）：
        1. 规则里只有 AbortIncompleteMultipartUpload 会被拒（InvalidArgument），
           必须同时有 Expiration，所以这里用 ``ExpiredObjectDeleteMarker`` 占位；
        2. 即使这样提交成功，服务端**也会把 AbortIncompleteMultipartUpload 静默丢掉**，
           回读只剩 Expiration。所以写完必须回读校验，失败就明确抛错，不能假装成功。

        真正可靠的清理手段是服务端的 stale uploads 扫描：
        ``MINIO_API_STALE_UPLOADS_EXPIRY`` + ``MINIO_API_STALE_UPLOADS_CLEANUP_INTERVAL``
        （已实测：设为 1m/30s 后，100 秒内未完成的分片被自动 Abort）。
        """
        config = LifecycleConfig(
            [
                Rule(
                    ENABLED,
                    abort_incomplete_multipart_upload=AbortIncompleteMultipartUpload(days_after_initiation=days),
                    expiration=Expiration(expired_object_delete_marker=True),
                    rule_filter=Filter(prefix=""),
                    rule_id="abort-incomplete-multipart",
                )
            ]
        )
        self.client.set_bucket_lifecycle(bucket, config)

        cfg = self.client.get_bucket_lifecycle(bucket)
        for rule in (cfg.rules if cfg else []):
            abort = getattr(rule, "abort_incomplete_multipart_upload", None)
            if abort is not None and getattr(abort, "days_after_initiation", None):
                return
        raise RuntimeError(
            "server accepted the lifecycle config but dropped AbortIncompleteMultipartUpload; "
            "rely on MINIO_API_STALE_UPLOADS_EXPIRY instead"
        )

    def set_bucket_cors(self, bucket: str, origins: list[str]):
        """写桶级 CORS。

        minio-py 7.2.4 没有 CORS API，这里直接发 S3 ``PUT /?cors`` 请求
        （MinIO 要求该请求带 Content-MD5）。老版本 MinIO 可能不支持桶级 CORS
        （返回 NotImplemented），此时请改用全局 ``MINIO_API_CORS_ALLOW_ORIGIN``。
        """
        allowed_origin = "".join(f"<AllowedOrigin>{o}</AllowedOrigin>" for o in origins)
        xml = (
            '<CORSConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            "<CORSRule>"
            f"{allowed_origin}"
            "<AllowedMethod>GET</AllowedMethod>"
            "<AllowedMethod>PUT</AllowedMethod>"
            "<AllowedMethod>POST</AllowedMethod>"
            "<AllowedMethod>HEAD</AllowedMethod>"
            "<AllowedHeader>*</AllowedHeader>"
            # 浏览器要读 ETag 才能上报分片，必须显式暴露
            "<ExposeHeader>ETag</ExposeHeader>"
            "<MaxAgeSeconds>3600</MaxAgeSeconds>"
            "</CORSRule>"
            "</CORSConfiguration>"
        ).encode("utf-8")
        self.client._execute(
            "PUT",
            bucket,
            body=xml,
            headers={
                "Content-Type": "application/xml",
                "Content-MD5": base64.b64encode(hashlib.md5(xml).digest()).decode(),
            },
            query_params={"cors": ""},
        )

    def configure_bucket(self, bucket: str, cors_origins: Optional[list[str]] = None, abort_days: int = 1) -> dict:
        """一次性配置：生命周期（清垃圾）+ CORS（浏览器直传）。返回各项结果，便于 CLI 打印。"""
        result = {"bucket": bucket, "lifecycle": None, "cors": None}
        try:
            self.set_abort_incomplete_lifecycle(bucket, abort_days)
            result["lifecycle"] = "ok"
        except RuntimeError as e:
            # 服务端丢弃 abort 规则：不是配置写错，是这套 MinIO 不支持，交给 stale uploads 扫描
            if "dropped AbortIncompleteMultipartUpload" in str(e):
                result["lifecycle"] = "unsupported (server drops abort rule; using MINIO_API_STALE_UPLOADS_EXPIRY)"
                logging.info(f"lifecycle for {bucket} unsupported by server, rely on stale uploads scanner")
            else:
                result["lifecycle"] = f"failed: {e}"
                logging.warning(f"set lifecycle for {bucket} failed: {e}")
        except S3Error as e:
            result["lifecycle"] = f"failed: {e.code} {e.message}"
            logging.warning(f"set lifecycle for {bucket} failed: {e}")
        except Exception as e:
            result["lifecycle"] = f"failed: {e}"
            logging.exception(f"set lifecycle for {bucket} failed")

        if not cors_origins:
            result["cors"] = "skipped"
            return result
        try:
            self.set_bucket_cors(bucket, cors_origins)
            result["cors"] = "ok"
        except S3Error as e:
            result["cors"] = f"failed: {e.code} {e.message} (fallback: MINIO_API_CORS_ALLOW_ORIGIN)"
            logging.warning(f"set cors for {bucket} failed: {e}")
        except Exception as e:
            result["cors"] = f"failed: {e}"
            logging.exception(f"set cors for {bucket} failed")
        return result


MINIO_MULTIPART = MinioMultipart()
