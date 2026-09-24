# 部署与运维（当前自有渠道架构）

状态：当前部署约束；旧 Chatwoot 部署说明不再适用。见 ADR 0012 与 [发布验收](workbench-redesign-acceptance.md)。

## 本地 Compose

`infra/compose/docker-compose.yml` 定义平台 PostgreSQL、平台 Redis、MinIO、Keycloak、API 与分队列 worker。Chatwoot 不在服务图中。Admin Web 当前以 Vite 本地服务运行，仓库还没有生产静态站点 Deployment/Service，需要在发布目标中补齐。

## 必需配置

- `APP_ENVIRONMENT` 必须显式区分 local/test/staging/production。
- staging/production 配置 `APP_SECRET_KEY` 与 `APP_OIDC_ISSUER`；bootstrap token 只允许 local/test。
- staging/production 配置由密钥管理器注入的 `APP_DATABASE_APP_URL`，连接专用非 owner、无 `BYPASSRLS` 角色；迁移使用独立数据库凭据。Compose 本地通过 `infra/compose/postgres-init/01-platform-app-role.sql` 建立演示用角色和密码。
- production 禁用 demo ERP 和公开参考价目表；业务 API、价目表和渠道出站凭据必须由租户/部署方明确配置。
- API 使用平台数据库应用角色，数据库强制 RLS；worker 使用同一套租户上下文与 transactional outbox。
- 连接池上限由 `APP_DATABASE_POOL_SIZE`/`APP_DATABASE_MAX_OVERFLOW` 与 `APP_DATABASE_APP_POOL_SIZE`/`APP_DATABASE_APP_MAX_OVERFLOW` 分别控制 owner 和 RLS app 角色，且都是“每进程、每数据库 URL”的上限。Compose 本地 API 配置 owner 10/0、app 50/0，每个 worker 两种角色均为 2/0；五个 worker 全满时上限约 80 个连接，为 PostgreSQL 默认 100 个连接保留管理余量。生产需按副本数、API 的 owner/app 双池、worker 数和 `max_connections` 重新核算。
- 交互 API 默认每租户 600 次/分钟；工作台的队列/会话 GET 单独使用每租户 24,000 次/分钟预算，以覆盖 1000 个窗口每 5 秒轮询队列和当前会话。工作台写操作仍使用通用 600 次/分钟限制。部署需按席位数、轮询间隔和数据库容量显式校准，不得把该读预算扩展到全体 API。
- **入口网关的网段必须填入 `APP_RATE_LIMIT_TRUSTED_PROXIES`**，否则客户侧限流退化。应用按地址分桶时看到的是网关 Pod 的地址，于是整条客户面共用一个桶（实测：200 并发访客被拒 14.4%，Redis 里只有一个键）。填入网关控制器所在网段后，同一压测拒绝率降为 0%，桶按客户端地址与访客凭据分裂。留空是安全默认（不轻信任何转发头），但**部署到入口网关后面就必须显式填写**，取值以集群实际的 Ingress/负载均衡器网段为准，例如 `10.244.0.0/16,10.96.0.0/16`。
- 客户面另有每访客预算 `APP_RATE_LIMIT_VISITOR_REQUESTS`（默认 60 次/分钟），与地址桶叠加：地址桶挡来源滥用，访客桶挡单个客户在企业 NAT 后面耗尽所有人的额度。两者都通过才放行。
- MinIO/S3 私有桶、短时签名 URL、备份和恢复演练必须验证。
- 前端通过 HTTPS 提供；设置 CSP、HSTS、X-Content-Type-Options、Referrer-Policy 与路由 fallback。生产构建关闭公开 sourcemap。
- 配置 `VITE_OIDC_ISSUER`、`VITE_OIDC_CLIENT_ID` 和 IdP 注册的 `${origin}/auth/callback`。`npm run build:release` 会拒绝缺少 HTTPS issuer/client id 或嵌入 API token 的构建。

## 健康与发布

- API：`/healthz`；数据库、Redis、对象存储和每个 connector 分开显示健康状态。
- 发布前执行数据库迁移、RLS 权限校验、单租户验收和出站渠道小流量投递。
- Worker 必须证明至少一个客户渠道可投递；未配置渠道时应停止发送能力，不能记录虚假成功。
- 新工作台先在测试租户验证，无数据删除式回滚：禁用新入口或退回上一前端构建，保留已写租约和审计记录。

## 当前限制

本次本地 Docker 已运行 PostgreSQL、Redis、API 与 worker，并完成迁移和人工主流程验收。MinIO 未启动；真实 OIDC Provider、生产静态托管、ERP 与渠道凭据、跨租户安全测试和负载测试仍需部署方配置/验证。未完成这些门禁前不标记为生产已验收。
