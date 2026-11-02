# Management authentication / 管理后台认证配置

管理后台与前台 API 共用同一个 Flask 后端和同一个登录接口 `POST /api/v1/auth/login`。前台传 `channel=web`、`password_encrypted=true`，管理后台传 `channel=management`、`password_encrypted=false`；不传 `channel` 时按管理后台处理。旧接口 `POST /v1/user/login` 保留并委托给同一套认证服务。

登录成功后签发随机不透明 Token，会话数据保存在 Redis；服务端只保存 Token 的 SHA-256 摘要，客户端无法从 Token 反推用户信息。Token 默认 8 小时过期，可通过 `AUTH_TOKEN_TTL_SECONDS` 调整。

管理后台只接受超级管理员和团队负责人登录。资源访问在每次请求时按当前数据库归属重新校验，不使用 Token 中缓存的旧租户信息。

## Required configuration

必须在部署私有的环境文件中设置至少 12 个字符、且不是默认值/占位值的管理员密码：

```dotenv
MANAGEMENT_ADMIN_PASSWORD=<unique password, at least 12 characters>
```

可选配置：

```dotenv
AUTH_TOKEN_TTL_SECONDS=28800
```

`MANAGEMENT_JWT_SECRET` 不再用于登录。保留旧值不会影响启动，但新部署可以删除。

## Local and Docker

- 本地开发：配置 `docker/.env`，然后运行 `.\scripts\dev.ps1`。
- Docker 全应用：Compose 的 `env_file: .env` 会把配置传给统一的 CVTE-RAG-Plus 容器。
- 不要再启动独立的 `management-backend` 或 `5000` 端口服务。
- 修改 `MANAGEMENT_ADMIN_PASSWORD` 不会让已有会话立即失效；删除 Redis 中对应的 `cvte-rag-plus:auth:session:*` 键，或轮换 Redis 数据，可强制所有用户重新登录。

## Session failure behavior

- Redis 不可用：登录返回 `503`。
- Token 缺失、过期或已退出：管理接口返回 `401`。
- 已登录但无资源权限：返回 `403`。
- 未登记的管理路由默认拒绝，新增路由必须显式加入放行规则。
