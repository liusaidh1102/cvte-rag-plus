# 知识库文件分片直传（浏览器 → MinIO Multipart）方案与实现

> 目标：大文件不经过应用服务器内存，浏览器直传 MinIO；每个分片只能上传"内容完全一致"的那一段；
> 分片状态只以 MinIO `ListParts` 为准（不写 Redis、不写 MySQL 分片表）；合并后写入知识库文档表。
>
> 代码位置：
> - 接口：`api/apps/file_upload_app.py`
> - 存储能力：`rag/utils/minio_multipart.py`
> - 分片状态：MinIO `ListParts`（`rag/utils/minio_multipart.py`）
> - 数据访问：`api/db/services/file_upload_service.py`
> - 前端上传器：`web/src/utils/multipart-upload.ts`（`md5.ts` / `md5.worker.ts`）

---

## 一、方案分析：哪里会出问题，怎么解

### 1. 核心安全点：预签名 URL 必须"钉死"分片内容

需求是"每个分片独立签名，只能上传该 MD5 对应的分片内容，防止拿到 URL 乱传别的文件"。
这要求 `Content-MD5` 参与签名，而不只是"请求里带一个 header"。

**结论：minio-py 做不到。** `minio/signer.py` 里 `_get_presign_canonical_request_hash()` 把
`SignedHeaders` 硬编码成 `host`：

```python
canonical_headers, signed_headers = "host:" + url.netloc, "host"
```

SDK 也没有任何参数能塞入额外签名头（`get_presigned_url(extra_query_params=...)` 只能加 query）。
因此 `rag/utils/minio_multipart.py` 里按 AWS SigV4 query-string 规范自研了 `build_presigned_url()`，
把 `SignedHeaders` 设为 `content-md5;host`。

这样形成两道锁：

| 攻击/误操作 | 服务端行为 | 实测状态码 |
| --- | --- | --- |
| 换个 `Content-MD5` 头去传别的文件 | 签名不匹配 | **403 SignatureDoesNotMatch** |
| 头写对了，body 换掉 | MinIO 校验 body 摘要 | **400 BadDigest** |
| 干脆不带 `Content-MD5` | 签名要求的头缺失 | **400 AccessDenied** |
| 改 `partNumber` 复用 URL | 签名不匹配 | **403** |

> 签名算法已与 minio-py 的 `presign_v4` 做过逐字节比对：host-only 场景下两者 `X-Amz-Signature`
> 完全一致（含中文/空格对象名），说明 path 编码、query 排序、签名密钥派生都正确；再叠加
> `content-md5` 头即可。

### 2. 分片状态：MinIO `ListParts` 是唯一真相

既不维护 Redis Bitmap，也不写 MySQL 分片表；`list-missing-chunk` 和 `merge` 都直接问 MinIO。

实现细节：

- `ListParts` 自动翻页，每页最多 1000 片；1GB/5MB 只有 205 片，一次调用即可。
- 分片元数据（partNumber / etag / size）本来就由 S3/MinIO 持久化，再存一份只是第二真相源。
- `chunk-notify` 已移除：前端 PUT 成功即完成，每片少一次 HTTP 往返和一次 DB 写入。
- merge 用同一次 `ListParts` 的结果做完整性校验，并直接作为 `CompleteMultipartUpload` 入参。
- 如果 multipart 已经 complete、`ListParts` 失效，则用 `stat_object` 判断对象是否已经存在。

### 3. 幂等：三道防线

| 场景 | 处理 |
| --- | --- |
| 同一分片重复 PUT | S3 语义：同 partNumber 重传直接覆盖，etag 以最后一次为准；`Content-MD5` 保证覆盖内容一致 |
| 重复点击 merge | 状态机 CAS：`UPDATE ... SET status='MERGING' WHERE id=? AND status IN ('INIT','UPLOADING','FAILED')`，只有一个请求能抢到；`status='DONE'` 直接回已有 `docId` |
| 上次 merge 进程挂了（卡在 MERGING） | `touch_stale_merging()`：超过 10 分钟允许后来者接管，避免任务永久卡死 |

不用 Redis 分布式锁做主互斥 —— 多一层依赖就多一种"锁住了但没人释放"的故障模式；
CAS 落在 MySQL 上，天然跨进程有效。

### 4. 完整性校验：一次 `ListParts` 回答三个问题

merge 只查一次 MinIO，拿到 `{partNumber: {etag, size}}` 后：

| 校验 | 判断 | 不通过时 |
| --- | --- | --- |
| 分片号齐全 | 集合恰好等于 `1..totalParts` | 返回 `missingParts`，前端重传 |
| 总大小一致 | `sum(size) == 声明 size` | 报错；确认声明有误可传 `force: true` |
| 非末片大小 | 每个 `partNumber < totalParts` 的片 `>= 5MiB` | 提前给出 `EntityTooSmall` 的可读错误 |

同一次结果又直接作为 `CompleteMultipartUpload` 的入参，不存在"先校验再查一遍"的额外开销。

### 5. 表名：只有 `file_upload`，不是 `file` / `file_part`

需求写的是 `file`（主任务）+ `file_part`，但 cvte-rag-plus 已经有 `file` 表（文件树，带
`parent_id` / `source_type` / `file2document` 一整套语义）。直接复用会污染文件树逻辑，
所以主任务单独用 `file_upload`，分片明细不落库、由 MinIO 维护。`merge` 成功后仍按原有约定
写 `document` + `file` + `file2document`，知识库列表里能正常看到这个文件。

### 6. 对象 key 必须在 init 时定死

MinIO 的 multipart upload 在 `CreateMultipartUpload` 那一刻就绑定了 object key，之后不能改名。
所以 init 时就要完成"重名去重"（`duplicate_name` + `obj_exist` 探测），并把最终 `location`
存进任务表；merge 写 `document.location` 时直接用这个值。

### 7. CORS：两个必配项，少一个前端就瘸

浏览器直传是跨域 PUT（前端 3000/8888 → MinIO 9000），必须满足：

1. `Access-Control-Allow-Headers` 放行 `content-md5`（否则预检就挂）；
2. `Access-Control-Expose-Headers` 暴露 `ETag` —— 前端不再上报 etag，这一项已非必需，
   保留只是为了排障时能在 DevTools 里直接看到响应头。

已实测桶级 CORS 生效：预检返回 `Access-Control-Allow-Headers: content-md5`、
`Access-Control-Expose-Headers: ETag`。

> minio-py 7.2.4 没有 CORS API，所以 `set_bucket_cors()` 直接发 S3 `PUT /?cors`
> （注意 MinIO 要求该请求带 `Content-MD5`，否则 `MissingContentMD5`）。
> 兜底方案是全局 `MINIO_API_CORS_ALLOW_ORIGIN`。

### 8. 分片垃圾清理：S3 生命周期在这套 MinIO 上不可用（实测）

先说结论：**不要依赖 `AbortIncompleteMultipartUpload` 生命周期规则**，本仓库用的
`pgsty/silo:latest`（RELEASE.2026-09-16）会"收下但静默丢弃"。

实测过程：

| 提交的规则 | PUT 结果 | 回读结果 |
| --- | --- | --- |
| 只有 `AbortIncompleteMultipartUpload` | 400 InvalidArgument | — |
| `Expiration(Days=3650)` + abort | 200 OK | **abort 被丢弃**，只剩 Expiration |
| `Expiration(ExpiredObjectDeleteMarker=true)` + abort | 200 OK | **abort 被丢弃** |

所以 `set_abort_incomplete_lifecycle()` 写完会**回读校验**，发现被丢弃就抛错，
`configure_bucket()` 会如实返回 `lifecycle=failed: ... rely on MINIO_API_STALE_UPLOADS_EXPIRY`，
CLI 打印出来，不会假装成功。

真正生效的是 MinIO 服务端的 stale uploads 扫描：

```yaml
MINIO_API_STALE_UPLOADS_EXPIRY=24h            # 超过 24h 未 complete 就 Abort
MINIO_API_STALE_UPLOADS_CLEANUP_INTERVAL=1h   # 扫描间隔
```

已验证：把这两个值改成 `1m` / `30s` 起一个临时实例，创建一个 multipart upload，
100 秒后 `ListParts` 返回 `NoSuchUpload` —— 清理确实发生了。默认值就是 24h，
与任务 TTL 天然对齐，所以在 `docker-compose-base.yml` 里显式写出来即可。

> 顺带说明：S3 生命周期最小粒度是"天"，而 stale uploads 支持小时/分钟，粒度更合适。

### 9. 分片大小与上限

- 分片 5MB：正好是 S3 允许的最小分片（除最后一片），既不浪费请求数也不会被 `EntityTooSmall` 拒绝。
- 单次 multipart 最多 10000 片 → 5MB × 10000 = **50GB** 上限；init 时校验，超了提示调大
  `chunk_size`。
- 中间分片若小于 5MB，`complete` 一定失败；merge 前先检查并给出可读错误，而不是让 MinIO
  抛 `EntityTooSmall`。
- 声明 size 与实际上传总字节不一致时拒绝合并（可传 `force: true` 跳过，用于"前端 size 算错"的补救）。

### 10. 前端 MD5：Web Crypto 不支持 MD5

`crypto.subtle.digest` 只支持 SHA-1/256/384/512，**没有 MD5**，而 S3 的 `Content-MD5` 就是 MD5。
所以自带了一份极简 MD5（`web/src/utils/md5.ts`，无第三方依赖），并用标准测试向量 +
5MB 随机数据与 Python `hashlib` 逐字节比对通过。

性能：5MB 分片算一次约几十毫秒，但 5 并发 + 页面滚动时主线程会掉帧，所以放进 Web Worker；
主线程维护 5 个 Worker 的池（与上传并发一致），每个 Worker 串行处理，`new Worker(new URL(...))`
由 webpack 5 打包；创建失败自动退化为主线程计算。

### 11. 并发与重试

- 并发 5：浏览器同域一般 6 个连接，留 1 个给其它请求（心跳、进度查询）。
- 单分片 PUT 失败重试 3 次，指数退避；**每次重试都重新签名**（URL 有 15 分钟有效期，
  403 多半是过期或签名问题）。

---

## 二、接口契约

统一前缀 `/v1/file_upload`，全部要求登录态（`Authorization` 头），返回体沿用
`{"code": 0, "message": "success", "data": {...}}`。

> 兼容性：`/chunk-notify` 已删除。旧版前端仍会调用它并收到 404，重试 3 次后放弃；
> PUT 与 merge 都不依赖该接口，所以上传结果不受影响，前后端可以独立升级。

### 1. POST `/init`

```jsonc
// 请求
{
  "kb_id": "a1b2...",
  "name": "年报.pdf",
  "size": 5244114,
  "chunk_size": 5242880,
  "fileMd5": "6f1c8d..."             // 整文件 MD5，32 位十六进制
}
// 响应 data
{
  "uploadId": "6f1c...",          // 对外 id（不是 MinIO 内部 uploadId）
  "kbId": "a1b2...",
  "bucket": "a1b2...",            // cvte-rag-plus 约定 bucket == kb_id
  "name": "年报.pdf",
  "location": "年报.pdf",          // 最终 object key（重名会加 _）
  "size": 5244114,
  "chunkSize": 5242880,
  "totalParts": 2,
  "partExpiresIn": 900,
  "status": "INIT"
}
```

做的事情：校验 kb 权限 → **按 `kb_id + fileMd5 + size` 查秒传** → 未命中则文件名去重 →
`CreateMultipartUpload` → 落 `file_upload`。

命中秒传时不创建 multipart upload，而是在 MinIO 服务端把已有对象复制到新 key，立即建文档并返回：

```jsonc
{
  "uploadId": "...",
  "docId": "...",
  "name": "年报.pdf",
  "location": "年报_1.pdf",
  "size": 5244114,
  "etag": "...",
  "status": "DONE",
  "instant": true,
  "sourceUploadId": "..."
}
```

> 秒传只在同一知识库内复用。不能直接让两个 document 共享同一个 object key，因为删除接口会按
> `document.location` 删除对象；这里改用服务端 `CopyObject`，浏览器上传字节为 0。

### 2. POST `/get-presigned-part`

```jsonc
// 请求
{ "uploadId": "6f1c...", "partNumber": 1, "chunkMd5": "TChkDcjfGTOq6hkhANUK4A==" }
// 响应 data
{
  "partNumber": 1,
  "uploaded": false,
  "url": "http://minio:9000/<bucket>/<key>?X-Amz-Algorithm=...&X-Amz-SignedHeaders=content-md5%3Bhost&...",
  "method": "PUT",
  "headers": { "Content-MD5": "TChkDcjfGTOq6hkhANUK4A==" },
  "expiresIn": 900
}
```

- `chunkMd5` 必须是 base64 的 16 字节 MD5，否则 400；
- `partNumber` 必须在 `1..totalParts`；
- 分片是否已上传由 `list-missing-chunk` 决定；前端只对缺失分片申请 URL。
- PUT 成功即完成，前端不需要再回调服务端；分片清单在 merge 时统一从 MinIO 读取。

### 3. GET `/list-missing-chunk?uploadId=xxx`

```jsonc
// 响应 data
{
  "uploadId": "6f1c...",
  "status": "UPLOADING",
  "name": "年报.pdf",
  "size": 5244114,
  "chunkSize": 5242880,
  "totalParts": 2,
  "uploadedParts": 1,
  "missingParts": [2],
  "source": "minio"         // minio | object（object = 已合并完成，直接 merge 即可）
}
```

### 4. POST `/merge`

```jsonc
// 请求（force 可选：声明 size 与实际不符时强制合并）
{ "uploadId": "6f1c...", "force": false }
// 成功
{
  "uploadId": "6f1c...", "docId": "9a3b...", "name": "年报.pdf",
  "location": "年报.pdf", "size": 5244114,
  "etag": "e8919f82...-2", "status": "DONE", "merged": true
}
// 分片不齐：code = 102
{ "code": 102, "message": "1 part(s) missing, upload them before merge",
  "data": { "uploadId": "6f1c...", "missingParts": [2], "merged": false } }
```

顺序：CAS 抢 MERGING → `ListParts` 取分片清单 → 完整性校验 →
size/最小分片校验 → `CompleteMultipartUpload` → `stat_object` → 写 `document` + `file` +
`file2document` → 状态 DONE。

---

## 三、数据模型

```sql
-- 主任务
CREATE TABLE file_upload (
  id              VARCHAR(32)  PRIMARY KEY,   -- 对外 uploadId
  minio_upload_id VARCHAR(256) NOT NULL,      -- MinIO 内部 uploadId
  tenant_id       VARCHAR(32)  NOT NULL,
  created_by      VARCHAR(32)  NOT NULL,
  kb_id           VARCHAR(32)  NOT NULL,
  bucket          VARCHAR(128) NOT NULL,
  name            VARCHAR(255) NOT NULL,      -- 展示文件名（已去重）
  location        VARCHAR(255) NOT NULL,      -- object key
  type            VARCHAR(32)  NOT NULL,
  size            BIGINT       DEFAULT 0,
  chunk_size      INT          DEFAULT 5242880,
  total_parts     INT          DEFAULT 0,
  status          VARCHAR(16)  NOT NULL DEFAULT 'INIT',  -- INIT/UPLOADING/MERGING/DONE/FAILED/ABORTED
  etag            VARCHAR(128),
  doc_id          VARCHAR(32),
  error_msg       TEXT,
  expire_at       BIGINT,                     -- epoch 毫秒
  create_time BIGINT, create_date DATETIME, update_time BIGINT, update_date DATETIME
);

-- 分片明细不落库：partNumber / etag / size 由 MinIO ListParts 提供
```

建表：`init_database_tables()` 会随服务启动自动 `create_table()`，无需手工执行 SQL。

---

## 四、时序

```
浏览器                       应用(Flask)                  MySQL          MinIO
  │  POST /init                │                            │              │
  │───────────────────────────>│ 校验kb/去重/建任务          │              │
  │                            │──────────────────────────────────────────>│ CreateMultipartUpload
  │                            │ INSERT file_upload ───────>│              │
  │<── uploadId,totalParts ────│                            │              │
  │  GET /list-missing-chunk   │                            │              │
  │───────────────────────────>│ ListParts ────────────────────────────────>│
  │<── missingParts=[1..n] ────│                            │              │
  │                            │                            │              │
  │  ┌─ 每个缺失分片（并发 5）──────────────────────────────────────────────┐
  │  │ Worker 算 base64(MD5(chunk))                                        │
  │  │ POST /get-presigned-part ─> SigV4(签名头 content-md5;host)           │
  │  │ PUT <url>  Header: Content-MD5 ─────────────────────────────────────>│ 校验签名+摘要
  │  │<──────────────────────────────────────────── 200 + ETag ────────────│
  │  └─────────────────────────────────────────────────────────────────────┘
  │                            │                            │              │
  │  POST /merge               │ CAS MERGING                │              │
  │───────────────────────────>│ ListParts ────────────────────────────────>│
  │                            │ CompleteMultipartUpload ──────────────────>│
  │                            │ 写 document/file/file2document             │
  │<── docId ──────────────────│                            │              │
```

---

## 五、部署与配置

### 1. 环境变量（`docker/.env`）

| 变量 | 说明 |
| --- | --- |
| `MINIO_PUBLIC_ENDPOINT` | **浏览器可达**的 MinIO 地址，生成预签名 URL 用；留空回退 `MINIO_VISIT_HOST:MINIO_PORT`。生产建议填反代后的 https 域名 |
| `MINIO_API_CORS_ALLOW_ORIGIN` | MinIO 全局 CORS 白名单，`*` 或逗号分隔来源 |
| `MINIO_CORS_ORIGINS` | 新建知识库桶时自动写入的桶级 CORS 来源 |
| `MINIO_ABORT_INCOMPLETE_DAYS` | 未完成分片的清理天数，默认 1 |
| `MINIO_PRESIGN_EXPIRES` | 分片 URL 有效期（秒），默认 900 |
| `KB_UPLOAD_TASK_TTL` | 任务 TTL（秒），默认 86400 |

> `MINIO_PUBLIC_ENDPOINT` 是最容易踩的坑：容器内 `MINIO_HOST=minio` 生成的 URL 浏览器打不开。

### 2. 建表（已执行）

```bash
docker exec -i cvte-rag-plus-mysql mysql -uroot -p < docker/init_file_upload.sql
```

执行后按提示输入 `docker/.env` 中配置的 `MYSQL_PASSWORD`。

脚本在 `docker/init_file_upload.sql`，两条 `CREATE TABLE IF NOT EXISTS`，字段与 peewee 模型一一对应；
服务启动时 `init_database_tables()` 也会 `create_table()`（IF NOT EXISTS），两边不冲突。

### 3. 给已有桶补配置

新知识库第一次直传时会自动建桶并写 CORS（`ensure_bucket` → `configure_bucket`）；
历史桶需要手动跑一次：

```bash
# 注意：compose 里的服务名是 app（容器名才是 cvte-rag-plus-server）
docker compose exec app flask --app api.apps init-storage
# 只处理指定桶 / 指定来源
docker compose exec app flask --app api.apps init-storage \
  --bucket <kb_id> --cors-origin https://rag.example.com --abort-days 1
```

> 镜像里如果没有 `init-storage`（旧构建），说明镜像是按旧代码打的，需要重新构建镜像；
> 在此之前可以先用等价的 Python 调用（`MINIO_MULTIPART.configure_bucket`）把桶配好。

### 4. 让接口生效：需要重新构建镜像

`docker/docker-compose.yml` 里 `app` 服务只有 `image:` 没有 `build:`，源码是 `COPY` 进镜像的，
所以改了 `api/` `rag/` 之后必须重新打镜像（否则容器里根本没有 `file_upload_app.py`）：

```bash
# 在仓库根目录，tag 与 docker/.env 的 CVTE_RAG_PLUS_IMAGE 保持一致
docker build -t cvte-rag-plus:v0.6.0 .
cd docker && docker compose up -d app
```

重启后 `init_database_tables()` 会对 `file_upload` 执行 `CREATE TABLE IF NOT EXISTS`，
并由 `migrate_db()` 自动补齐 `file_upload.file_md5` 及 `(kb_id, file_md5, size)` 索引，
不会动已有数据；旧版本的 `file_upload_part` 不再读写，确认无需留档后可手工 `DROP`。随后即可用
`docker compose exec app flask --app api.apps init-storage` 补桶配置。

### 3. 前端接入

```ts
import { multipartUpload } from '@/utils/multipart-upload';

const controller = new AbortController();
const result = await multipartUpload({
  kbId,
  file,
  chunkSize: 5 * 1024 * 1024,   // 默认 5MB
  concurrency: 5,               // 默认 5
  signal: controller.signal,
  onProgress: ({ percent, phase, uploadedParts, totalParts }) =>
    setProgress({ percent, phase, uploadedParts, totalParts }),
});
// result.docId 就是知识库里的文档 id，接着调 /document/run 触发解析即可

// 内部会自动：
// 1. 流式计算整文件 MD5，init 时命中秒传则直接返回；
// 2. 按 kbId + fileName + size + lastModified 把 uploadId 存入 localStorage；
// 3. 刷新后自动取回 uploadId，调 list-missing-chunk 只补缺失分片。
```

---

## 六、实测结论（真实 MinIO / MySQL，非 mock）

在仓库自带的 docker 环境（`pgsty/silo` + mysql 8）上逐项验证：

| 用例 | 结果 |
| --- | --- |
| 2 分片（5MB + 1234B）直传 + 合并 | 内容与拼接结果逐字节一致，`stat.size=5244114`，etag `...-2` |
| 换成别的 `Content-MD5` 头 | 403 SignatureDoesNotMatch |
| 头对、body 换掉 | 400 BadDigest |
| 不带 `Content-MD5` | 400 AccessDenied |
| 篡改 `partNumber` | 403 |
| `ListParts` 与实际分片号/大小 | 完全一致 |
| 5 个真实桶写 CORS | 全部 ok；预检 200，`Allow-Headers: content-md5`、`Expose-Headers: ETag` |
| 5 个真实桶写 lifecycle | **该镜像会丢弃 AbortIncompleteMultipartUpload**，已改为 `MINIO_API_STALE_UPLOADS_EXPIRY` |
| stale uploads 清理（1m/30s 临时实例） | 100 秒后 `ListParts` 返回 NoSuchUpload，清理生效 |
| 建表 SQL | 已执行，唯一索引生效（重复 part 插入报 1062） |
| `ListParts` 查缺 | 与实际上传分片一致，缺失列表正确 |
| 前端 MD5：标准向量 + 5MB 数据 | 与 `hashlib` 结果一致 |

---

## 七、已知边界 / 后续可做

1. **过期任务清理**：`file_upload.expire_at` 已落库，但目前只靠 MinIO 生命周期清分片；
   可以加一个定时任务把 `expire_at < now` 且未完成的任务置 `ABORTED` 并 `AbortMultipartUpload`。
2. **秒传范围**：当前只在同一知识库内按 `kb_id + file_md5 + size` 命中；跨知识库秒传需要额外
   的租户级对象索引与权限校验。
3. **服务端加密 / 校验和**：如启用 SSE-C 或 `x-amz-checksum-*`，签名头需要相应扩展。
4. **限流**：目前按用户维度没有速率限制，公网部署建议在网关层对 `/get-presigned-part` 做限流
   （它是最容易被刷的接口）。
5. **浏览器兼容**：Worker 用 `new Worker(new URL(...), {type:'module'})`，需要 webpack 5；
   不支持时自动退化主线程计算，功能不受影响。
