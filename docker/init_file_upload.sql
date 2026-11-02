-- 知识库文件分片直传（MinIO multipart）所需的主任务表
-- 说明：cvte-rag-plus 已有 `file` 表（文件树），故主任务表命名为 `file_upload`，
--       分片状态由 MinIO `ListParts` 维护，不落库。
-- 执行：mysql -uroot -p cvte_rag_plus < docker/init_file_upload.sql

CREATE DATABASE IF NOT EXISTS cvte_rag_plus DEFAULT CHARSET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE cvte_rag_plus;

-- 主任务：一条记录对应一次 MinIO multipart upload
CREATE TABLE IF NOT EXISTS `file_upload` (
  `id`              VARCHAR(32)  NOT NULL COMMENT '对外暴露的 uploadId',
  `minio_upload_id` VARCHAR(256) NOT NULL COMMENT 'MinIO 侧 multipart upload id',
  `tenant_id`       VARCHAR(32)  NOT NULL COMMENT '租户 id',
  `created_by`      VARCHAR(32)  NOT NULL COMMENT '发起上传的用户 id',
  `kb_id`           VARCHAR(32)  NOT NULL COMMENT '知识库 id',
  `bucket`          VARCHAR(128) NOT NULL COMMENT 'MinIO bucket，cvte-rag-plus 约定等于 kb_id',
  `name`            VARCHAR(255) NOT NULL COMMENT '展示文件名（已去重）',
  `location`        VARCHAR(255) NOT NULL COMMENT 'MinIO object key，init 时确定',
  `type`            VARCHAR(32)  NOT NULL DEFAULT '' COMMENT '文件类型/扩展名',
  `size`            BIGINT       NOT NULL DEFAULT 0 COMMENT '文件总字节数',
  `file_md5`        VARCHAR(32)  DEFAULT NULL COMMENT '整文件 MD5，用于秒传命中',
  `chunk_size`      INT          NOT NULL DEFAULT 5242880 COMMENT '分片大小，最后一片可更小',
  `total_parts`     INT          NOT NULL DEFAULT 0 COMMENT '分片总数，partNumber 取值 1..total_parts',
  `status`          VARCHAR(16)  NOT NULL DEFAULT 'INIT' COMMENT 'INIT|UPLOADING|MERGING|DONE|FAILED|ABORTED',
  `etag`            VARCHAR(128) DEFAULT NULL COMMENT 'merge 完成后对象的 etag',
  `doc_id`          VARCHAR(32)  DEFAULT NULL COMMENT 'merge 成功后写入的知识库文档 id',
  `error_msg`       LONGTEXT     DEFAULT NULL COMMENT 'merge 失败原因',
  `expire_at`       BIGINT       DEFAULT NULL COMMENT '任务过期时间（epoch 毫秒）',
  `create_time`     BIGINT       DEFAULT NULL,
  `create_date`     DATETIME     DEFAULT NULL,
  `update_time`     BIGINT       DEFAULT NULL,
  `update_date`     DATETIME     DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `file_upload_tenant_id` (`tenant_id`),
  KEY `file_upload_created_by` (`created_by`),
  KEY `file_upload_kb_id` (`kb_id`),
  KEY `file_upload_kb_id_file_md5_size` (`kb_id`, `file_md5`, `size`),
  KEY `file_upload_status` (`status`),
  KEY `file_upload_doc_id` (`doc_id`),
  KEY `file_upload_expire_at` (`expire_at`),
  KEY `file_upload_create_time` (`create_time`),
  KEY `file_upload_create_date` (`create_date`),
  KEY `file_upload_update_time` (`update_time`),
  KEY `file_upload_update_date` (`update_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='分片直传任务主表';

-- 旧版本的 `file_upload_part` 表已不再使用，确认无需留档后可手工清理：
-- DROP TABLE IF EXISTS `file_upload_part`;
