# 部署与运维

当前架构：FastAPI API 与独立异步 Worker、自有网页客服和渠道适配器。API 镜像包含 React SPA，部署不依赖第三方客服内核。能力范围见 [能力矩阵](capabilities.md)，环境验证要求见 [生产接入条件](production-readiness.md)。

## 本地开发

`infra/compose/docker-compose.yml` 定义 PostgreSQL/pgvector、Redis、MinIO、Keycloak、API 和分队列 Worker。示例账号、口令与 `start-dev` 仅用于 local；Compose 不是生产配置。

```bash
cp .env.example .env
docker compose -f infra/compose/docker-compose.yml up -d --wait ai-postgres ai-redis
```

后端依赖、迁移、API 与 Vite 启动步骤见 [README](../README.md)。Vite 开发服务器默认在 5173，将 `/api` 代理到 8000 并剥离前缀。文件摄取需要对象存储，交互式 AI Worker 需要有效的 `APP_LLM_API_KEY`；未配置模型时不能把普通交互 Worker 启动成功作为预期。无需模型的路径可运行 outbox/retention 等对应 Worker。

完整 Compose 镜像构建：

```bash
docker compose -f infra/compose/docker-compose.yml build ai-api
```

按实际凭据、对象存储与迁移状态启动对应服务；不能把完整 Compose 首次启动等同于数据库已迁移或 AI 已配置。

## 前端交付与 API 地址

`infra/compose/api.Dockerfile` 使用 Node 构建前端，只把 `dist/` 复制到 Python 运行镜像。`platform_core.spa` 提供 `/assets` 和 `/`、`/support`、`/admin/*`、`/auth/*` 的 SPA fallback。运行时不需要 Node 或单独静态服务。没有有效 `dist/` 时，不挂载 SPA。

**构建期与运行期配置不同**：`VITE_OIDC_ISSUER`、`VITE_OIDC_CLIENT_ID`、`VITE_API_BASE_URL` 会编译进 JavaScript；给运行容器添加环境变量不能改变已有前端。当前 Dockerfile 使用普通 `npm run build`，没有声明这些值的 build ARG，也没有执行 `build:release`；发布流程应使用带获准构建配置的构建阶段/镜像方案，不能假定现有镜像已经通过发布身份门禁。

在受控构建环境验证前端配置：

```bash
cd apps/admin-web
# 由构建系统注入实际 VITE_OIDC_ISSUER、VITE_OIDC_CLIENT_ID 和 VITE_API_BASE_URL
npm ci
npm run build:release
```

- Vite dev 的默认 API base 为 `/api`；这条代理只存在于开发服务器。
- API 服务真实路由为 `/v1/*`，SPA 服务器不会自动剥离 `/api`。
- 当前 `SupportChat.tsx` 将客户 API 前缀固定为 `/api`；`VITE_API_BASE_URL` 只改变 operator 客户端，不能统一改变客户入口。
- 因此保留当前代码部署完整产品时，同源网关必须增加 `/api/*` 到 API 的剥离前缀规则；当前 Kubernetes Ingress 没有该重写。规则只改写 `/api`，应保留 `/`、`/support`、`/admin`、`/auth` 和 `/assets` 的 SPA/静态路径。
- Operator 可单独使用完整 HTTPS API origin，但客户 `/api` 仍需要同源代理；跨域 operator API 还需 CORS 和身份策略。origin 不加尾随 `/`，因为客户端直接拼接以 `/` 开头的路径。
- 不应设置 `VITE_API_BASE_URL=/`：当前拼接会形成 `//v1/...`，而且 `build:release` 仅接受 `/api` 或 HTTPS URL。
- IdP 注册实际 `${origin}/auth/callback` 和注销地址；API issuer/audience 与前端 client id 分别配置。

所有方案均需 HTTPS。配置 CSP、HSTS、X-Content-Type-Options、Referrer-Policy；公开 `.map` 文件数应为零。`/assets` 哈希产物可长缓存，`index.html` 不可长缓存。不要把开发 token 或密钥编译进前端。

## 身份、数据与模型

- `APP_ENVIRONMENT` 显式设为 local/test/staging/production。bootstrap token 仅允许 local/test。
- staging/production 使用真实 `APP_SECRET_KEY`、`APP_OIDC_ISSUER` 和 `APP_DATABASE_APP_URL`；应用角色非 owner、非 superuser、无 BYPASSRLS。迁移凭据独立保管。
- 请求 tenant 来自认证映射/签名访客凭证；API 与租户业务 Worker 通过 tenant-bound 事务执行 RLS。
- 队列元数据发现/领取使用专用 bookkeeping session；这不等于普通业务可以复用 owner 连接。
- production 拒绝 Demo ERP 和公开参考价；企业数据通过受权 connector/credential reference 接入。
- 模型数据目的地、语料、密钥、预算与开关由部署方明确授权；缺模型、工具或渠道时不能伪造成功。

## 连接池与限流

连接上限按每进程、每数据库 URL 计算，核算 API/Worker 副本、owner/app 双池、overflow 和 PostgreSQL `max_connections`。Compose 的池配置是本地示例，不是生产容量承诺。

`DATABASE_POOL_TIMEOUT` 默认 5 秒，应小于上游请求预算。池耗尽返回 503/`DATABASE_SATURATED`；其他数据库异常不应被混同为容量饱和。

- 默认租户 API 预算：600 次/分钟。
- Workbench 队列/详情读取预算：24,000 次/分钟；写入仍使用通用预算。
- 访客预算：60 次/分钟，与地址桶叠加。
- 根据席位、轮询、模型吞吐和数据库容量校准；全局背压不是租户公平排队。
- `APP_RATE_LIMIT_TRUSTED_PROXIES` 仅填写实际可信代理地址/CIDR；不能照抄示例集群网段。留空是不信任转发头的安全默认，但网关后面必须核验客户地址归因。

```bash
python scripts/report_trusted_proxies.py --namespace <namespace>
```

该工具报告地址和验证命令，不修改部署；无法连接集群时退出 2。

## 对象存储、扫描与恢复

使用私有 MinIO/S3 兼容桶、短时签名 URL 和企业批准的保留策略。平台有格式检查、扫描状态机和对象清理；内置格式扫描器不等于恶意软件扫描。配置真实扫描服务、处理历史文档后再启用 `APP_REQUIRE_SCANNED_DOCUMENTS`；不能仅凭 `clean` 字段宣称病毒扫描完成。

定期执行 `scripts/backup_restore_drill.py`。Kubernetes 提供计划备份和季度恢复演练 CronJob；对象桶、独立备份桶、凭据、保留与恢复结果必须实际核验。详见 [Kubernetes 指南](../infra/kubernetes/README.md)。

## 监控与健康

- `/healthz`：进程存活；`/readyz`：应用数据库就绪。
- Redis 不可用时限流有界降级，不作为数据库 readiness 条件。
- Connector 健康属于租户接口；对象存储用部署探针/监控检查。
- API/Worker metrics 应仅内网采集。当前示例 Ingress 的 `/` 前缀会覆盖 API `/metrics`，而示例 ConfigMap 启用它；API 的 8000 端口共享业务与指标。部署网关须显式拒绝公网 `/metrics`、`/docs`、`/redoc`、`/openapi.json`，不能把独立 metrics Service 或 NetworkPolicy 当作 URL 级限制。Worker 8001 有独立监控网络策略。
- `70-alerts.yaml`、`71-servicemonitor.yaml` 使用 Prometheus Operator CRD；集群先安装 Operator/CRD，再应用资源并确认告警到达接收方。
- 配置 `OTEL_EXPORTER_OTLP_ENDPOINT` 后追踪才外发；未配置时进程内缓冲不是集中追踪。

## 发布与回滚

按 [Kubernetes 指南](../infra/kubernetes/README.md) 完成配置和迁移，再 rollout 固定版本镜像；本仓库 CI 不是 CD。执行所选企业场景、RLS/授权负例、模型/连接器故障、容量及恢复验证。

网页回复读取已持久化时间线；网页-only 部署不需要 SMTP/微信传输。使用邮件/微信时应验证所选 provider 的发送与线程关联；外部 SMTP 还需允许实际端口，示例 NetworkPolicy 只放行外部 443。传输受理不表示客户已读，未知结果保持 UNKNOWN/人工对账。

按租户启用获准能力，记录业务与质量指标。回滚优先关闭新行为、恢复 Prompt/上一兼容构建；保留会话租约、业务回执和审计，不执行数据删除式回滚。
