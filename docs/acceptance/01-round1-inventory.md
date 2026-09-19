# Round 1 — 事实盘点（只读）

日期：2026-09-19。基线：HEAD `d44420e` + 未提交工作树（锁定见 `00-round0-lock.md`）。
规则：只陈述事实，每条附 file:line；未确定的写「未找到」；不评价好坏。

## 1. 路由

### 1.1 前端页面（apps/admin-web/src/main.tsx）

| 路径 | 页面 | 鉴权 |
|---|---|---|
| `/` | redirect → `/quality`（main.tsx:22） | 无路由守卫；数据请求 401 时由 TokenDialog 拦截（Layout.tsx:22,25-26） |
| `/quality` | QualityDashboard | 同上 |
| `/gaps` | GapQueue | 同上 |
| `/prompts` | PromptRelease | 同上 |
| `/flags` | FeatureFlags | 同上 |
| `/cases` | Cases | 同上 |
| `/members` | Members | 同上 |
| `/usage` | Usage | 同上 |
| `/branding` | Branding | 同上 |
| `*` | redirect → `/quality` | — |

无登录页；凭证 = Bearer token（localStorage `b2b_token`，lib/api.ts getToken/setToken）。

### 1.2 后端 API（78 条路径，快照 docs/acceptance/openapi-snapshot.json）

鉴权三层：① 免 bearer（中间件豁免或自身签名）② 需登录（任意已解析成员）③ 需特定 Action（RBAC，packages/policy/src/platform_policy/engine.py:84-159）。角色缩写：owner=tenant_owner、secadm=security_admin、supadm=support_admin、kmgm=knowledge_manager、agent=support_agent、viewer=support_viewer、aud=auditor、intsvc=integration_service。

**免 bearer（identity/middleware.py:31-81）**：

| 路径 | 认证方式 | 依据 |
|---|---|---|
| GET /healthz, /metrics, /openapi.json, /docs, /redoc | 无 | middleware.py:31-40（/metrics 另受 APP_METRICS_ENABLED 门控） |
| POST /v1/webhooks/chatwoot | HMAC 签名+时间戳 | middleware.py:44-46; support_bridge/router.py |
| POST /v1/webhooks/connectors/{connector_id} | 前缀豁免，连接器签名 | middleware.py:72-81 |
| POST /v1/identity/members/accept | 单次邀请 token（body） | middleware.py:48-56 |
| GET /v1/public/branding | 公开（Host 头解析租户） | middleware.py:59-63 |
| /v1/saml/* | SAML 断言签名 | middleware.py:74-75 |
| /scim/v2/* | SCIM 专用 bearer（自建租户） | middleware.py:76-78 |

**需登录（默认成员即可，无 Action 门槛）——未找到额外角色限制**：

| 路径 | 说明 |
|---|---|
| GET /v1/identity/members | identity/router.py:157-158（_auth_or_denied） |
| GET /v1/connectors | integrations/router.py:164 实为 CONNECTOR_READ → 见下表 |
| GET /v1/knowledge/aliases | knowledge/router.py:383-390 KNOWLEDGE_READ |

（实际逐条核查后，除下表所列外无「仅需登录」的业务端点；所有端点都有 Action 门槛或豁免身份。）

**需特定 Action（require_policy / _gate 位置 → 允许角色）**：

| 路径 | Action（位置） | 允许角色 |
|---|---|---|
| GET /v1/audit-events | AUDIT_READ（audit/router.py:86） | owner, secadm, aud |
| GET /v1/cases | CASE_READ（cases/router.py:189） | owner, secadm, supadm, agent, viewer, aud |
| POST /v1/cases | CASE_CREATE（cases/router.py:107） | owner, supadm, agent |
| GET /v1/cases/{id} | CASE_READ（cases/router.py:214） | 同 CASE_READ |
| POST /v1/cases/{id}/commands | CASE_UPDATE（cases/router.py:243） | owner, supadm, agent |
| POST /v1/conversations/{ref}/agent-runs | CASE_UPDATE（agent_runtime/router.py:65） | owner, supadm, agent |
| GET /v1/conversations/{ref}/agent-runs | CASE_READ（agent_runtime/router.py:218） | 同 CASE_READ |
| GET /v1/quality/metrics, /v1/quality/routes | AUDIT_READ（evaluation/router.py:117,142） | owner, secadm, aud |
| GET/POST /v1/prompts | PROMPT_READ（prompt_router.py:212,227） | owner, secadm, aud |
| POST /v1/prompts/{id}/candidate|promote|reject, /v1/prompts/rollback | PROMPT_RELEASE（prompt_router.py:244-331） | owner |
| GET /v1/flags, /{key}/evaluate, /{key}/preview | FLAG_READ（flag_router.py:131-172） | owner, secadm, aud |
| POST /v1/flags, /{key}/enabled, /{key}/rollout, /{key}/targets | FLAG_WRITE（flag_router.py:194-258） | owner |
| GET /v1/knowledge/spaces, /documents/{id}/versions, /versions/{id}, /versions/{id}/download-url, /gaps, /gaps/drafts, /gaps/stats | KNOWLEDGE_READ（knowledge/router.py:227-386; gap_router.py:174-218） | owner, secadm, supadm, kmgm, agent, viewer |
| POST /v1/knowledge/documents, /versions/{id}/ready, /aliases, /aliases/{a} DELETE | KNOWLEDGE_UPLOAD（knowledge/router.py:166,294,405,439） | owner, supadm, kmgm |
| POST /v1/knowledge/gaps/{id}/acknowledge|dismiss|drafts, /drafts/{id}/review|publish | KNOWLEDGE_PUBLISH（gap_router.py:232-336） | owner, kmgm |
| GET /v1/identity/accounts, /departments 等 | CASE_READ（org_router.py:167,215,275） | 同 CASE_READ |
| POST/PATCH accounts/departments | TENANT_ADMIN（org_router.py:182-323） | owner, secadm |
| POST /v1/identity/members/invite, POST/DELETE /v1/identity/members/{id} | TENANT_ADMIN（identity/router.py:147 经 _auth_or_denied，:158,172,433,512 调用） | owner, secadm |
| GET/POST /v1/identity/saml/connections, POST .../status | TENANT_ADMIN（saml_router.py:266-314） | owner, secadm |
| GET /v1/tenant/usage | （usage.py:190 前）成员可读；未找到 Action 门槛 | 任意成员 |
| PUT /v1/tenant/quota | TENANT_ADMIN（usage.py:207） | owner, secadm |
| GET /v1/tenant/billing | AUDIT_READ（usage.py:190） | owner, secadm, aud |
| POST /v1/tenant/billing/adjustments | BILLING_ADJUST（usage.py:261） | owner |
| GET/PUT /v1/tenant/branding | GET 未找到 Action（branding.py 公开读逻辑）；PUT TENANT_ADMIN（branding.py:105） | PUT: owner, secadm |
| POST /v1/tenant/compliance/export | COMPLIANCE_EXPORT（compliance/router.py:83） | owner, secadm, aud |
| GET/POST /v1/tenant/domains, DELETE /{id}, POST /{id}/verify | TENANT_ADMIN（domain_router.py:109-186） | owner, secadm |
| GET /v1/connectors, GET /v1/dead-letters | CONNECTOR_READ（integrations/router.py:164,497） | owner, secadm, supadm |
| PUT /{id}/credential-ref, POST /{id}/health-check|sync|reactivate, POST /v1/dead-letters/{id}/resolve|retry | CONNECTOR_ADMIN（integrations/router.py:195-600） | owner |
| POST /v1/retrieval/query | KNOWLEDGE_READ（retrieval/router.py:118） | 同 KNOWLEDGE_READ |
| POST /v1/tool-proposals | 动态（tool_gateway/router.py:231 required_action） | 依工具 risk：read→TOOL_READ，low→TOOL_WRITE_LOW，confirmed→TOOL_WRITE_CONFIRMED，human_approval→TOOL_HUMAN_APPROVAL |
| GET /v1/tool-proposals/{id} | CASE_READ（router.py:548） | 同 CASE_READ |
| POST /{id}/confirm | CASE_UPDATE（router.py:321） | owner, supadm, agent |
| POST /{id}/execute | 动态（router.py:443） | 同上动态 |

### 1.3 可交互元素（前端）

Layout：8 个 NavLink（Layout.tsx:39-48）+ Token 按钮（:62-64）。
TokenDialog：token 输入（:52-56）、Connect（:71，验证 GET /v1/tenant/usage → reload）、Cancel（:74）。
Prompt 组件（Prompt.tsx）：文本/下拉/整数字段 + Confirm（:140-149，required/integer/min/max 校验 :30-40）+ Cancel（:151-153）。
ui.tsx：ErrorBanner Retry（:22-24）、ActionFeedback Retry（:56-58）。

各页（handler → 副作用终点均为 API 调用 + 列表重载；api.ts 的 ApiError 为 Error 子类，lib/types.ts）：

| 页面 | 元素 → 端点 |
|---|---|
| QualityDashboard | 1h/24h/30d 分段按钮（:49-53，仅本地状态） |
| GapQueue | Claim→POST gaps/{id}/acknowledge（:153-159）；Dismiss→dismiss（:161-175）；Draft→drafts（:177-197）；Approve/Reject→drafts/{id}/review（:235-271）；Publish→drafts/{id}/publish（:273-306）；状态筛选 select（:106-109）；Gaps/Drafts tab（:82-91） |
| PromptRelease | Create draft→POST /v1/prompts（:105-115）；View/Hide（:159-163）；Candidate→{id}/candidate（:165-169）；Promote→{id}/promote（:171-183，confirm）；Reject→{id}/reject（:185-195）；Rollback→POST /v1/prompts/rollback（:221-237） |
| FeatureFlags | Define→POST /v1/flags（:53-68，key 必填 :58）；Enable/Disable→{key}/enabled（:113-117）；Set rollout→{key}/rollout（:119-155，integer 0-100） |
| Cases | 行点击选中（:95-102）；Record first response→commands（:175-176）；Transition/Change priority/Assign→commands（:177-220）；Close 详情（:148） |
| Members | 邀请表单 email+role（:108-121，submit :56 POST invite，按钮 :123 disabled 条件 email.trim）；行内 role select（:135 起）；Remove（confirm→apiDelete，:85-91）；Copy token（:200 附近） |
| Usage | 窗口编辑 Save/Cancel/StartEdit（:181-189，save→apiPut /v1/tenant/quota :140）；账本更正表单（:93 apiPost adjustments，submit :349）；Copy（:354）；403 时 billing 区渲染为权限说明（:361 附近） |
| Branding | 表单 display_name/logo_url/primary_color/support_email + Save（:126→apiPut :52）+ 实时预览 |

**代码侧假交互扫描**：全部 onClick 均可追到 API 调用/本地状态/导航三类终点；未发现空函数、仅 console.log、TODO 占位、onClick 绑在无 role/tabIndex 的 div 上的情况。运行态验证在 Round 2。

## 2. 外部依赖

| 依赖 | 用途 | 代码位置 | 超时/重试/熔断 |
|---|---|---|---|
| Chatwoot REST | send_message / fetch_message / list_messages | support_bridge/chatwoot_client.py:133-152 | 10s、3 次重试、断路器（5 失败/30s） |
| Gitee AI（OpenAI 兼容） | chat(qwen3.8-flash) / embed(Qwen3-Embedding-8B,1536) / rerank(bge-reranker-v2-m3) | llm/gitee_ai.py:44（共享断路器）、factory.py:23 | 30s、2 重试、共享断路器；rerank 单独 2s deadline（reranker.py:78-86，超时→degraded） |
| MinIO/S3 | 文档原件 put/下载预签名 | knowledge/storage.py:64-65（默认 minioadmin 凭据回退） | httpx timeout |
| PostgreSQL（ai-postgres,5435） | 全部业务数据，FORCE RLS | db.py | 异步 psycopg |
| PostgreSQL（chatwoot-postgres,5434） | Chatwoot 自有，平台禁止访问 | infra/compose/docker-compose.yml | 不适用 |
| Redis（ai-redis,6380） | 限流计数器；不可用→进程内 InMemoryRateLimiter（rate_limit.py:284-295） | config.py:26-28 注释确认队列不依赖 Redis | 降级继续 |
| Keycloak | OIDC issuer/JWKS | identity/middleware.py、oidc.py | JWKS 缓存 300s |
| 连接器（Jira/Linear/CRM/IM + business_api 通用读） | 适配器 | integrations/sdk.py、tool_gateway/registry.py:134（8 个工具定义种子） | resilience.py 断路器+退避（jitter 0.3） |

## 3. 环境变量与 fail-open 分析（config.py，env_prefix=APP_）

**缺了拒绝启动（fail-closed）**：
- APP_SECRET_KEY：staging/production 缺失→RuntimeError（config.py:224-225）
- 认证：APP_OIDC_ISSUER 与 APP_ALLOW_BOOTSTRAP_TOKENS 都缺→RuntimeError（config.py:230-231）；bootstrap tokens 在 local/test 之外开启→RuntimeError（config.py:221-222）
- APP_LLM_API_KEY：interactive worker 缺失→WorkerConfigurationError 拒绝启动（worker/wiring.py:62-68）
- APP_CHATWOOT_WEBHOOK_SECRET：缺失→webhook 一律 503 WEBHOOK_NOT_CONFIGURED（support_bridge/router.py:47-57）——fail-closed 但表现为「沉默」

**缺了降级运行（degraded，有记录）**：
- APP_CHATWOOT_API_TOKEN 缺失→worker 无 sender，弃权仍记审计但**发不出客户通知**，日志 worker_cannot_send（wiring.py:70-80）
- APP_REDIS_URL 不可达→内存限流器（rate_limit.py:284-295）
- rerank 超时→degraded 按融合序继续（reranker.py:78-86）
- Chatwoot 历史拉取失败→退化为单轮，trace 标记（worker/inbox_consumer.py:224-227）

**缺了静默变弱（fail-open 倾向，需 Round 4 复核）**：
- APP_HANDOFF_EVIDENCE_ENABLED 默认 False→转人工不带证据便签（orchestrator.py:1454）
- APP_MODEL_FALLBACK_ENABLED 默认 False→主模型故障直接 ModelError（generator.py:123）
- object_storage_access_key/secret 缺失→回退 minioadmin/minioadmin（storage.py:64-65）
- APP_METRICS_ENABLED 默认 None→非 local/test 下 /metrics 404（observability_router.py:50-58）——这是 fail-closed，反而安全

行为开关（flag，默认关）：citation_guard、query_normalization、metadata_filter、score_floor、authority_boost、priority_claim、model_fallback、handoff_evidence。

compose 注意点：`environment:` 覆盖 `env_file:`；`APP_CHATWOOT_API_TOKEN: ${VAR:-}` 的 `:-` 会把「未设置」变成空串而非缺失（compose 服务定义层面）。

## 4. 异步任务 / 状态机

**Worker 角色（apps/worker/src/worker/，入口 runner.py main():371，APP_WORKER_QUEUE 分派，未知值致命）**：

| 角色 | 队列/扫描 | 终态 |
|---|---|---|
| interactive（默认，与 outbox 同进程） | inbox_events，SKIP LOCKED 认领，600s 回收 stale PROCESSING | COMPLETED / FAILED |
| outbox | outbox_events 认领→handler（case.created/updated、usage.recorded），5 次后 park | sent / failed/parked |
| ingestion | document_versions，claim_ingestion_versions()（SECURITY DEFINER），900s 回收 | 见 ingest 状态机 |
| retention | 3600s 扫全部活跃租户，sweep_expired_data | 无状态 |
| sla | 60s 扫 open cases 超时→升级阶梯，幂等唯一键 | case_escalations + outbox |

**摄取状态机（knowledge/ingest.py:28-35）**：UPLOADED→PARSING→CHUNKING→EMBEDDING→INDEXING→READY；任意活跃态→FAILED→QUEUED_FOR_RETRY→PARSING；READY→SUPERSEDED|EXPIRED。

**Case 生命周期（cases/models.py:30-70）**：NEW/…/RESOLVED/CLOSED/REOPENED，TRANSITIONS 表约束非法迁移。

**控制租约（identity/lease_service.py:40,80,147）**：AI_ACTIVE →（人工接管）HUMAN_ACTIVE / QUEUED_FOR_HUMAN；发送前 CAS 复检。

## 5. 测试现状

- 命令：`pytest`（根目录，pyproject testpaths = apps/api/tests + packages/contracts/tests + tests/evals；addopts 载入 zero_tolerance 证据插件）
- 规模：98 个测试文件（unit 55 / integration 50 / evals 3，含目录数统计于下「命令」）
- integration 全部 `pytestmark = pytest.mark.integration`（需 Docker 服务）；eval 用例 23 条、11 类（tests/evals/dataset.py:38-51，docstring 仍写 twelve——已知注释缺陷）
- 迭代会话声明：1372 passed / ruff+mypy 全绿 / 迁移 0033-0036 循环通过 / tsc+build 通过（docs/iteration-delivery-report.md「验证」表）——**本验收在 Round 3 独立复核**
- 明确没覆盖：前端行为（门禁只有 tsc+build，overview.md:70-72 承认）；真实 Chatwoot 链路（tests/e2e/ 为独立脚本非 pytest）；并发正确性专项测试仅 test_orchestrator_lease_race.py

## 6. 我执行了哪些命令确认以上信息

```
python -c "...json.load('docs/acceptance/openapi-snapshot.json')"   # 78 条路径与方法
curl -s http://127.0.0.1:8000/openapi.json -o docs/acceptance/openapi-snapshot.json
sed -n '31,84p' apps/api/src/platform_core/identity/middleware.py    # 豁免表
grep -rn "require_policy(\|_gate(request" apps/api/src/platform_core --include="*.py"
sed -n '60,165p' packages/policy/src/platform_policy/engine.py       # RBAC 表
grep -n "onClick\|apiPost\|apiPut\|..." apps/admin-web/src/pages/*.tsx
sed -n '28,35p' apps/api/src/platform_core/knowledge/ingest.py       # 状态机
grep -n "class CaseStatus" -A 12 apps/api/src/platform_core/cases/models.py
grep -n "AI_ACTIVE\|HUMAN_ACTIVE\|QUEUED_FOR_HUMAN" apps/api/src/platform_core/identity/lease_service.py
find apps/api/tests packages/contracts/tests tests -name "test_*.py" | wc -l   # 98
```
