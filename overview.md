# b2b-ai-support-plan — 本轮开发报告

**提交**：`9d4a384` — `feat(integrations): make connector health and reauthorization real`
**验证基线**：新增 19 个单元测试通过；无 DB 子集 **243 passed, rc=0**；`ruff check` / `ruff format --check` / `mypy`（104 文件，0 错）全绿
**端到端验证**：**未能执行** —— Docker Desktop 全程未启动（见第四节）

---

## 一、先行核查：项目实际进度比预期靠前得多

上一轮的记忆摘要停在 Phase 0/1，但仓库 HEAD 已在 Phase 5。以 `docs/development-plan.md` 为准绳逐条回代码核对后（证据见
`.workbuddy-ai/artifacts/phase-progress-and-remaining-work.md`）：

| Phase | 结论 |
|---|---|
| 0 基础与尽职调查 | **DONE** — ADR 4/4、依赖扫描 CI、dependabot |
| 1 AI 回答与交接循环 | **DONE** — 签名 webhook+去重、摄取流水线、预过滤检索+引用、弃权/交接、发送前租约复检、观测性 |
| 2 企业身份/授权/Case/SLA | **DONE** — OIDC+JWKS、RBAC/ABAC、全表 FORCE RLS、知识 ACL、Case+SLA、审计 |
| 3 集成与工具 | **PARTIAL** — 尾部未闭合（本轮补上一块） |
| 4 质量与生产加固 | **PARTIAL** — 限流、备份演练、开关运行时消费缺失 |
| 5 产品化 | **PARTIAL** — SAML/SCIM、自定义域名路由、HA 模板缺失 |

核查方法沿用本仓库的既有审计手法：**对任何被依赖的能力，grep 它的写入方/调用方**——文档与
「直接调用该函数的测试」都不会暴露缺失的生产调用方。

## 二、本轮交付：Phase 3 连接器健康与重授权

Phase 3 的验收标准之一「OAuth 重新授权可见且可操作」此前**无法成立**，因为三个东西都「存在但从不被消费」：

| 缺陷 | 证据 |
|---|---|
| `health_check()` 零调用方 | 定义于 `sdk.py` / `crm.py` / `jira.py` / `im.py`，全仓 grep 调用方 = 0 |
| `NEEDS_REAUTH` / `DEGRADED` / `last_health_at` 从不被写 | 枚举与列已定义，无任何生产赋值 |
| 无轮换路径 | `credentials.py` 只解析 `env://`，没有任何代码重指引用 |

**后果**：租户的 Jira token 过期后，连接器仍是 `active`，每次工具调用以
`CONNECTOR_AUTH_EXPIRED` 失败，而平台里没有任何地方说明这件事。

### 关键设计决策（值得留存）

`health_check()` 走的是**无鉴权**的 `GET {base}/health`（`crm.py:98`，不带 Authorization 头），所以：

> **探测成功不能证明凭证有效。**

由此推出两条规则，已固化进 `integrations/health.py`：

1. **探测永远不会清除 `NEEDS_REAUTH`。** 若「可达」能解除它，一个已知被拒的凭证会被静默重新武装，
   而平台接下来对一个 `active` 连接器要做的事是**执行写操作**。
2. `NEEDS_REAUTH` 只能由**显式运维动作**清除，且该动作还必须证明凭证现在**可解析**
   （`can_clear_reauth` 同时要求可达 **且** `credential_is_present`）。第二个条件正是为了拦住
   「运维把引用指向了一个忘记设置的变量」——否则 API 会对一个仍无法鉴权的连接器报告 `active`。

另：`{"api_token": ""}` 视为**缺失**。它产生 `Authorization: Bearer `（空），这不是凭证，
却会让连接器看起来已配置而每次调用都 401。

### 落地内容

- `integrations/health.py` — 状态转移做成**纯函数**（`status_after_probe` /
  `status_after_auth_failure` / `can_clear_reauth`）+ 异步落库器。纯函数是刻意的：
  决定「连接器能否执行写操作」的规则因此**无需 Postgres 即可验证**。
- `tool_gateway/registry.AuthReportingExecutor` — 适配器是以**返回值**（dict）而非异常报告
  `CONNECTOR_AUTH_EXPIRED` 的，所以工具路径上没有任何环节能观察到它。包装器在 build 时套上
  （只有那里同时握有 `Connector` 行与 session），并原样委托 `verify_postcondition`。
- `integrations/router.py` — `GET /v1/connectors`、`POST .../health-check`、
  `POST .../reactivate`、`PUT .../credential-ref`。
- `EventType.CONNECTOR_NEEDS_REAUTH`（**仅在转入** NEEDS_REAUTH 时发出，持续失败的连接器只通知一次）。
- `Action.CONNECTOR_READ` / `CONNECTOR_ADMIN` —— 与工具词汇表**刻意分离**：
  「被允许调用 Jira 工具」不等于「被允许重新配置 Jira 连接」。`support_admin` 可读健康
  （工具坏掉的就是他们），轮换凭证仅 `tenant_owner`。

### 测试接缝的选择

仅在重授权**成功**路径上 monkeypatch `probe_connector`：为了回答 `/health` 而搭一个 HTTP 服务
是在测 `httpx`，不是测这条规则。不可达路径统一用 `http://127.0.0.1:9`（discard 端口），
连接立即被拒，避免每次运行都吃一次 DNS + connect 超时。

## 三、连带定位的一个环境缺陷（非本轮引入）

`apps/api/tests/unit/identity/test_tenant_context.py::test_unresolvable_token_is_401_not_a_synthetic_context`
被归在 `unit/` 下，却走真实的 `bootstrap_token_resolver` → 连 Postgres。
Docker 不在时它**挂在连接上**（不是失败，是挂住），把整个 `pytest apps/api/tests/unit` 拖死。
结果是「全量测试超时」看起来像代码回归，实际是环境缺失。

无 DB 时可通过的是 **243 个测试**（排除该用例后 `rc=0`）。该用例归类问题已记录，待 Docker 可用后
再决定是移到 `integration/` 还是改为可注入的解析器。

## 四、未完成的验证（显式声明）

Docker Desktop 全程未启动：`ai-postgres:5435`、`ai-redis:6380`、`minio:19000`、
`chatwoot:3000`、`keycloak:8081` 全部不可达，`docker ps` 无法连上 daemon。
本机 `5432` 上另有一个原生 PostgreSQL，但 `postgres` 与 `platform` 两个用户名都被拒，
凭证未知；把项目指向它会脱离文档化的 compose 环境，故未采用。

因此 **`apps/api/tests/integration/test_connector_health.py`（18 个测试）已写完但未运行**。
在这一文件通过之前，本轮切片**不得视为已验证**。

## 五、下一步（按既定顺序）

1. Docker 恢复后：跑本轮 18 个集成测试 → 全量回归 → 迁移链 → 真实 MinIO 端到端
2. Phase 3 收尾：死信生产者 + 管理端查询/重放、`SyncCursor` 写入与读取、连接器 webhook 摄取
3. Phase 4：入站限流中间件、备份/恢复演练脚本、特性开关运行时消费
4. Phase 5：自定义域名 Host→tenant 路由、SAML/SCIM、`infra/kubernetes/` HA 模板
5. 最终：整体验证与端到端（MinIO + Postgres + Redis + Chatwoot + Keycloak）
