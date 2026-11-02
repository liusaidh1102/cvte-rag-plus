# CVTE-RAG-Plus 本地 / Docker 双模式启动

> 以下路径以 `D:\code\cvte-rag-plus` 为例，若你的本地目录不同，请替换为实际路径。

项目固定使用 Docker 运行基础设施，业务服务是 **一个 Flask 后端 + 两个前端**：

- 前台 Web：`web`
- 管理后台 Web：`management/web`
- 统一后端：`api/server.py`
- 数据库访问：Peewee（统一后端共用模型和连接）
- 登录：随机 Token + Redis Session（默认 8 小时）
- 知识库大文件：统一后端的 5MB 分片直传 MinIO

基础设施端口：

- MySQL：`5455`
- Redis：`6379`
- MinIO：`9000` / 控制台 `9001`
- Elasticsearch：`1200`
- Kibana：`6601`

---

## 一、一次性准备

### 1. Docker Desktop

```powershell
docker version
docker compose version
```

### 2. Python 环境

只需要一个后端虚拟环境：

```powershell
cd D:\code\cvte-rag-plus

$env:UV_CACHE_DIR = "D:\code\cvte-rag-plus\.uv-cache2"
uv python install 3.10
uv venv --python 3.10 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
```

`management/server/.venv` 不再需要。管理端路由已经注册到统一后端中。

### 3. 前端依赖

```powershell
cd D:\code\cvte-rag-plus\web
npm install

cd D:\code\cvte-rag-plus\management\web
pnpm install
```

### 4. 必需配置

复制配置模板：

```powershell
Copy-Item docker/.env.example docker/.env
```

然后在 `docker/.env` 中配置：

```dotenv
MANAGEMENT_ADMIN_PASSWORD=<至少12位的非默认密码>
```

可选配置：

```dotenv
AUTH_TOKEN_TTL_SECONDS=28800
```

`MANAGEMENT_JWT_SECRET` 不再用于登录。

---

## 二、本地开发：两个前端 + 一个后端

```powershell
cd D:\code\cvte-rag-plus
.\scripts\dev.ps1
```

脚本会：

1. 用 Docker Compose 启动 MySQL、Redis、MinIO、Elasticsearch、Kibana。
2. 停止 Docker 中的 `app` 和 `management-frontend`，释放端口。
3. 启动统一 API、解析 Worker（源码完整时）、前台 Web、管理后台 Web。
4. 把日志同时输出到控制台和 `logs/dev/`。

入口：

- 前台：<http://localhost:8000>
- 管理后台：<http://localhost:3333>
- 统一后端：<http://localhost:9380>
- MinIO 控制台：<http://localhost:9001>
- Kibana：<http://localhost:6601>

两个前端开发服务器都把 `/api` 和 `/v1` 代理到 `http://127.0.0.1:9380`。

常用参数：

```powershell
# 基础设施已启动，只重启本地应用
.\scripts\dev.ps1 -SkipInfra

# 只启动统一后端和 Worker
.\scripts\dev.ps1 -NoFrontend

# 只启动两个前端
.\scripts\dev.ps1 -FrontendOnly

# 只启动 Docker 基础设施
.\scripts\dev.ps1 -InfraOnly
```

按一次 `Ctrl+C` 停止全部本地应用进程，Docker 基础设施继续运行。

日志目录：`D:\code\cvte-rag-plus\logs\dev\`

```text
all.log
api.log
worker.log
web.log
mgmt-web.log
```

---

## 三、只启动 Docker 基础设施

```powershell
cd D:\code\cvte-rag-plus
.\scripts\start.ps1 infra
```

等价命令：

```powershell
docker compose --env-file docker\.env -f docker\docker-compose-base.yml `
  up -d mysql redis minio es01 kibana
```

检查：

```powershell
docker compose --env-file docker\.env -f docker\docker-compose-base.yml ps
```

停止基础设施但保留数据卷：

```powershell
docker compose --env-file docker\.env -f docker\docker-compose-base.yml `
  stop mysql redis minio es01 kibana
```

---

## 四、全部应用使用 Docker

```powershell
cd D:\code\cvte-rag-plus
.\scripts\start.ps1 docker -Build
```

入口：

- 前台：<http://localhost>
- 管理后台：<http://localhost:8888>
- 统一后端：<http://localhost:9380>

Docker 中只有 `app` 一个业务后端；管理后台 Nginx 的 `/api` 和 `/v1` 都代理到 `app:9380`。

停止应用但保留基础设施：

```powershell
docker compose --env-file docker\.env -f docker\docker-compose.yml `
  stop app management-frontend
```

---

## 五、数据库与上传说明

- 主 API 和迁移后的管理路由统一使用 `api.db.db_models.DB`（Peewee）。
- 管理端添加知识库文档时，使用主 API：
  - `/v1/file_upload/init`
  - `/v1/file_upload/get-presigned-part`
  - `/v1/file_upload/list-missing-chunk`
  - `/v1/file_upload/merge`
- 浏览器直接 PUT 到 MinIO 分片地址，后端只负责签名、状态和合并。
- 原有的“选择已有文件添加”入口保留。

---

## 六、常见问题

### 端口被占用

```powershell
docker compose --env-file docker\.env -f docker\docker-compose.yml `
  stop app management-frontend
```

再检查本地端口：

```powershell
Get-NetTCPConnection -State Listen |
  Where-Object LocalPort -in 8000,3333,9380 |
  Select-Object LocalAddress,LocalPort,OwningProcess
```

如果机器上还残留旧版 `management-backend` 容器，手动停止一次即可：

```powershell
docker ps -a --filter name=cvte-rag-plus-management-backend
docker stop cvte-rag-plus-management-backend
```

### 管理后台登录返回 503

说明 Redis 不可用或 `MANAGEMENT_ADMIN_PASSWORD` 未加载：

```powershell
docker compose --env-file docker\.env -f docker\docker-compose-base.yml ps redis
docker restart cvte-rag-plus-redis
```

### 管理后台接口返回 401 / 403

- `401`：Token 缺失、过期或已退出，重新登录。
- `403`：当前用户没有目标知识库、文档、文件或团队的权限。

### 本地后端连不上基础设施

```powershell
docker compose --env-file docker\.env -f docker\docker-compose-base.yml ps
docker compose --env-file docker\.env -f docker\docker-compose-base.yml logs -f mysql es01
```
