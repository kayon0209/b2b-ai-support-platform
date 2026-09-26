# R1 修复轮交付说明（Codex 验收 B1-01 – B1-07）

日期：2026-09-26
分支：`codex/ai-support-v2-r1-fix`
基线：`96c81ad`（`origin/master`，已含验收报告所述的 11 个提交）
代码 head：`61668daa064d38dc2dd960b273d2d7917a8c8c1f`
差异：14 个提交，56 个文件，+13901 行

本文件逐项报告**实际**状态。Codex 的验收报告未被修改，也未被合入本分支。

> 当前状态以文末 §7「Codex follow-up 修复与复验」为准；§1–§6 保留 WorkBuddy 原交付时的记录。

## Codex 后续修复（2026-09-26）

以下为本交付报告编写后的代码修复与复验结果；下文原始交付说明保留 WorkBuddy 当时的状态记录。

| 发现 | 修复 |
|---|---|
| 影子分析、副驾请求只有 consumer 函数，没有运行入口；通用 relay 会认领未知事件 | `SemanticWorker` 已加入 interactive worker 生命周期，独立轮询影子、副驾与任务规划事件；默认 relay 排除这三类专属事件。消费者只在 owner 连接读取队列标识，之后在租户 RLS 会话读取 payload 与业务数据。 |
| 任务规划在 Inbox 事件内等待模型 | 改为事务 outbox 的后台任务；事件仅携带会话与轮次 ID，consumer 从租户 RLS 会话读取脱敏对话。工作台空列表会短时自动刷新。 |
| 坐席补录信息写成 `customer` 发言 | 改为 `origin=agent_collected`，保留坐席 actor 与采集时间；不再制造客户消息。敏感补录值继续不落库。 |
| `processing` 过期回收依据事件创建时间，老队列事件可能刚领取就被重复回收 | 新增 nullable `outbox_events.processing_started_at`，迁移号 `0064_outbox_claim_started`；过期恢复依据实际领取时间。迁移总数门禁更新为 63。 |
| R1 表未进入跨租户负例扫描；旅程 fixture 把工具定义写为全局行 | 四张新表加入跨租户扫描和 seed；旅程测试改用租户级工具定义并清理自身数据。 |
| 集成测试有硬编码本地 `platform` URL；迁移测试会 DROP 固定数据库名 | 集成测试连接统一改读 `APP_TEST_DATABASE_URL` / `APP_ADMIN_DATABASE_URL`；迁移测试数据库改为进程唯一名称。 |

### 本地验收结果

- Unit 与 contracts：**1573 passed**。
- PostgreSQL integration：**1076 passed, 2 skipped**；另加两个生产 worker 入口验收（任务规划与副驾各 1 项）均通过。
- Admin Web：TypeScript typecheck、production build 通过；UI 状态测试 **29 passed**。
- Ruff lint/format、Mypy（236 个源文件）和 Python compile 均通过。
- 本地集成测试使用独立数据库 `r1_codex_20260926`，全量完成后未写入项目开发库。GitHub Actions 尚待本次提交触发。

**仍未完成生产发布验收**：真实模型质量评测、真实浏览器双窗口与接管竞态、生产近似负载/队列积压测量、故障注入与回滚演练。所有新 feature flags 仍默认关闭；本地测试使用 stub 模型证明队列与数据路径，不代表真实模型质量或生产性能。

---

## 1. 阻断缺陷处理结果

| ID | 状态 | 处理 |
|---|---|---|
| B1-01 影子分类未调用模型 | **已修** | `c4edae9` |
| B1-02 任务规划无生产调用方 | **已修** | `c4edae9` + `61668da` |
| B1-03 副驾 job 创建后不可读 | **已修** | `624c139` |
| B1-04 补参丢弃输入 | **已修** | `0a96a3d` |
| B1-05 准备提案无 ToolProposal | **已修** | `c24e92f` |
| B1-06 content_hash 忽略 slot 值 | **已修** | `c4edae9` |
| B1-07 迁移计数门禁 | **已修** | `c4edae9` |

另修：任务面板的跨会话响应与跨任务输入状态（`fa13adc`）。

### 1.1 B1-01：影子分类

诊断成立。`wiring.py` 把 `LlmAnswerGenerator` 放在 `deps.generator`、把
`ChatProvider` 放在 `deps.extra["chat"]`；原接线把前者交给调用 `complete` 的
函数，因此每次分类都失败，而 `record_shadow` 仍报告 `recorded`。

- `worker/shadow_consumer.py` 从 `extra["chat"]` 取 provider，并拒绝没有
  `complete` 的对象。
- 无 provider 是**记为失败**，不是跳过。provider 检查是第一条语句，测试断言
  该分支只发出一条语句。
- 分类移出事件路径。原实现在 per-event session 内 `await` 模型调用后才返回，
  使"不延迟客户处理"的注释与接线不符；现在只在 run 的同一事务里写一条
  outbox 行，消费者用 SKIP LOCKED 认领、独立租户会话、2 秒 deadline、零重试。

### 1.2 B1-02：任务规划

`tasks/planning_seam.py` 是新增的接线点，双重门控 `agent.conversation_tasks`
与 `agent.semantic_assist`（任务来自建议；只开存储而无建议就无事可存）。
shadow 明确排除在可创建任务的决策之外——写行会让 SHD-01 的"业务状态相同"
在唯一会被检查的地方失效。

**端到端已验证**（`61668da`）：spec 原句产生三个任务，read 为 `ready`，两个
write 为 `needs_human` + `SEMANTIC_NO_WRITE_CAPABILITY`，缺参保留，地址不入
任务行、订单号入行，重放不新增、同键不同值抛 `TASK_IDENTITY_CONTENT_MISMATCH`，
另一租户不可见。

该测试还暴露了一个此前无人发现的问题：seam 只把 `FLAG_TASKS` 传给
`resolve_mode`，而后者只识别 shadow/assist/semantic_read，导致 mode 恒为 OFF、
所有已开启的租户被拒。已修。

### 1.3 B1-03：副驾 job

诊断的两点都成立，且第二点（按主键查 `job_id` 列）即使补上插入也不会自愈。

- `create_copilot_job` 在同一事务内先写 `copilot_drafts` 行与
  `copilot.generate_requested` outbox 事件，再返回。`queued` 必然意味着 job 存在。
- `read_copilot_job` 按 `(tenant_id, job_id)` 查询，即 URL 所指的那一列。
- 新增两项校验：源 turn 必须属于本会话（本会话之外的源 404）；排队超时在读取时
  报 `expired`。
- `worker/copilot_consumer.py` 消费该事件。它**不能发送**（AST 测试断言无任何
  outbound 标识符），**不覆盖人工编辑**（`edited_by_human` 先查），取
  `extra["chat"]` 而非 generator。

### 1.4 B1-04：补参

- 字段名必须是该任务正在等待的，否则整批 409 `TASK_FIELD_NOT_REQUESTED`。
- 先经 `chat_service.append_customer_turn` 写入会话（客户角色、同一脱敏器、
  真实作者与时间戳），再写 slot（`origin: customer_stated`、`confirmed: false`、
  指向刚写入的 `turn_id`）。
- 敏感字段值 withheld，但保留"已采集"的事实、来源与 turn。
- **两处写入都成功才变 ready**。turn 写失败则异常传播、状态不变——"没有持久
  证据"不得读作"已采集"。
- `store.transition` 新增 `slots` 参数并拒绝无 `origin` 的 slot。

### 1.5 B1-05：写提案

- `prepare_proposal` 现在真正调用 `ToolGateway.propose`（与提案路由同一入口），
  并把 `proposal_id` 写回任务。
- 无写能力 → `needs_human` + `SEMANTIC_NO_WRITE_CAPABILITY`，不进入永不兑现的
  确认状态。
- withheld 或 inferred 的 slot 值阻止提案——否则会把空字符串交给 gateway 并被接受。
- 幂等键 `task-{id}-r{revision}`，重放检查在路由层：`ToolGateway.propose` 总是
  插入，它没有重放语义。
- 被拒的提案记录原因码与工具名，不再静默。

写测试时发现并修掉两个会发布出去的 bug：`task.status is not TaskStatus.READY`
拿 `str` 与 `StrEnum` 比较，恒不相等，使该守卫对所有任务（含 ready）触发，
`prepare_proposal` 一直是空操作；幂等键此前只是装饰。

### 1.6 B1-06：content_hash

`SO-1` 与 `SO-2` 现在哈希不同。值以 SHA-256 摘要参与——单向，列内不是客户数据
副本；withheld 或 inferred 的值按"缺失"哈希，因为它们的身份在会话记录里。
原注释称"刻意排除值"针对的是**存储行**而非哈希，把它套用到哈希上正是该缺陷的
来源。

### 1.7 B1-07 与迁移重编号

`EXPECTED_MIGRATIONS` 61 → 62。

**迁移号从 0055 改为 0063**：master 已占用 0055–0062，原号会冲突。实测
`upgrade head` → `downgrade 0062` → `upgrade head` 全部成功，四表
`relrowsecurity` 与 `relforcerowsecurity` 从 `pg_class` 读回均为 true。

---

## 2. T00–T09 逐项状态

| 任务 | 状态 | 依据 |
|---|---|---|
| T00 基线与契约差异 | 完成 | `baseline-and-contract-delta.md`；三条假设被证伪并记录 |
| T01 语义契约与裁决 | 完成 | 44 项单测；CLASSIFY 路由接入生产调用点 |
| T02 影子模式 | 完成（SHD-01/OPS-01 部分） | 9 项集成测试实测零业务副作用；已移入持久队列 |
| T03 评测框架 | 完成（EVAL-02 阻塞） | 25 项单测；无真实模型质量数字 |
| T04 任务表与状态机 | 完成 | 29 单测 + 16 集成；迁移往返实测 |
| T05 工具候选与补参 | 完成 | 能力过滤 + 规划器 + 7 项提案集成测试 |
| T06 副驾 job | 完成 | 24 单测 + 10 集成 + 独立 consumer |
| T07 工作台 UI | **部分完成** | 面板与四条路由已交付；**UI-01/UI-02 未验证** |
| T08 端到端 | **部分完成** | 数据层旅程已验证；**浏览器双窗口、接管竞态、故障注入未做** |
| T09 文档与证据 | 部分完成 | 本文件 + manifest；**回滚演练未做，CI 未跑** |

计数：完成 6 / 部分完成 3 / 未开始 1。R2、R3 未实现，未标记完成。

---

## 3. 验收 ID 实际状态

**完成**（本分支可复现）：
SEM-01、SEM-02、SEM-03、SHD-01、TASK-01、TASK-02、TASK-03、TOOL-01、
TOOL-02、TOOL-03、COP-01、COP-02、SEC-01、SEC-02、SEC-04、EVAL-01、
MIG-01、DOC-01

**部分完成**：
OPS-01（过期/配额/重放有测试；**无 worker 重启演练**）
OPS-02（kill switch 有测试；**无回滚演练**）
UX-01（数据层验证；**无浏览器验证**）

**未执行**：
SEC-03、UI-01、UI-02、UX-02、UX-03、PERF-01、PERF-02、CI-01

**阻塞**：
EVAL-02（无真实模型凭据与预算）、DOC-02（依赖上面未执行项）

新功能开关全部默认 false；本次交接不启用任何一项。

---

## 4. 复现命令（全部在交付 head 上执行过）

```bash
# 单元 + 契约（干净检出，无 .env）
APP_ENVIRONMENT=test APP_ALLOW_BOOTSTRAP_TOKENS=true \
  .venv/bin/python -m pytest apps/api/tests/unit packages/contracts/tests \
  -m "not integration" -q
# 实际：1566 passed, 0 failed

# 集成（隔离库，先 alembic upgrade head）
APP_ENVIRONMENT=test APP_ALLOW_BOOTSTRAP_TOKENS=true \
APP_DATABASE_APP_URL="postgresql+psycopg://platform_app:platform_app@localhost:5435/r1_it" \
APP_ADMIN_DATABASE_URL="postgresql+psycopg://platform:platform@localhost:5435/r1_it" \
  .venv/bin/python -m pytest \
  apps/api/tests/integration/test_customer_journey_to_tasks.py \
  apps/api/tests/integration/test_collect_fields_persistence.py \
  apps/api/tests/integration/test_prepare_proposal_creates_proposal.py \
  apps/api/tests/integration/test_copilot_job_persistence.py \
  apps/api/tests/integration/test_task_routes_http.py \
  apps/api/tests/integration/test_conversation_tasks_rls.py \
  apps/api/tests/integration/test_shadow_no_side_effects.py \
  apps/api/tests/integration/test_schema_privileges.py -q
# 实际：91 passed

# 类型与风格
.venv/bin/ruff check apps packages scripts tests pytest_plugins_release   # 通过
.venv/bin/ruff format --check apps packages scripts tests pytest_plugins_release  # 507 文件
.venv/bin/mypy                                                            # 235 文件

# 前端
cd apps/admin-web && npm run typecheck && npm run build && npm test       # 29 passed
```

**说明一处与上轮报告的差异**：上一轮报告的"3 failed"来自开发库的数据漂移。
在干净检出、无 `.env` 的环境下这三个 pricing 测试不失败——这与验收报告测到的
1566/1384 口径一致，不是本轮修复的结果。

**未执行**：`kubectl kustomize`、完整 `pytest`（含 e2e 脚本）、GitHub Actions
全流程。CI-01 因此仍未通过。

---

## 5. 仍未关闭的风险

1. **无浏览器验证**。UI-01/UI-02/UX-02/UX-03 需要真实渲染、键盘走查、移动
   布局与双窗口旅程，本轮只有状态逻辑的可执行测试。
2. **无接管竞态与故障注入**。SEC-03、OPS-01 的 worker 重启、OPS-02 的回滚
   演练均未做。
3. **无性能实测**。PERF-01/02 需要生产近似负载。
4. **EVAL-02 阻塞**。无真实模型凭据，`semantic_read` 不可启用，ADR 仍为
   Proposed。
5. **既存缺陷**：`intent.RESTRICTED_TERMS` 仅英文，中文"改银行账号"不被识别为
   敏感请求。属规则基线，需独立评测；语义层不能修复，因为它不能覆盖规则。

---

## 6. 给重验的建议顺序

1. 在 `61668da` 上重跑 §4 的全部命令，核对数字。
2. 逐条复核 §1 的七个修复，重点是能复现原缺陷的测试：
   `test_shadow_consumer.py`、`test_customer_journey_to_tasks.py`、
   `test_collect_fields_persistence.py`、`test_prepare_proposal_creates_proposal.py`、
   `test_copilot_job_persistence.py`。
3. 迁移重编号（0055 → 0063）与 `EXPECTED_MIGRATIONS` 同步值得单独确认。
4. §5 的五项是本次交接**没有**关闭的，不是待优化。

---

## 7. Codex follow-up 修复与复验（2026-09-26）

本节为 WorkBuddy 原交付说明之后的 follow-up，覆盖本地代码复核发现的阻断：
代码修复提交：`2653c06`。

1. `SemanticWorker` 已由 interactive worker 启动，独立处理影子、副驾及任务规划 outbox；默认 relay 排除这些专属事件。owner 队列连接只选择事件标识，payload 与业务记录由租户 RLS session 读取。
2. 任务规划改为事务 outbox 异步请求，事件只携带会话/轮次 ID。模型输入从 tenant-bound 数据库读取已脱敏轮次，Inbox 不再等待规划模型。
3. 坐席补录写入 `agent_collected` 来源、actor 与时间戳，不再写成 customer turn；敏感字段值仍不持久化。
4. 新增 `outbox_events.processing_started_at` 与迁移 `0064_outbox_claim_started`，stale reclaim 按领取时刻计算。迁移数量门禁更新到 63。
5. 跨租户负例扫描纳入四张 R1 表；集成测试不再把工具目录定义写入全局行。
6. 全部集成测试数据库连接改为环境变量，迁移验收数据库按进程命名，避免操作共享开发库。

本地复验：unit + contracts **1573 passed**；完整 PostgreSQL integration **1076 passed、2 skipped**；任务规划 worker 与副驾 worker 各自的真实入口集成测试在全量跑测后新增并单独通过。Admin Web typecheck、production build 通过，前端测试 **29 passed**；Ruff、Mypy（236 个源文件）、Python compile 通过。

最初在本机执行 integration suite 时，代码中硬编码的连接串使部分 app-role 操作落到了共享 `platform` 开发库。已按本轮时间窗、只选 tenant 不存在的测试 ID，清理 40 个测试租户对应的 7111 行，并清理之后残留的 2 条测试 outbox 记录。修正连接配置后，完整 suite 在独立的 `r1_codex_20260926` 数据库通过；最终验证没有写入开发库。

GitHub Actions run [#36239136194](https://github.com/kayon0209/b2b-ai-support-platform/actions/runs/36239136194) 已在代码提交 `2653c06` 上全部通过，包含 Release Evidence。真实模型质量、浏览器双窗口/接管竞态、生产近似负载、故障注入与回滚演练仍未完成，所有新 feature flags 保持关闭；本节的本地 stub-model 验收不代表真实模型质量或生产性能。

本交接不包含自动合并或生产发布授权。
