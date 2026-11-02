# CVTE-RAG-Plus 启动说明

> 以下路径以 `D:\code\cvte-rag-plus` 为例，若你的本地目录不同，请替换为实际路径。

> 更新时间：2026-11-02
>
> 架构：Docker 只负责 MySQL、Redis、MinIO、Elasticsearch、Kibana；本地开发运行 **一个后端 + 两个前端**。

---

## 一、最常用：本地开发模式

```powershell
cd D:\code\cvte-rag-plus
.\scripts\dev.ps1
```

脚本会：

1. 启动 Docker 中的 MySQL、Redis、MinIO、Elasticsearch、Kibana。
2. 停止 Docker 中的 `app`、`management-frontend`，避免端口冲突。
3. 启动统一 API、解析 Worker（源码完整时）、前台 Web、管理后台 Web。
4. 把日志汇总到控制台并写入 `logs/dev/`。

按一次 `Ctrl+C` 只停止本地应用，不停止 Docker 基础设施。

入口：

| 服务 | 地址 |
| --- | --- |
| 前台 Web | http://localhost:8000 |
| 管理后台 Web | http://localhost:3333 |
| 统一后端 | http://localhost:9380 |
| MinIO 控制台 | http://localhost:9001 |
| Kibana | http://localhost:6601 |
| Elasticsearch | http://localhost:1200 |
| MySQL | `localhost:5455` |
| Redis | `localhost:6379` |

常用参数：

```powershell
# 基础设施已启动，只重启应用
.\scripts\dev.ps1 -SkipInfra

# 只启动统一后端和 Worker
.\scripts\dev.ps1 -NoFrontend

# 只启动两个前端
.\scripts\dev.ps1 -FrontendOnly

# 只启动 Docker 基础设施
.\scripts\dev.ps1 -InfraOnly
```

兼容入口：

```powershell
.\scripts\start.ps1 local
.\scripts\start.ps1 local -NoFrontend
```

---

## 二、只启动 Docker 基础设施

```powershell
cd D:\code\cvte-rag-plus
.\scripts\start.ps1 infra
```

等价命令：

```powershell
docker compose --env-file docker\.env `
  -f docker\docker-compose-base.yml `
  up -d mysql redis minio es01 kibana
```

检查：

```powershell
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
```

停止基础设施但保留数据卷：

```powershell
docker compose --env-file docker\.env `
  -f docker\docker-compose-base.yml `
  stop mysql redis minio es01 kibana
```

---

## 三、全部应用使用 Docker

```powershell
cd D:\code\cvte-rag-plus

# 使用已有应用镜像
.\scripts\start.ps1 docker

# Python / 前端代码修改后重新构建
.\scripts\start.ps1 docker -Build
```

入口：

| 服务 | 地址 |
| --- | --- |
| 前台 Web | http://localhost |
| 管理后台 Web | http://localhost:8888 |
| 统一后端 | http://localhost:9380 |

只停止 Docker 应用，保留基础设施：

```powershell
docker compose --env-file docker\.env -f docker\docker-compose.yml `
  stop app management-frontend
```

---

## 四、环境与配置

| 内容 | 位置 |
| --- | --- |
| 主 Python 环境 | `D:\code\cvte-rag-plus\.venv` |
| 前台依赖 | `web\node_modules` |
| 管理后台依赖 | `management\web\node_modules` |
| Docker 配置 | `docker\.env` |
| 日志 | `logs\dev` |

必须配置：

```dotenv
MANAGEMENT_ADMIN_PASSWORD=<至少12位的非默认密码>
```

可选：

```dotenv
AUTH_TOKEN_TTL_SECONDS=28800
```

`MANAGEMENT_JWT_SECRET` 不再用于登录。管理后台与前台共用一个 Flask 后端、一个登录接口 `POST /api/v1/auth/login` 和同一套随机 Token + Redis Session；前台传 `channel=web`，管理后台传 `channel=management`。旧接口 `/v1/user/login` 保留兼容。

---

## 五、统一后端说明

- 数据库统一使用 Peewee。
- 管理端接口保持 `/api/v1/*` 路径不变。
- 知识库文档上传使用主 API 的 `/v1/file_upload/*` 分片直传，5MB 分片、MD5 秒传、断点续传。
- 管理端不再监听 `5000`，不再需要 `management/server/.venv`。

---

## 六、常见问题

### 端口被占用

```powershell
docker compose --env-file docker\.env -f docker\docker-compose.yml `
  stop app management-frontend
```

检查本地端口：

```powershell
Get-NetTCPConnection -State Listen |
  Where-Object LocalPort -in 8000,3333,9380 |
  Select-Object LocalAddress,LocalPort,OwningProcess
```

旧版残留容器可手动停止：

```powershell
docker stop cvte-rag-plus-management-backend
```

### 管理后台登录返回 503

Redis 不可用或管理员密码未加载：

```powershell
docker compose --env-file docker\.env -f docker\docker-compose-base.yml ps redis
```

### 管理后台返回 401 / 403

- `401`：Token 缺失、过期或已退出。
- `403`：用户没有目标资源权限。

### 修改了代码但不生效

Python 后端没有热重载：

```powershell
.\scripts\dev.ps1 -SkipInfra
```

Docker 模式需要重新构建：

```powershell
.\scripts\start.ps1 docker -Build
```
