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
- **连接池等待上限由 `DATABASE_POOL_TIMEOUT` 控制（默认 5 秒，两个角色共用）**，不设置则继承 SQLAlchemy 的 30 秒默认值，而那正是要避开的：30 秒长于任何合理的上游超时，池满时每个请求会挂满 30 秒才被拒绝，此时调用方早已在上游超时放弃——等待没换来任何答案，还占着槽位让本该成功的请求排队。池耗尽会返回 `503` + `DATABASE_SATURATED`（可重试），与真正的内部错误（`500` + `INTERNAL_ERROR`）区分开，因为前者会自行恢复、后者不会。**这个值应小于上游超时预算**；调大它不会提高吞吐，只会让饱和持续更久。
- 交互 API 默认每租户 600 次/分钟；工作台的队列/会话 GET 单独使用每租户 24,000 次/分钟预算，以覆盖 1000 个窗口每 5 秒轮询队列和当前会话。工作台写操作仍使用通用 600 次/分钟限制。部署需按席位数、轮询间隔和数据库容量显式校准，不得把该读预算扩展到全体 API。
- **入口网关的网段必须填入 `APP_RATE_LIMIT_TRUSTED_PROXIES`**，否则客户侧限流退化。应用按地址分桶时看到的是网关 Pod 的地址，于是整条客户面共用一个桶（实测：200 并发访客被拒 14.4%，Redis 里只有一个键）。填入网关控制器所在网段后，同一压测拒绝率降为 0%，桶按客户端地址与访客凭据分裂。留空是安全默认（不轻信任何转发头），但**部署到入口网关后面就必须显式填写**，取值以集群实际的 Ingress/负载均衡器网段为准，例如 `10.244.0.0/16,10.96.0.0/16`。
- 客户面另有每访客预算 `APP_RATE_LIMIT_VISITOR_REQUESTS`（默认 60 次/分钟），与地址桶叠加：地址桶挡来源滥用，访客桶挡单个客户在企业 NAT 后面耗尽所有人的额度。两者都通过才放行。
- **告警与链路追踪需要集群侧组件**：`infra/kubernetes/70-alerts.yaml`（`PrometheusRule`）与 `71-servicemonitor.yaml`（`ServiceMonitor`）是 Prometheus Operator 的 CRD，必须先在集群装好 Operator 及其 CRD，否则 `kubectl apply -k` 会以 `no matches for kind` 失败——这个失败是刻意的，它比「装了 Operator 却没有告警」更诚实，因为后者看起来和健康部署一模一样。填入 `OTEL_EXPORTER_OTLP_ENDPOINT` 后链路追踪才会真正上报；留空时 `observability_tracing` 降级为进程内环形缓冲，行为不变但什么都不外发。`OTEL_SERVICE_NAME` 已在各工作负载清单中按角色区分（API 与五个 worker 各自独立），否则采集端无法区分交互式回答与入库重试。
- MinIO/S3 私有桶、短时签名 URL、备份和恢复演练必须验证。
- **前端由 API 镜像自身提供**：`api.Dockerfile` 是多阶段构建，先用 Node 构建 `apps/admin-web`，只把 `dist/` 复制进运行镜像；`platform_core.spa` 在 `/assets` 提供带哈希名的静态文件，并把浏览器路由的路径（`/`、`/support/*`、`/admin/*`、`/auth/*`）回退到 `index.html`，因此深链刷新可用。不需要额外的静态托管组件、证书或跨域策略。若改用独立前端部署，必须同时移除该镜像层与 `APP_SPA_DIST`，并自行承担 SPA fallback。
- 没有前端构建产物时（例如只跑后端测试），API 不挂载任何东西，`/support` 保持与改动前一致的 401，启动日志会打印缺失目录路径——这是刻意的：一个没有构建产物的检出不该变成一个看起来正常、实则空白的页面。
- 前端通过 HTTPS 提供；设置 CSP、HSTS、X-Content-Type-Options、Referrer-Policy。生产构建关闭公开 sourcemap。`/assets` 下的文件名带内容哈希，可长缓存；`index.html` 不可长缓存。
- 配置 `VITE_OIDC_ISSUER`、`VITE_OIDC_CLIENT_ID` 和 IdP 注册的 `${origin}/auth/callback`。`npm run build:release` 会拒绝缺少 HTTPS issuer/client id 或嵌入 API token 的构建。

## 健康与发布

- API：`/healthz`；数据库、Redis、对象存储和每个 connector 分开显示健康状态。
- 发布前执行数据库迁移、RLS 权限校验、单租户验收和出站渠道小流量投递。
- Worker 必须证明至少一个客户渠道可投递；未配置渠道时应停止发送能力，不能记录虚假成功。
- 新工作台先在测试租户验证，无数据删除式回滚：禁用新入口或退回上一前端构建，保留已写租约和审计记录。

## 当前限制

本次本地 Docker 已运行 PostgreSQL、Redis、API 与 worker，并完成迁移和人工主流程验收。MinIO 未启动；真实 OIDC Provider、生产静态托管、ERP 与渠道凭据、跨租户安全测试和负载测试仍需部署方配置/验证。未完成这些门禁前不标记为生产已验收。
