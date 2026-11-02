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
import logging
from datetime import datetime

from api.db import UploadStatus
from api.db.db_models import DB, FileUpload
from api.db.services.common_service import CommonService
from api.utils import current_timestamp, datetime_format


class FileUploadService(CommonService):
    model = FileUpload

    @classmethod
    @DB.connection_context()
    def get_by_upload_id(cls, upload_id, created_by=None):
        """按 uploadId 取任务；带 created_by 时顺带做归属校验。"""
        try:
            query = cls.model.select().where(cls.model.id == upload_id)
            if created_by:
                query = query.where(cls.model.created_by == created_by)
            return query.first()
        except Exception:
            logging.exception(f"get_by_upload_id {upload_id}")
            return None

    @classmethod
    @DB.connection_context()
    def find_completed_by_md5(cls, kb_id, file_md5, size):
        """查找同知识库下内容相同的已完成上传，供 MD5 秒传复用对象。"""
        if not file_md5:
            return None
        try:
            return (
                cls.model.select()
                .where(
                    (cls.model.kb_id == kb_id)
                    & (cls.model.file_md5 == file_md5)
                    & (cls.model.size == size)
                    & (cls.model.status == UploadStatus.DONE.value)
                    & (cls.model.doc_id.is_null(False))
                )
                .order_by(cls.model.create_time.desc())
                .first()
            )
        except Exception:
            logging.exception(f"find_completed_by_md5 {kb_id}/{file_md5}/{size}")
            return None

    @classmethod
    @DB.connection_context()
    def cas_status(cls, upload_id, from_status, to_status, **extra):
        """状态机 CAS：只有当前状态在 from_status 里才推进，返回是否抢到。

        合并接口靠它做互斥：多个 tab / 重复点击只会有一个请求真正去 complete，
        而且不依赖 Redis 锁是否可用。
        """
        data = {"status": to_status}
        data.update(extra)
        data["update_time"] = current_timestamp()
        data["update_date"] = datetime_format(datetime.now())
        try:
            return cls.model.update(**data).where(
                (cls.model.id == upload_id) & (cls.model.status.in_(list(from_status)))
            ).execute() == 1
        except Exception:
            logging.exception(f"cas_status {upload_id} {from_status} -> {to_status}")
            return False

    @classmethod
    @DB.connection_context()
    def touch_stale_merging(cls, upload_id, stale_ms):
        """接管僵死的 MERGING（进程在合并中途挂掉）。

        只更新 update_time，因此一定产生行变更；条件里带 update_time 上限，
        保证同一时刻只有一个请求能接管。
        """
        now = current_timestamp()
        try:
            return cls.model.update(
                update_time=now, update_date=datetime_format(datetime.now())
            ).where(
                (cls.model.id == upload_id)
                & (cls.model.status == UploadStatus.MERGING.value)
                & (cls.model.update_time < now - stale_ms)
            ).execute() == 1
        except Exception:
            logging.exception(f"touch_stale_merging {upload_id}")
            return False

    @classmethod
    @DB.connection_context()
    def mark_failed(cls, upload_id, error_msg):
        try:
            cls.model.update(status=UploadStatus.FAILED.value, error_msg=str(error_msg)[:2000]).where(
                cls.model.id == upload_id
            ).execute()
        except Exception:
            logging.exception(f"mark_failed {upload_id}")
