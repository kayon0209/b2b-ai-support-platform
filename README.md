# 企业级 AI 智能客服平台

**让 AI 回答有依据，让业务动作有边界，让人工接管有控制权。**

面向 B2B 企业支持场景的生产导向型平台：把客户渠道、知识检索、业务工具、坐席工作台、租户安全和质量评估放进一条可观测、可审计的服务闭环。

<p align="center">
  <img src="docs/blueprints/platform-overview.svg" alt="企业级 AI 客服平台服务链路：渠道接入、证据驱动的 Agent Runtime、客户回复、人工接管和受控工具" width="100%" />
</p>

> **定位说明**：仓库展示的是可运行、可测试、带生产控制边界的平台实现。真实企业身份源、ERP、消息渠道、对象存储、静态托管和生产容量仍需部署配置与验收；代码合并和 CI 通过不代表任意环境已经生产就绪。当前逐项状态见[工作台验收记录](docs/workbench-redesign-acceptance.md)。

## 它解决什么问题

企业客服不是“把问题发给模型，再把答案显示出来”。平台要能判断回答依赖什么证据、模型何时应该停止、业务工具能做什么、客户如何转人工、谁拥有当前会话，以及每次动作如何复核和追溯。

本项目以 **FastAPI 模块化单体 + 独立异步 Worker** 承载客服控制平面，自有客户入口与坐席工作台；核心业务数据归平台所有，外部系统经 API、签名 Webhook 和版本化事件连接。

图中顶部是接入和推理主链路；中部把 Agent Runtime 的输出拆成三条有明确权限边界的路径；底部是三条路径共用的数据、异步任务和治理基础。客户回复和坐席回复都会回到出站层，业务系统只通过受控工具适配器连接。

## 核心能力

### 1. 对话、工单和人工接管是一条连续工作流

- 客户从 `/` 进入企业客服入口，在 `/support` 发起访客会话；租户标识随企业链接传递。
- 坐席在 `/admin/workbench` 使用对话优先的收件箱：待认领、我的会话、等待客户；会话可以先于工单存在。
- `ConversationControlLease` 是 AI、队列、人工和结束状态的所有权依据。认领、转交、退回和结束携带版本号；并发认领由数据库事务串行化。
- 人工接管后，AI 发送路径会重新检查控制租约。工单生命周期与聊天结束状态分离，避免把“模型答过”误认为“业务问题已解决”。
- 客户消息时间线、业务回执卡片、人工回复、结束后的 CSAT 和质量指标通过同一会话标识关联。

### 2. AI 先找证据，再组织回答

- 知识摄取覆盖来源、文档版本、解析、切块、索引和访问控制。
- Retrieval 在生成前执行租户与知识 ACL 过滤，并组合关键词/向量检索与重排能力。
- Agent Runtime 负责意图与风险路由、上下文组装、引用校验、低置信度处理和转人工，不把所有对话塞进一个不可控 Prompt。
- 对知识不足、证据冲突或能力未配置的请求，运行路径可以澄清、拒答或交给人工；相似度分数不被当成事实置信度。
- 客服副驾建议可由坐席编辑；客户可见回答仍经服务端策略与会话控制检查。

### 3. 企业工具执行有明确的授权边界

Tool Gateway 把“模型提出动作”和“系统执行动作”分开：

1. 解析已注册工具与参数契约；
2. 校验当前 actor、租户、资源和策略；
3. 对需确认的动作保存提案并等待批准；
4. 用幂等键执行已授权操作；
5. 验证外部系统后置状态并记录审计事件。

模型不能绕过权限、确认、幂等和后置验证去执行高风险业务变更。真实 ERP、合同价目表等由租户数据源提供；生产配置拒绝把演示 ERP 和公开参考价冒充为企业真实数据。

### 4. 多租户隔离是数据层约束

- 租户从服务端认证成员、外部身份映射或已验证访客凭证解析，不信任客户端传入的 `tenant_id`。
- 租户业务表使用 PostgreSQL Row-Level Security；生产 API/Worker 连接专用非 owner、无 `BYPASSRLS` 的应用角色。
- 应用层查询仍显式带租户条件，作为数据库 RLS 之外的第二道防线。
- 客户原文在进入模型和日志边界前按策略最小化、脱敏；审计记录动作、对象、结果和关联标识，不记录凭据、附件正文或完整客户载荷。
- 外部 CRM、消息和工单系统通过 REST、签名 Webhook 和适配器连接，不跨系统读写数据库。

### 5. 运营控制、评估和可靠性进入产品路径

- 管理台包含会话工作台、客户与工单、知识管理、Prompt 发布、Feature Flag、连接器、审批、质量指标、实验、成员和品牌配置等页面。
- Inbox/Outbox 将业务提交与异步工作解耦；Worker 按队列处理交互运行、知识摄取、事件投递、SLA 与保留任务。
- 评估流程包含引用支持、答案质量、拒答与高风险动作样例；Release Evidence job 对发布证据做门禁检查。
- API 暴露健康与运行指标；日志和追踪遵循敏感数据最小化要求。

## 核心架构

| 边界 | 主要职责 | 代表模块 |
|---|---|---|
| Identity & Policy | Tenant、Membership、OIDC、RBAC/ABAC、服务端租户解析 | `identity/`, `packages/policy/` |
| Support Bridge & Channels | Webhook 验签、访客会话、渠道适配、消息最小化与连续性 | `support_bridge/`, `channels/` |
| Agent Runtime | 路由、上下文、生成、引用、拒答、人机控制租约 | `agent_runtime/` |
| Knowledge & Retrieval | 来源、版本、摄取、ACL、混合检索与重排 | `knowledge/`, `retrieval/` |
| Tool Gateway | 工具注册、授权、确认、幂等执行、后置验证 | `tool_gateway/` |
| Cases & SLA | 工单、分配、升级、SLA 时钟与附件证据 | `cases/` |
| Audit & Evaluation | 追加式审计、质量指标、实验、评估和发布证据 | `audit/`, `evaluation/` |
| Async Runtime | Inbox/Outbox 消费、交互、摄取、SLA 和保留 Worker | `apps/worker/` |
| Operator UI | 客户支持页、坐席工作台和运营控制台 | `apps/admin-web/` |

**技术栈**：Python 3.12、FastAPI、Pydantic、SQLAlchemy 2、Alembic、PostgreSQL 16/pgvector、Redis、React、TypeScript、Vite、Docker Compose。Kubernetes 清单用于表达生产部署形状；目前尚未在真实集群应用验证。

## 仓库结构

```text
apps/
  api/                  FastAPI 模块化控制平面与 Alembic 迁移
  worker/               Inbox/Outbox、知识摄取、SLA 与后台任务
  admin-web/            React 客户入口、坐席工作台和运营控制台
packages/
  contracts/            事件与外部契约
  policy/               共享授权词汇与策略接口
  observability/        日志、指标和追踪辅助组件
docs/
  adr/                  架构决策记录
  blueprints/           产品与架构视觉资料
infra/
  compose/              本地 PostgreSQL、Redis、API 与 Worker 定义
  kubernetes/           Kustomize 清单和部署说明
apps/api/tests/         单元、PostgreSQL 集成、迁移与性能测试
```

## 本地开发

### 依赖

- Python 3.12+
- Node.js 22
- Docker Compose
- PostgreSQL 16（本地 Compose 使用 pgvector 镜像）和 Redis 7

### 启动 API 与管理前端

```bash
cp .env.example .env
docker compose -f infra/compose/docker-compose.yml up -d --wait ai-postgres ai-redis

python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

APP_ENVIRONMENT=local \
APP_ALLOW_BOOTSTRAP_TOKENS=true \
APP_DATABASE_URL=postgresql+psycopg://platform:platform@localhost:5435/platform \
PYTHONPATH=apps/api/src \
alembic -c apps/api/migrations/alembic.ini upgrade head

PYTHONPATH=apps/api/src:packages/contracts/src:packages/policy/src:packages/observability/src \
uvicorn platform_core.main:app --reload --port 8000
```

另开一个终端启动管理前端：

```bash
cd apps/admin-web
npm ci
npm run dev
```

- 客户入口：`http://localhost:5173/`
- 客服窗口：`http://localhost:5173/support?tenant=admin-demo`
- 运营控制台：`http://localhost:5173/admin/quality`
- 坐席工作台：`http://localhost:5173/admin/workbench`
- API 健康检查：`http://localhost:8000/healthz`

`.env.example` 仅用于本地演示，含开发环境示例值；bootstrap token 仅允许 local/test。未配置外部模型密钥时，可验证确定性路由、失败处理与人工接管路径；模型生成、真实渠道投递、ERP 数据和附件对象存储需要各自的凭据与服务配置。请勿把本地 `.env` 或真实租户载荷提交到仓库。

## 质量与发布门禁

GitHub Actions 覆盖：Ruff、Mypy、密钥扫描、Python/npm 依赖审计、管理前端构建、单元测试、PostgreSQL/Redis 集成测试、并发写保护和 Release Evidence。

常用本地检查：

```bash
ruff check apps packages scripts tests pytest_plugins_release
ruff format --check apps packages scripts tests pytest_plugins_release
mypy
pytest apps/api/tests/unit packages/contracts/tests -m "not integration"
npm --prefix apps/admin-web run typecheck
npm --prefix apps/admin-web run build
kubectl kustomize infra/kubernetes
```

**生产验收状态以文档中的实测证据为准。** 完整浏览器 OIDC/注销、真实外部渠道投递、S3/MinIO 上传安全、生产 ERP/报价、HTTPS/CSP 静态托管、浏览器无障碍与生产近似负载仍需要部署级验证。当前本地 100 并发工作台队列 p95 为 753ms，高于 500ms 验收目标；在达到目标及完成剩余部署门禁前，不应把该仓库描述为已通过生产放量验收。

## 深入阅读

| 主题 | 文档 |
|---|---|
| 当前架构与系统边界 | [docs/architecture.md](docs/architecture.md) |
| Agent Runtime 与安全运行方式 | [docs/agent.md](docs/agent.md) |
| 多租户安全、身份与威胁模型 | [docs/security.md](docs/security.md) |
| API、事件和幂等契约 | [docs/api-contracts.md](docs/api-contracts.md) |
| 坐席工作台需求和信息架构 | [docs/workbench-redesign-spec.md](docs/workbench-redesign-spec.md) |
| UI/UX、安全与生产验收记录 | [docs/workbench-redesign-acceptance.md](docs/workbench-redesign-acceptance.md) |
| 产品缺口和剩余工作 | [docs/product-gap-analysis.md](docs/product-gap-analysis.md) |
| 部署、配置和运维限制 | [docs/deployment-and-operations.md](docs/deployment-and-operations.md) |
| 架构决策 | [docs/adr/](docs/adr/) |
