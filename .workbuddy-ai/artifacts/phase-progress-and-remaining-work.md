# 进度核查与剩余待办（2026-09-18）

核查方式：以 `docs/development-plan.md` 的 Phase 0–5 为准绳，逐条回代码验证。
判定 DONE 的必要条件是**在生产调用路径上找到调用方**——不看文档、不看 docstring、不把测试调用当作已接线。
本仓库有明确的缺陷史：「值/能力存在但从不被消费」（共 6 例），故本轮把这一族作为重点猎捕对象。

## 一、已确认的仓库状态（实测）

| 事实 | 证据 |
|---|---|
| HEAD = `1abb036`，工作树干净 | `git status --short` 空 |
| 迁移 24 个（0001–0024） | `ls apps/api/migrations/versions/*.py \| wc -l` = 24 |
| `/metrics` 真实可用 | TestClient：`200`，`text/plain; version=0.0.4`，含 `platform_agent_runs_total` |
| `/healthz` 可用；`/nope`、`/v1/cases` 无 token 返回 401 | TestClient 实测（fail-closed 前置认证，符合设计） |
| 本机 5432 有原生 PostgreSQL，但无凭证 | psycopg 连接返回 `password authentication failed` |

**环境阻塞（已上报用户）**：Docker Desktop 未运行，`ai-postgres:5435` / `ai-redis:6380` /
`minio:19000` / `chatwoot:3000` / `keycloak:8081` 全部不可达。全量 pytest 只产出 `E`，
**0 条断言结果**。任何涉及 RLS、租户隔离、幂等、交接竞态的验证在 Docker 恢复前都无法执行。

## 二、Phase 逐项状态

### Phase 0 — 基础与尽职调查：**DONE**
ADR 4/4（`docs/adr/0001`–`0004`）、CI 六个 job（含 `dependency-scan`）、
`.github/dependabot.yml`、`requirements-dev.txt`（唯一依赖清单）、compose 可解析、
评估数据集含 `must_abstain` 类别。

### Phase 1 — AI 回答与交接循环：**DONE**
- 签名 webhook + 去重：`support_bridge/router.py` HMAC/timestamp/delivery 校验 + `uq_inbox_delivery`
- 摄取流水线：`worker/ingestion_consumer.py` + 迁移 `0017`–`0019`（含 `claim_ingestion_versions`）
- 预过滤检索 + 引用校验：`retrieval/hybrid.py` 候选前即绑定租户/ACL，`qa_path.validate_citations`
- 弃权与交接：`orchestrator._finish_abstain` + `release_to_queue`
- **发送前租约复检**：`orchestrator` 第 8 步 `assert_can_send(expected_lease_version=...)`
- 观测性：`observability_tracing.py` / `observability_metrics.py` / `observability_router.py` / `HttpMetricsMiddleware`

### Phase 2 — 企业身份、授权、Case 与 SLA：**DONE**
`EnterpriseAccount` / `Department` / `Membership`（迁移 `0001`、`0015`）、OIDC + JWKS
（`identity/oidc.py`）、RBAC + ABAC（`packages/policy`）、全表 `FORCE ROW LEVEL SECURITY`、
知识 ACL（`0006`）、Case 生命周期与 SLA（`0008`）、append-only 审计 + 管理端检索。

### Phase 3 — 企业集成与工具：**PARTIAL（尾部未闭合）**
已接线：连接器 SDK、`jira.create_issue`、`crm.update_account`（写 + 后置校验 + 幂等）、
`Idempotency-Key` 全局强制、熔断 + 有界重试、`credentials.py` 的 `env://` 解析。

**未闭合（均为「存在但不被消费」）**：

| 缺陷 | 证据 | 生产含义 |
|---|---|---|
| `DeadLetterItem` 从不被写入 | 全仓仅 `evaluation/pii.py` 的保留期 DELETE | 重试耗尽的工具调用**静默丢失**，无重放路径 |
| `health_check()` 零调用 | 定义于 `sdk.py:104`、`crm.py:98`、`jira.py:50`、`im.py:26`；grep 调用方 = 0 | 连接器故障不可见 |
| `ConnectorStatus.NEEDS_REAUTH` / `DEGRADED` / `last_health_at` 从不被赋值 | `integrations/models.py` 仅枚举定义 | **验收标准「OAuth 重新授权可见且可操作」= 未满足** |
| `SyncCursor` 零引用 | `integrations/models.py:45` 仅定义 | 增量同步位置不存在，重连即全量/重复 |
| 凭证无轮换路径 | `credentials.py` 仅 `env://`，无 rotate/refresh | 令牌过期后无法自愈 |

### Phase 4 — 质量、看板与生产加固：**PARTIAL**
已接线：评估 runner + 发布门禁（P0 回归阻断）、质量看板 API、知识缺口队列 + 四眼发布、
prompt/模型版本发布与回滚、PII 脱敏（模型边界前 + 日志前）、保留期清扫 worker、租户月配额。

**未闭合**：

| 缺陷 | 证据 | 生产含义 |
|---|---|---|
| 入站限流中间件缺失 | `main.py` 仅注册 `TenantContextMiddleware` + `HttpMetricsMiddleware`；无 slowapi/令牌桶 | 无请求速率防护（配额是「月运行次数」，不是限流） |
| 备份/恢复脚本缺失 | `scripts/` 仅 `seed_admin_demo.py`、`smoke_gitee_ai.py`；全仓无 `pg_dump`/`pg_restore`/PITR | 文档承诺的「季度恢复演练」无工具可执行 |
| 特性开关无运行时消费方 | `flag_service.evaluate` / `evaluate_many` 存在，但 `flag_service.py` / `flag_router.py` 之外**零调用** | 开关可定义、可审计、可预览，但**不改变任何行为**→ 灰度发布不可执行 |

### Phase 5 — 产品化：**PARTIAL**
已落地：租户品牌（`0022`）、成员自助邀请（`0020`/`0021` + admin-web 成员页）、
用量配额与计费事件（`0023` + `usage.recorded` 出站事件）、知识审批工作流。

**未落地**：
- **SAML / SCIM** — 零代码（仅 ADR 提及「pilot 之后」）
- **自定义域名路由** — `0022` 明确递延；`middleware.py` 按 OIDC/slug 解析租户，**无 Host→tenant 解析**
- **额外 IM 渠道** — `im.py` 适配器未接入任何发送/摄取路径
- **保留与合规（数据区域）** — 保留期 worker 已接线，合规策略字段未落实
- **HA 生产模板** — `infra/` 只有 `compose/`，**无 `infra/kubernetes/`**

### 已知且**有意保留**的缺口（非缺陷）
`ambiguous-refund-eligibility`：`tests/evals/dataset.py` 中标记 `must_abstain=True`，
注释写明「gap stays visible」。正确解法是**检索前做完账户身份解析**，而非正则匹配问题文本
（此前两次尝试均因 `"are"` 被误判而失败）。不得为了让评测变绿而放宽该用例。

## 三、剩余待办（按「不做会不会坏 / 是否阻塞他项 / 工作量」排序）

**必须做（安全性 / 验收标准未满足）**
1. 连接器健康轮询——让 `health_check()` 有调用方，写入 `last_health_at` / `DEGRADED`，发告警
2. 凭证轮换 + `NEEDS_REAUTH` 置位路径（Phase 3 验收标准直接未满足）
3. 死信生产者 + 管理端查询 + 重放（重试耗尽的调用当前静默丢失）
4. 入站限流中间件（排除 `/healthz`、`/metrics`，不得误伤租户隔离）

**应当做（框架已建但无消费方 / 运维能力缺失）**
5. 特性开关运行时消费（接进发布或路由路径，使灰度真正可执行）
6. 备份/恢复演练脚本 + 写回 `docs/deployment-and-operations.md`
7. `SyncCursor` 写入与读取（增量同步）
8. 连接器 webhook 摄取端点（provider 签名 + 复用去重）

**Phase 5 产品化（体量较大，分批）**
9. 自定义域名 Host→tenant 路由（含 DNS/TLS 说明）
10. SAML + SCIM
11. HA 生产模板 `infra/kubernetes/`（Deployment/StatefulSet + PDB + 迁移 Job）
12. 额外 IM 渠道接入生产发送路径

**最终**
13. 全量回归 + ruff/mypy 门禁 + 真实依赖端到端（MinIO + Postgres + Redis + Chatwoot + Keycloak）
14. 集中排查 bug / 风险 / 性能 / 安全 / UI-UX / 交互体验

## 四、本轮自检结果

| 检查 | 结果 |
|---|---|
| 工作树是否干净 | 干净，无未提交改动 |
| `/metrics` 端到端可用 | 通过（200 + Prometheus 文本 + 真实样本） |
| 全量 pytest | **无法执行**（Docker 未运行，仅 `E`） |
| ruff / mypy | 待 Docker 无关，可直接执行（见下轮） |
