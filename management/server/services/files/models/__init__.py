from peewee import *
import os
from datetime import datetime
from api.db.db_models import DB

class BaseModel(Model):
    # 添加共同的时间戳字段
    create_time = BigIntegerField(null=True)
    create_date = CharField(null=True)
    update_time = BigIntegerField(null=True)
    update_date = CharField(null=True)
    
    class Meta:
        database = DB

class Document(BaseModel):
    id = CharField(primary_key=True)
    thumbnail = TextField(null=True)
    kb_id = CharField(index=True)
    parser_id = CharField(null=True, index=True)
    parser_config = TextField(null=True)  # JSONField在SQLite中用TextField替代
    source_type = CharField(default="local", index=True)
    type = CharField(index=True)
    created_by = CharField(null=True, index=True)
    name = CharField(null=True, index=True)
    location = CharField(null=True)
    size = IntegerField(default=0)
    token_num = IntegerField(default=0)
    chunk_num = IntegerField(default=0)
    progress = FloatField(default=0)
    progress_msg = TextField(null=True, default="")
    process_begin_at = DateTimeField(null=True)
    process_duation = FloatField(default=0)
    meta_fields = TextField(null=True)  # JSONField
    run = CharField(default="0")
    status = CharField(default="1")
    
    class Meta:
        db_table = "document"

class File(BaseModel):
    id = CharField(primary_key=True)
    parent_id = CharField(index=True)
    tenant_id = CharField(null=True, index=True)
    created_by = CharField(null=True, index=True)
    name = CharField(index=True)
    location = CharField(null=True)
    size = IntegerField(default=0)
    type = CharField(index=True)
    source_type = CharField(default="", index=True)
    
    class Meta:
        db_table = "file"

class File2Document(BaseModel):
    id = CharField(primary_key=True)
    file_id = CharField(index=True)
    document_id = CharField(index=True)
    
    class Meta:
        db_table = "file2document"


class MultipartUploadTask(BaseModel):
    """Multipart upload task tracking"""
    id = CharField(primary_key=True, db_column="upload_id")  # uploadId (UUID format)
    kb_id = CharField(index=True)  # Knowledge base ID
    tenant_id = CharField(null=True, index=True)  # User/Tenant ID for ownership validation
    file_name = CharField(null=True, index=True)
    total_parts = IntegerField()  # Total number of parts
    part_size = IntegerField()  # Size per part in bytes
    location = CharField(null=True)  # Final file location after merge
    file_size = IntegerField(default=0)  # Total file size
    status = CharField(default="init", index=True)  # init, uploading, completed, expired, cancelled
    created_by = CharField(null=True, index=True)
    expire_at = BigIntegerField(null=True)  # TTL timestamp (24h expiry)
    create_time = BigIntegerField(null=True)
    
    class Meta:
        db_table = "multipart_upload_task"


class FilePart(BaseModel):
    """Individual part tracking for multipart uploads"""
    id = CharField(primary_key=True)  # Unique identifier
    upload_id = CharField(index=True)  # Foreign key to MultipartUploadTask
    part_number = IntegerField(index=True)  # Part number (1-indexed)
    etag = CharField(null=True, index=True)  # MinIO ETag after successful PUT
    md5_base64 = CharField(null=True)  # Content-MD5 used for this part
    size = IntegerField()  # Actual size in bytes
    status = CharField(default="uploaded", index=True)  # uploaded, failed, merged
    create_time = BigIntegerField(null=True)
    
    class Meta:
        db_table = "file_part"
