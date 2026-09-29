# R1 修复轮交付说明（Codex 验收 B1-01 – B1-07）

日期：2026-09-26
分支：`codex/ai-support-v2-r1-fix`
基线：`96c81ad`（`origin/master`，已含验收报告所述的 11 个提交）
代码 head：`61668daa064d38dc2dd960b273d2d7917a8c8c1f`
差异：14 个提交，56 个文件，+13901 行

本文件逐项报告**实际**状态。Codex 的验收报告未被修改，也未被合入本分支。

> 当前状态以文末 §9「Codex follow-up：剩余 R1 实施与再验收」及 [验收表](acceptance.md) 为准；§1–§6 保留 WorkBuddy 原交付时的记录，§7–§8 保留先前 follow-up 记录。

## Codex continuation：R2/R3 本地增量（2026-09-27）

本节记录当前隔离 worktree 的新增结果，不改写上方历史交付。分支为
`codex/r2-r3-implementation`，基线 HEAD `8150a74e29b58a6f090916412482b8e51abefe5e`；
本轮改动尚未提交、推送或触发 GitHub CI。

### R2-02：标准流程会话任务入口

- 新增默认关闭的 `agent.standard_flow_instances` flag、API 和 Workbench 按钮。服务端绑定最新同租户 customer turn、当前人工 owner、lease version、模板 key/version；浏览器不能指定 tenant/actor/source turn。
- `standard_flow_start_requests` 保存哈希后的 idempotency key/request fingerprint，强制 tenant RLS 且应用角色仅 SELECT/INSERT。相同 key/相同请求回放同一 task；异请求冲突。task 创建写 append-only task event、AuditEvent、outbox 与低基数指标。
- 实例使用 `manual_flow` 状态，明确排除 `can_progress()`。坐席可记录 customer-source 字段、取消或转人工；verified-business-record/human-review 字段不能靠自由文本冒充已核验。`invoice_application` 只在已有 Case 关联能唯一解析账户时，由有 `tool.write.confirmed` 权限的支持管理员/租户负责人准备平台内部 `case.create` 提案；executor 在写入时再次核对会话账户关联，审批页确认/执行后 verified receipt 回写 task。撤回与 execute 通过 ToolProposal 行锁串行；不执行开票。其它模板继续人工接续。

### R2-03 与 R3 边界

- 已检查 `EvaluationRunner`、`scripts/run_eval.py` 和 `hybrid_search`。本轮补上 tenant/space fixed-set manifest loader、Ed25519 前后测 attestation、signed artifact RLS persistence、approve/publish 快照复核及候选激活路径；旧的 unsigned metrics/post-test 路径拒绝写入。部署 allowlist、公钥/worker 私钥和真实数据集尚未配置，因此没有真实候选质量分数，普通 tenant flag 仍默认关闭。原有语义 CLI 的临时 corpus 不作为知识发布集。契约细节见 [R2-03 知识发布契约](r2-03-knowledge-release-contract.md)。
- R3 继续使用 vendor-neutral canonical contract 与 fake adapter tests；本轮 canonical read schema 覆盖账户、订单、发票、工单、物流、商机、产品、库存、报价九个 authority domain。没有引入真实 ERP/MES/WMS/CRM provider；真实适配器仍需企业 authority、授权 sandbox 和归属证明。
- 2026-09-28 R3 contract continuation：新增九域 canonical read model；`OwnershipProof` 现在绑定 tenant、connector、authority binding/version、具体外部 record 和 freshness TTL；`read_verified_fact()` 强制校验后才返回事实。canonical schema 版本更新为 `1.1`。`packages/contracts/tests` 与 `apps/api/tests/unit/integrations` 共 **138 passed**，Ruff、format、Mypy 通过；未调用真实 ERP/CRM provider。

### 本地验证

- R2 task state/planner/template unit tests：56 passed。
- 定向 unit、API、RLS/cross-tenant、知识发布门禁、Tool Gateway 和 R3 canonical fake contract：**227 passed**，1 个 Starlette/httpx deprecation warning；包括标准流程 16 项集成验收（内部提案、角色限制、账户重核、撤回、确认执行状态回写）。此结果早于本节的 R2-03 硬阻断改动；新断言本轮未执行。
- 2026-09-28 candidate-aware runner 复验：更新后的 `test_knowledge_release_gate.py` 使用隔离 PostgreSQL、合成 baseline/candidate 文档、fake answerer 和 deterministic embedder，经真实 `ReleaseEvaluationWorker` outbox handler 运行并签名持久化 pre/post test；baseline expected-candidate recall `0.0`、candidate recall `1.0`，安全 fake post-run citation support `1.0`，unsafe fake post-run 被签名阻塞并触发回滚。前后测事件覆盖 metadata-only claim、tenant-RLS payload load 和 fencing-token completion。R2-03/worker/迁移定向套件共 24 passed。此为 SQL/retrieval、签名、快照与队列边界验证，不是获批固定语料或真实模型质量评测。
- 新建的隔离 PostgreSQL 数据库从 base 迁移到 `0068_standard_flow_instances`；完整 downgrade-to-base/re-upgrade、one-step rollback、全租户表 FORCE RLS 扫描：1 passed。`0068` 已计入 67 个 migration revisions。
- Ruff check/format 与本轮修改的 15 个 Python source 文件 Mypy 检查通过。
- Admin Web 临时镜像：typecheck、Vite production build、36 项 UI 测试和 runtime guards 通过。临时镜像输出用于验证，不是交付目录。
- 新 HEAD 的 GitHub CI 尚未执行；生产 browser/A11y、真实候选 evaluator、真实外部业务连接器和 staging 演练仍未完成。功能 flag 保持默认关闭。

### 本轮接续：候选暂存底座（2026-09-27）

- 增加仅限 `system` / `service` + `integration_service` 的 release-candidate staging API；要求幂等键、已批准草稿、活动知识空间和干净扫描结果。候选使用可重放 UUID、内容哈希和 `gap-candidate://` 地址，审计只记录标识与哈希，不记录草稿正文。
- ingestion 完成时保留候选的 `draft` 状态；普通检索仍只查 active 版本。候选不出现在文档/版本列表或下载 API；内部 overlay 仅允许同租户、同空间、ready/clean、专属地址的候选，并继续应用 principal ACL、扫描和有效期过滤。
- 当前工作未运行测试、未触发 CI；仅做静态 diff 检查。此路径还没有接 evaluator runner、授权固定数据集、candidate snapshot/provenance artifact 或发布激活衔接，前后测写入和 release hard block 保持生效，不能据此给候选评测或生产就绪结论。

### 本轮接续：可重放评测执行 seam（2026-09-27）

- 扩展 `EvaluationRunner` 接受调用方解析的 principal scope；新 `release_evaluator.py` 要求租户绑定和 repeatable-read/serializable 事务，在固定时间点以相同样本先测 active baseline、再测单个 staged candidate。
- runner 校验获批 dataset hash、稳定 sample id、expected source 和 principal scope；retrieval config hash 覆盖 aliases、candidate overlay、embedding/answerer 配置、retrieval 参数及 principal scope；snapshot hash 覆盖知识空间、版本、ACL 与 chunks。artifact 只保存 metrics 和结果摘要 hash，不保存原始问句、答案或片段。
- signed pre/post artifact 已持久化并在 approve/publish 前验签、核对 allowlist 和当前快照；普通签名配置为空时拒绝写入。尚无真实获批固定集、已配置 worker 私钥、公钥 allowlist或持久 job 触发器，所以没有生产候选分数或自动评测调用。人工 rollback 保留。

### 本轮接续：签名前后测和候选激活（2026-09-28）

- 增加严格 dataset manifest；语义 hash 覆盖 case 内容、expected sources、tenant/space 和 principal scope。获批对象配置要求 tenant 前缀 object key、hash、approval reference 和两位不同 reviewer ID；worker loader 读取对象后重新计算 hash。
- 新增 worker-only Ed25519 签名入口与 API 公钥验签，签名 artifact 绑定 paired runs、candidate、dataset approval reference、result digests 和 retrieval cutoff。签名验收后，应用在 RLS 表持久化 attestation；approval/publish 会再次验签、检查 allowlist 和知识/ACL/chunk 快照。发布激活已索引的候选；签名 post-test 会写通过或 rollback_required 事件。
- 旧 unsigned metrics 和 post-test API 永久拒绝。新增 `release_evaluator` 专用 outbox worker、严格事件契约、租户 RLS payload 读取、fencing token、stale claim 恢复及三次有界重试；publish 开启自动运行时，会和知识激活在同一事务写 post-test event。自动运行默认关闭，样本 ceiling 默认为 0；当前未配置真实 dataset、公钥、worker 私钥或费用预算，因此尚无真实模型评测，也没有自动 worker 调用。tenant gate 默认关闭。
- 验收：release signature、dataset hash、loader 单测与 signed API 集成旅程共 9 passed；Ruff check/format、Mypy（本轮涉及的 15 个 source modules）、worker/API import smoke 通过。Migration 0069 在隔离 PostgreSQL 完成 upgrade、降级到 0068、再 upgrade；两张 release evidence 表 FORCE RLS 均开启。GitHub CI 和 staging 未运行。

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
| T03 评测框架 | 部分完成（EVAL-02 阻塞） | 哈希绑定精确文本、对话历史、标签与来源；输出意图宏/微 F1、exact match、逐意图 TP/FP/FN、槽位 exact-match、缺参槽位 F1 与脱敏失败切片；固定语义集和真实保留集仍未完成 |
| T04 任务表与状态机 | 完成 | 29 单测 + 16 集成；迁移往返实测 |
| T05 工具候选与补参 | 完成 | 能力过滤 + 规划器 + 7 项提案集成测试 |
| T06 副驾 job | 完成 | 24 单测 + 10 集成 + 独立 consumer |
| T07 工作台 UI | **部分完成** | 面板与四条路由已交付；**UI-01/UI-02 未验证** |
| T08 端到端 | **部分完成** | 数据层旅程已验证；**浏览器双窗口、接管竞态、故障注入未做** |
| T09 文档与证据 | 部分完成 | 本文件 + manifest；head `ade1bf0` 的完整 CI 与 Release Evidence 已通过；**回滚演练未做** |

计数：完成 6 / 部分完成 4 / 未开始 0。R2、R3 未实现，未标记完成。

---

## 3. 验收 ID 实际状态

**完成**（本分支可复现）：
SEM-01、SEM-02、SHD-01、TASK-01、TASK-02、TOOL-01、TOOL-02、TOOL-03、
COP-01、COP-02、SEC-01、SEC-04、MIG-01、CI-01（run #36260478409）

**部分完成**：
SEM-03（fake provider 降级通过；**无真实 provider 故障注入**）
TASK-03（幂等/恢复控制通过；**无 Worker 重启演练**）
SEC-02（测试权限负例通过；**真实身份撤权未演练**）
SEC-03（代码 lease 校验通过；**无真实双坐席在途接管**）
OPS-01（过期/配额/重放有测试；**无 worker 重启演练**）
OPS-02（kill switch 有测试；**无回滚演练**）
UI-01/UI-02、UX-01/UX-02/UX-03（部分桌面/移动旅程通过；**完整截图、无障碍与双浏览器矩阵未完成**）
EVAL-01（评测框架通过；**600-case 固定集未冻结**）
DOC-01（文档可审；ADR 仍为 Proposed）
DOC-02（交付说明已更新；**缺回滚及生产证据**）

**未执行**：
PERF-01（生产近似负载）

**阻塞**：
EVAL-02（固定保留集未执行，且合成探测延迟超标）、PERF-02（无真实模型 p95/预算测量）

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

**未执行**：`kubectl kustomize`、完整本地 `pytest`（含 e2e 脚本）。GitHub Actions
在 head `ade1bf0` 的全量套件及 Release Evidence 已通过。

---

## 5. 仍未关闭的风险

1. **无浏览器验证**。UI-01/UI-02/UX-02/UX-03 需要真实渲染、键盘走查、移动
   布局与双窗口旅程，本轮只有状态逻辑的可执行测试。
2. **无接管竞态与故障注入**。SEC-03、OPS-01 的 worker 重启、OPS-02 的回滚
   演练均未做。
3. **无性能实测**。PERF-01/02 需要生产近似负载。
4. **EVAL-02 阻塞**。本地 Gitee 凭据已配置；`semantic-v2` 在 Qwen3.8-Flash
   与 Qwen3.5-Flash 的各一条合成 no-thinking 请求上通过 schema，但耗时分别为
   4,844ms 和 7,723ms，仍高于 2 秒目标。600 条固定保留集尚未冻结/执行，`semantic_read`
   不可启用，ADR 仍为 Proposed。
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

---

## 8. Codex follow-up：工作台副驾闭环与最终本地复验（2026-09-26）

### 本轮实现

1. **副驾工作台真实接线**：话术侧栏新增“建议回复 / 会话摘要”、明确生成入口、队列/生成/完成/失败/过期状态、来源消息跳转；结果可插入空草稿，或在已有草稿时由坐席明确选择追加/替换。生成不会自动发送。
2. **刷新与切换保护**：`sessionStorage` 只记每会话最近一个 job id；刷新后从租户 API 重新读取状态。请求、轮询与选中会话绑定，切换会话不把旧结果写入新草稿。切换队列项目时保留 `tab`/搜索参数。
3. **服务端版本来源**：工作台详情给出服务器计算的 `timeline_revision`。来源引用仅由工作台详情返回，访客 `/support` 时间线不返回这些内部指针。
4. **幂等与重新生成**：同一个 `Idempotency-Key` 绑定同一个 job；相同请求重放不重复排队；同键异请求返回 409。新 key 才是显式重新生成。重放先于开关/当前版本校验，网络超时后可安全取回已创建 job。
5. **租约/版本复核**：worker 在调用前检查 job 的 timeline 和 human lease，调用完成后再检查一次；生成期间来了新消息会将结果写成 `stale`，不能插入。回复 API 接受 job id 而不接受客户端来源列表，服务端重验 actor、conversation、timeline、lease 与 source turns；过期草稿拒绝发送。成功回复将 `copilot_job_id` 和来源引用保存到坐席 turn。
6. **可观测处理中状态**：轮询 API 根据 outbox 的已提交 claim 状态返回 `running`，不需要在外部模型调用前提交半成品业务事务。
7. **迁移**：新增 `0065_copilot_reply_provenance`，为坐席回复保存来源 job 和 turn refs；新列可空/默认空，旧应用仍能读写已有数据。迁移计数门禁更新到 64。
8. **隔离扫描**：跨租户测试改为仅统计带 `tenant_id` 的业务行；`tool_definitions.tenant_id IS NULL` 是平台全局参考目录，按架构允许跨租户读取，不再误报为租户泄漏。租户归属行仍必须对其他租户和空上下文不可见。
9. **自定义指令限制**：目前只允许受控默认提示词。非空 `instructions` 返回 `COPILOT_INSTRUCTIONS_UNAVAILABLE`，不落库、不进入模型请求；前端不提供该输入。自动审核拒绝了把坐席自定义指令扩展到模型 payload 的改动，因为尚未确认可将这类可能含敏感信息的文本发送到当前配置的模型服务。获得明确的数据目的地授权后，才能重新评审此项。

### 本地验收

- Unit + contracts：**1574 passed**。
- PostgreSQL integration：**1091 passed, 2 skipped**（1093 collected）；完整迁移、RLS 和新副驾/来源测试通过。
- Admin Web：TypeScript typecheck、production build 通过；UI 状态测试 **29 passed**。
- Ruff：本轮修改的 Python 文件通过；Mypy：**10 个受影响源文件通过**。
- Alembic：隔离数据库 `r1_codex_20260926` 应用到 head；integration migration gate 验证所有迁移可从 base 升级、单步回滚再升级，64 个迁移已注册。
- Computer Use 浏览器旅程使用**独立隔离租户、合成对话和本地 stub provider**：桌面三栏、移动副驾抽屉；生成摘要后可点来源回到原消息、插入到本地草稿但不发送；新客户 turn 到达后 job 变 stale、插入按钮消失、坐席草稿保留；切换两条“我的会话”记录后 URL 仍保留 `?tab=mine`，旧 job 不串入第二条会话。
- DOM 尺寸测量：1536×1024、1280×800、390×844 以及 768 CSS px（模拟 200% 缩放后的宽度）均未发现横向溢出；未完成真实浏览器 200% 缩放和完整键盘/屏幕阅读器验收。
- 本地浏览器/数据库 fixture 不包含真实客户内容；生成由 stub 明确完成，无真实模型调用。测试后已清理 UI fixture；隔离验收数据库仍保留，不连接项目开发库。

### T00–T09 与发布门禁当前结论

| 任务 | 当前状态 | 尚未关闭 |
|---|---|---|
| T00 | 完成（产物可审） | ADR 仍为 Proposed；影响客户路由的 `semantic_read` 前须正式接受 |
| T01 | 完成（控制逻辑） | 真实模型语义效果归 EVAL-02 |
| T02 | 完成（shadow 本地验证） | 真实模型成本/容量归 PERF 门禁；无 worker 重启演练 |
| T03 | 部分完成 | EVAL-02 被阻塞，未执行真实模型保留集 |
| T04 | 完成（迁移/RLS/状态机） | 多进程重启恢复场景仍需上线拓扑演练 |
| T05 | 完成（工具候选/提案边界） | 真实业务连接器沙箱缺席，不计外部业务成功 |
| T06 | 完成（worker/job/引用/人工发送） | 自定义指令受限；真实模型质量仍阻塞 |
| T07 | 部分完成（API、桌面/移动实测） | UI-01 截图矩阵、完整键盘/屏幕阅读器仍未全验 |
| T08 | 部分完成（合成端到端旅程） | 双坐席并发、provider/connector 故障注入和生产负载未做 |
| T09 | 部分完成 | CI-01 在 head `ade1bf0` 已通过；真实回滚演练和发布证据包整理仍未做 |

**仍阻塞生产放量**：EVAL-02、PERF-01/02、OPS-01 worker 重启、OPS-02 回滚、SEC-03 两坐席在途接管、完整 UI/UX 无障碍矩阵。所有生产租户新开关继续默认关闭，`semantic_read` 不启用。

**GitHub Actions**：[run #36260478409](https://github.com/kayon0209/b2b-ai-support-platform/actions/runs/36260478409) 在代码/评测 head `ade1bf0ade71578228e633d54d816582b70c0f5c` 全部通过，包含 Release Evidence。CI 通过不替代真实模型质量/生产容量/回滚门槛。PR #19 保持打开；本报告不授权合并或生产发布。

### 2026-09-27 Gitee 提供方诊断与后续修复

- 使用现有全模型 Token 资源包创建了项目专用通用令牌；本地 `.env` 权限为 `0600`，文件被忽略且未纳入 Git。令牌只允许从当前资源包扣费，关闭订阅、代金券、账户余额和新购资源包自动授权，并启用按 Token 计费。没有新购资源包。
- Docker `ai-api` 容器读取到 `https://api.moark.com/v1` 和新令牌，`/healthz` 返回正常。该容器镜像来自主工作区，不包含本分支的语义服务；此结果只证明本地提供方连通性，不是分支端到端验收。
- 合成最小聊天请求由 Qwen3.8-Flash 成功返回，耗时 4,259ms，报告 59 个输入和 30 个输出 Token；请求 `max_tokens=8`，但 usage 报告了 30 个输出 Token。另一次 `max_tokens=300` 的语义探测报告了 1,518 个输出 Token。当前不能把 `max_tokens` 当作已验证的计费上限，分类请求也未达到 2 秒目标。
- 语义探测使用合成订单文本和空能力集，没有真实客户数据或工具执行。旧提示词 `semantic-v1` 下，默认思考模式在 8 秒和 15 秒服务预算下超时；直接提供方调用耗时 56,448ms、报告 231 个输入和 1,518 个输出 Token，严格 schema 校验失败。关闭思考参数后两次探测分别耗时 9,954ms 和 5,728ms；最后一次安全错误详情为缺少 `primary_intent`。该结果促成了提示词收紧。
- 提示词现为 `semantic-v2`，明确列出必需字段、枚举、嵌套对象和证据偏移规则；超时/失败现在记录已耗时长。随后使用临时 no-thinking 请求对同一严格校验器做了各一条合成探测：Qwen3.8-Flash 4,844ms / 747 输入 / 192 输出，Qwen3.5-Flash 7,723ms / 752 输入 / 299 输出，均通过 schema；两者都没有满足 2 秒时限。临时 no-thinking 参数尚未进入生产适配器。受影响单测 **74 passed**，Ruff 与 Mypy 通过；已停止 live 模型探测，固定保留集与 PERF p95 仍未测。`semantic_read` 和其他生产开关继续关闭。

### 2026-09-27 评测报告补全

- T03 的离线比较报告现在支持槽位 exact-match、缺参槽位逐类/宏微 F1、失败样例只记录槽位名而不记录槽位值；数据集哈希包含精确输入文本、授权历史、预期标签、槽位和 provenance，确保输入或标注变化都会改变 digest。
- 修复评测测试文件中既有的重复 `_family_in` 定义，使本次变更的 Mypy 检查通过。评测模块单测 29 项通过，Ruff、格式检查、Mypy 和 `git diff --check` 通过。
- 这只是指标与报告能力，不是模型测评结果：600 条独立且复核的固定语义集尚未冻结，未执行真实模型 holdout、p95 或负载试验；没有新增线上模型调用。生产开关保持关闭。head `ade1bf0` 的 GitHub Actions 全部通过，包含 Release Evidence。

---

## 9. Codex follow-up：剩余 R1 实施与再验收（2026-09-27）

### 实施补全

1. **隐私字段别名防护**：`classify_field` 统一识别 camelCase、地址别名、银行卡/证件/凭据字段和中文姓名/地址标签。会话任务规划及坐席补录使用同一裁定器，只保留字段名、来源、actor 和补录时间，不把地址或受限字段值复制进任务记录。Tool Gateway 的脱敏递归覆盖嵌套对象与数组。
2. **中文受限意图**：规则分类器现将银行账号、银行卡号、信用卡号、密码、凭证和 API 密钥请求归为 `sensitive_request`；模型输出不能覆盖此规则。原先“把银行账号改一下”落入普通写操作的缺口已由单测覆盖。
3. **语义语料补平衡**：当前 manifest 固定 `semantic-v2-r1-balanced-highrisk-synthetic-2026-09-27`，1,700 条、150 个 phrase families，按 family 分为 1,020/340/340。八类主意图各至少 24 条 holdout；额外 500 条安全边界样本中，holdout 有 100 条，跨租户、索赔、提示注入、虚假审批声明和受限数据各至少 20 条。哈希：`200cb1fa0a42da8c59083cbb150e8303b4eb4f379420dbed1e9029a532c13608`。
4. **标注规范**：新增 `semantic-evaluation.md`，定义意图/场景/产品线/槽位/证据/能力白名单的标注边界，列出双人独立复核与裁决流程、holdout 解封条件和报告隐私规则。样本仍是模板编写的合成数据；第二位领域标注人、裁决和签字未完成，生产质量门禁继续阻塞。
5. **Prompt 版本绑定**：此前 runner 报告写 `semantic-v7`，但发送给模型的 `SCHEMA_VERSION` 当时是 `semantic-v6`。上下文与评估报告现都从同一个 `SYSTEM_PROMPT_VERSION` 读取；完整性测试确认 manifest 绑定 `semantic-v7`。修复前的模型数据仅保留作历史排障材料，不作为当前 v7 质量证据。本轮未追加真实模型调用。
6. **任务恢复边界**：新增集成测试把 outbox 事件保留为过期 `processing` claim，由全新 `SemanticWorker` 实例恢复并创建唯一任务集；第二个新实例不重复建任务。它验证持久状态恢复，尚不是 OS 级 kill/restart 或多主机生产演练。
7. **工作台无障碍**：右侧副驾 tabs 加入 roving focus、左右方向键、Home/End 和关联的 tabpanel ARIA；转接/结束会话复用共享 Dialog 的焦点管理、Escape 和恢复焦点。键盘导航测试、现有 Dialog 焦点测试及本地合成会话浏览器走查均通过。完整屏幕阅读器走查和真实 200% 缩放仍待做。
8. **上版兼容回滚 smoke**：用 `master` 应用（基线 `96c81ad`）连接隔离验收数据库（含迁移 `0065`），读取会话队列，并对一条合成会话完成“领取→释放”；两个 API 操作均返回 200。该结果验证上版应用在扩展 schema 上可读写，不等于 staging/生产回滚或数据库降级演练。

### 复验与任务状态

本轮浏览器检查还确认副驾 tab 的方向键、Home 和 End 导航更新焦点及选中项；移动宽度下会话内容可加载。尺寸矩阵沿用上一节的 1536/1280/768/390 CSS px DOM 测量结论。测试数据库为 `r1_ui_accept_20260927`，仅含合成数据。

本地复验：完整 `pytest` **2782 passed, 2 skipped**（73.60 秒）；Ruff 检查和 562 个文件格式检查通过；Mypy **236 个源文件通过**；`kubectl kustomize infra/kubernetes` 通过。Admin Web `npm test`、typecheck、production build 均通过，tab 键盘导航新增 7 项检查。Python 输出有 12 条非失败警告（Starlette/httpx 弃用、SQLAlchemy DISTINCT ON 弃用、既有 OpenAPI operation ID 重复）；未改变门禁或跳过失败项。

| 任务 | 当前状态 | 尚需外部条件或后续验收 |
|---|---|---|
| T00 | 完成（差异、契约、脱敏和标注文件可审） | ADR 仍为 Proposed；语义路由变更前须正式接受 |
| T01 | 完成（控制逻辑与中文安全规则） | 当前版本真实模型质量归 EVAL-02 |
| T02 | 完成（shadow/off 代码边界） | 多主机 worker、真实 provider 故障和成本容量演练 |
| T03 | 部分完成（指标、hash、1,700 条语料、运行器、标注规范） | 双人独立标注与裁决；当前 prompt 的模型 holdout 和延迟门禁 |
| T04 | 完成（RLS、状态机、幂等、本地进程恢复实现） | 多主机生产消费者竞争与外部写入对账 |
| T05 | 完成（能力过滤、提案/确认/回执边界） | 真实 ERP/CRM connector sandbox |
| T06 | 完成（异步副驾、来源、stale/idempotency/人工发送） | 真实模型质量与租户允许自定义指令的数据目的地决策 |
| T07 | 部分完成（真实 API 页面、响应式与键盘修复） | 真实 200% 浏览器缩放、逐规格截图、完整屏幕阅读器验收 |
| T08 | 部分完成（合成用户旅程、lease/worker 竞态测试） | 两坐席在途接管、provider/connector 故障注入、生产负载 |
| T09 | 部分完成（本地上版兼容 smoke、交付和标注文档；GitHub CI/Release Evidence 已通过） | staging 回滚与生产证据 |

**生产启用仍阻塞**：EVAL-02、PERF-01/02、双坐席真实接管、多主机 Worker/外部写入对账故障演练、生产身份/连接器、独立人工标注及完整无障碍验收。所有新功能开关保持关闭，`semantic_read` 不启用。本轮不执行合并或生产发布。

GitHub Actions [run #36295891831](https://github.com/kayon0209/b2b-ai-support-platform/actions/runs/36295891831) 在实现提交 `247290e5df86c529dde17465efaab64765439764` 上通过所有工作流，包括完整 Integration、Release Evidence、Web、typecheck、lint、secret/dependency scan 和 concurrency guard。对应的本地验收摘要见 [实现提交证据包](evidence/247290e5df86c529dde17465efaab64765439764/local-verification.md)。

**Jev 使用记录**：Jev 只对脱敏的合成模型汇总数据给出“抽取 12 条 dev-only 样本诊断 schema 失败、保持 holdout 封存”的建议；未发送密钥或样本原文。Codex 完成评测设计、代码、复验和门禁结论。

---

## 10. Codex follow-up：生成中接管并发验收与当前分支 CI（2026-09-28）

### 实施与本地复验

- 加强 `test_orchestrator_lease_race.py::test_human_takeover_mid_generation_blocks_outbound_send`：AI 在真实 orchestrator 流程中进入受控生成屏障后，另一独立 `platform_app` 数据库会话执行并提交人工接管，再恢复生成。断言生成只调用一次、transport 零发送、run 变为 `handed_off` 且 `output_hash` 为空。这是 PostgreSQL 应用角色/RLS 边界上的并发测试，不使用真实模型或真实渠道。
- 新建隔离 PostgreSQL 数据库并升级至迁移 head `0070_outbox_processing_fence`。目标集成文件 **7 passed**；Ruff 检查、格式和 `git diff --check` 通过。为避免 focused pytest 运行尝试在只读 managed-worktree 路径写部分 release-evidence 文件，局部复跑覆盖了 `addopts` 中的证据收集插件；所有测试本身执行通过。完整 release-evidence 插件及零容忍检查由下述 CI 全套验证。
- 本地验收数据库无活跃连接后已删除，未操作共享 `platform` 数据库。

### 当前分支证据

代码提交 `25e6c2178c4f8f337fc896dcd94ebf52551aaa96` 已推送到 `codex/r2-r3-implementation`。PR #27 的 GitHub Actions [run #36378299874](https://github.com/kayon0209/b2b-ai-support-platform/actions/runs/36378299874) 九个 job 全部通过：unit、integration、完整 release evidence/零容忍检查、admin web、typecheck、lint、dependency scan、secret scan、concurrency guard。

| 任务 | 当前状态 | 尚需外部条件或后续验收 |
|---|---|---|
| T08 | 部分完成（真实 PostgreSQL 两独立应用会话验证生成期间人工接管可阻止 AI 外发） | 不同坐席身份与浏览器双窗口旅程、真实 provider/connector 故障注入、生产负载 |
| T09 | 部分完成（PR #27 实现提交及完整 CI/Release Evidence 通过） | staging 回滚与降级演练、真实发布证据包 |

此结果只关闭了一个本地代码级接管竞态缺口。真实 OIDC 撤权、屏幕阅读器/200% 浏览器验收、外部系统故障对账、生产近似性能与评测门槛仍未完成；feature flags 继续默认关闭。本轮未合并或部署 PR #27。

**Jev 使用记录**：本轮未使用 Jev；Codex 完成并发测试设计、实现、测试与 CI 核验。

---

## 11. Codex follow-up：坐席队列标签的键盘可访问性（2026-09-28）

### 实施与本地复验

- 会话队列现在把 `queue / mine / waiting` 的同一组 tab 契约用于 URL 状态、渲染和键盘导航。添加选中项 roving `tabIndex`、`aria-controls`/`aria-labelledby`、`tabpanel`，并实现左右方向键循环、Home/End；方向键切换同步焦点、选中项与 `?tab=`。未选中的标签不再占用额外 Tab 停靠点。
- `workbench-tabs.test.mts` 增加 6 个队列标签规则断言，现共 13 项导航断言。Admin Web 测试、typecheck 与 production build 通过。
- 使用当前分支前端和 API、隔离 PostgreSQL、随机生成的本地合成管理员身份核对认证态工作台。Chrome 视口 1280×800、640×400（200% 等效 CSS 宽度）、390×844 和 320×640 均测得 `documentElement.scrollWidth == clientWidth`；640 和 390 下查看了实际渲染。另通过 Chrome 显示菜单将此 localhost 页实际缩放到 200%，Chrome 报告 CSS viewport/document 宽度均为 756px、无横向溢出；复验后缩放已重置为 100%。浏览器 AX 树显示 tab/panel 关系。ArrowRight、Home、End 更新 tab、焦点和 URL；Tab 从选中 tab 到队列搜索，跳过未选中 tab。未接入真实坐席/会话、模型或业务连接器。
- 同一认证态空会话页完整 Tab 遍历记录 13 个可聚焦控件，逐一读取标签/placeholder/文本后没有发现缺少可访问名称的控件。该次没有队列会话数据，非空会话、菜单/对话框展开后的完整键盘路径和 VoiceOver 朗读仍未验收。
- 隔离库升级至 `0070_outbox_processing_fence`，本地 API 和 Vite 仅绑定回环地址。验收后已停止服务、关闭浏览器标签、删除临时库与临时身份/令牌文件；未访问共享开发库。

### 当前分支证据

实现提交 `4717deab72865b423269db7d41f327d4b3ce57ef` 已推送到 `codex/r2-r3-implementation`。PR #27 的 GitHub Actions [run #36382604120](https://github.com/kayon0209/b2b-ai-support-platform/actions/runs/36382604120) 九个 job 全部通过，含 integration、完整 release evidence/零容忍检查、admin web、typecheck、lint、依赖与密钥扫描和 concurrency guard。

| 验收项 | 当前状态 | 尚需后续验收 |
|---|---|---|
| UI-02 | 部分完成（空态 Tab 遍历 13 个控件名称齐全；队列与副驾 tab 键盘/AX 关系、对话框焦点和 Escape 已核对） | 非空会话/弹层全路径键盘走查、VoiceOver 屏幕阅读器验收 |
| UI-01 | 部分完成（Chrome 实际 200% 缩放及 1280/640/390/320 CSS px 均无横向溢出） | 1536/1280/390 逐规格截图归档 |
| T07 | 部分完成（认证态工作台队列导航已补齐） | 完整辅助技术和缩放验收 |

真实 Chrome 200% 缩放已通过无横向溢出核查；视口覆盖补充了窄屏重排验证。逐规格截图仍未归档。生产发布仍需完成 VoiceOver、非空坐席旅程、真实评测集、性能、OIDC/外部连接器和 staging 回滚门槛；本轮未合并或部署 PR #27。

**Jev 使用记录**：本轮未使用 Jev；Codex 完成可访问性缺口核查、实现、浏览器复验与 CI 核验。

---

## 12. Codex follow-up：R1 基线 API 在扩展 schema 上的读写兼容 smoke（2026-09-28）

- 在隔离 PostgreSQL 新建干净库并用当前 R2/R3 migrations 升级到 `0070_outbox_processing_fence`；用随机合成 tenant、support agent、agent profile 和 queued conversation 准备仅供测试的数据。
- 从 R1 基线 checkout `dc1c0a8` 启动旧 API（只监听 `127.0.0.1:8002`），使用本地合成 token 验证 `GET /v1/tenant/usage`、`GET /v1/agents`、workbench queue read 均返回 200；随后 claim 与 release 两次 mutation 均返回 200，并确认对话分别进入 agent queue、再回到待认领队列。
- 旧应用代码在 R2/R3 扩展 schema 上保留了本地读写兼容。这不是 staging/生产 rollback、数据库 downgrade 或 Worker/待确认动作故障演练。API 停止、隔离数据库及临时 token/seed 文件均已清理；没有读写共享开发库。

**Jev 使用记录**：本轮未使用 Jev；Codex 完成隔离环境准备、旧版本兼容 smoke 与清理核验。

---

## 13. Codex follow-up：双坐席浏览器并发认领 smoke（2026-09-28）

- 在新建隔离 PostgreSQL 库中准备两个独立的 synthetic `support_agent`、agent profiles 和一个 queued conversation；两个 Chrome 标签使用不同 localhost origin 和各自本地合成 bootstrap token，访问当前分支前端及隔离 API。
- 两个浏览器同时点击“接入会话”：Synthetic Seat Two 收到成功状态并在“我的会话”中看到该项；Synthetic Seat One 收到 lease/version 冲突，回复区保持只读。数据库最后验证 `owner_type=human`、`lease_version=2` 且只有一个 `owner_ref`，证明单条会话不会被两名坐席同时接管。
- 此测试不包含客户消息、Case 或真实连接器，也没有验证双浏览器 Copilot 草稿串扰、断线恢复和真实 OIDC 撤权。临时 Chrome tabs、两套 Vite、隔离 API、数据库及测试令牌已清理。

**Jev 使用记录**：本轮未使用 Jev；Codex 完成合成双坐席并发场景、浏览器复验与隔离环境清理。

---

## 14. Codex follow-up：R3 外部记录与客户归属 fail-closed 校验（2026-09-28）

- `read_verified_fact()` 现在要求 canonical `source_record_ref` 与本次请求的 `record_ref` 完全一致；provider 返回同租户/同账户下的另一条记录也会以 `BUSINESS_RECORD_MISMATCH` 被拒绝。
- 含 `customer_account_ref` 的事实现在必须同时带服务端期望账户和匹配的 `OwnershipProof`。调用方漏传账户范围或 proof 会以 `BUSINESS_OWNERSHIP_UNVERIFIED` 失败；原有验证继续绑定 tenant、connector、authority binding/version、具体外部资源和 freshness TTL。
- 新增错误 provider 返回其他记录、customer-scoped read 漏传账户范围的回归用例；更新 R3 contract、execution plan 和 acceptance 中的本地/外部条件区分。
- 本地复验：`apps/api/tests/unit/integrations` 与 `packages/contracts/tests` **140 passed**（含本次新增回归）；Ruff、格式检查、目标模块 Mypy 和 `git diff --check` 通过。仅使用合成 fake provider；未连接或声称验收真实 ERP/CRM。
- 实现提交 `fabeb30b557722184581d59d01232eea4732860b` 已推送到 PR #27；GitHub Actions [run #36396710697](https://github.com/kayon0209/b2b-ai-support-platform/actions/runs/36396710697) 九个 job 全部通过，包括 integration、完整 release-evidence/零容忍、并发门禁、Admin Web、typecheck、lint、依赖审计和 secret scan。
- 浏览器 UI-01 视口已查看，但逐规格 PNG 未归档；浏览器下载策略拒绝该导出，Chrome headless 在本机应用注册时崩溃。该截图证据仍待通过批准的环境归档；不影响本节 R3 合同测试结论。

**Jev 使用记录**：本轮未使用 Jev；Codex 完成 R3 风险面审查、fail-closed 修复、回归测试和验收文档同步。

---

## 15. Codex follow-up：Workbench 路由切换时的会话上下文隔离（2026-09-28）

- 旧版路由会在新详情请求完成前继续保留上一会话的 `detail`。主聊天面板原先做了 ref 比较，右侧 Copilot/客户上下文与转接/结束对话框没有一致的 current-route gate；用户可在路由切换窗口看到旧会话信息。
- 新增统一的 active conversation identity guard：详情不属于当前 URL 时显示加载状态、隐藏副驾和客户内容、禁止从旧详情发起新的回复/任务写入；转接与结束对话框随会话变化关闭。
- 旧会话的 Copilot 请求、分页历史、快捷话术、回复、附件及分类更正等异步结果在回写前校验 active conversation。待重试回复幂等键按 conversation_ref 分开，避免两个会话的相同文案复用同一 key；旧发送请求完成时，仅当草稿 revision 未变化才清空输入框。
- 新增 `workbench-conversation.test.mts`，覆盖 4 个 route identity、6 个幂等键和 2 个草稿 revision 保护断言；Admin Web 全部行为测试、typecheck 和 production build 通过。尚未做真实非空会话浏览器矩阵、跨标签草稿同步、断线恢复或 VoiceOver 验收。
- 实现提交 `56833ae3f91d83cd8492020de8ae58b5bd25ee72` 已推送至 PR #27；GitHub Actions [run #36401909032](https://github.com/kayon0209/b2b-ai-support-platform/actions/runs/36401909032) 九个 job 全部通过，包括 Admin Web、集成测试、并发门禁及完整 release-evidence/零容忍检查。

**Jev 使用记录**：本轮未使用 Jev；Codex 完成路由/异步竞态审查、状态隔离修复、前端复验和验收文档更新。

---

## 16. Codex follow-up：非空工作台、草稿恢复与本地 staging 模拟（2026-09-28）

### 草稿同步实现

- 新增 `apps/admin-web/src/lib/workbenchDraftSync.ts`：草稿按 actor/conversation 分区，严格校验 schema、长度、时钟偏差和 30 分钟 TTL；草稿正文只写当前浏览器 tab 的 `sessionStorage`，同源标签间经 `BroadcastChannel` 交换。没有使用 `localStorage`，也没有发送到 API。
- 新标签打开同一会话时通过 request/draft 握手补齐当前草稿；两边编辑不同内容时显示冲突提示，需坐席明确选择“同步另一标签页”或“保留本页”。清空状态同样可同步。queue/right tablist 只处理无修饰键的方向键/Home/End，Ctrl/Option/Shift/Meta 修饰键不得触发应用级切换。
- `workbench-conversation.test.mts` 现覆盖 4 identity、6 idempotency、2 draft revision、13 draft sync 断言；`workbench-tabs.test.mts` 21 项验证无修饰键导航和修饰键忽略。

### 浏览器与辅助技术复验

- Safari 同源双标签使用隔离 API 与两条非空合成会话：A 标签草稿广播到同会话新标签；两个标签各自编辑后显示冲突且不静默覆盖；分别实测保留本页与显式采用远端草稿。切换到 B 会话时 A 草稿不显示。B 的会话专属草稿在同一标签刷新后恢复。没有发送任何回复。
- macOS 系统设置确认 VoiceOver 最初关闭，本轮按授权临时开启；Safari AX tree 可见 skip-main、主导航、队列 tabs、会话按钮、三条消息、带标签 composer、发送按钮及 Copilot tab/panel。自动化键盘 Control+Option+Arrow 在此运行环境无法可靠与页面箭头事件区分，出现过队列 tab 切换，因此不把 AX 树检查冒充完整原生 VoiceOver 朗读/键盘验收；该部分仍需真实 VoiceOver 手动复核。验收后已恢复原始状态：VoiceOver 关闭、Option 修饰键为右 Option。
- 逐规格截图 PNG 仍未归档：浏览器安全策略阻止页面导出，已有画面仅作临时目视检查，不声称存在持久化截图证据。

### 断线恢复与本地 staging 模拟

- 专用隔离 PostgreSQL `workbench_live_20260928` 已升级至 `0070_outbox_processing_fence`。停止本地合成 API 后刷新 B 会话，页面呈现暂不可用/重试，未拥有详情时回复区不开放；启动 API 后轮询恢复队列与非空详情，按坐席/会话键从 `sessionStorage` 恢复 B 草稿；点击重试清除错误提示。API `/healthz` 和授权队列在恢复后分别返回 200。
- 本地“staging”仅为回环地址 UI/容器模拟：候选 Admin Web 运行于 `127.0.0.1:15175`；从 Git commit `68de17e` 导出的基线 UI 运行于 `127.0.0.1:15176`；二者连接同一专用 synthetic API/数据库。基线 UI 可读取 2 条队列会话、B 会话 3 条 timeline turns；切回候选标签后 B 草稿仍在。它验证兼容/恢复 smoke，不是云 staging、Kubernetes、多实例、数据库降级或生产 rollback 演练。

### 模拟业务系统与合成评测

- Demo ERP `business_api_adapter=demo` 通过受控 Tool Gateway 路径回答合成订单 `SO-9001`，receipt 带 `source=demo` 与 `fetched_at`；不可用 provider 走明确的 abstain/error 路径。`test_the_shipped_demo_provider_answers_a_real_question` 和 `test_an_unreachable_erp_tells_the_customer_what_is_wrong` **2 passed**；visitor ownership、1,700 条语义数据 manifest、runner 和 release-gate 单测共 **33 passed**。这些样例全为合成数据。
- `test_release_evaluation_requires_service_evidence_two_reviewers_and_supports_rollback` 在隔离 PostgreSQL **1 passed**：使用 fake evaluator、deterministic embedder、合成数据和测试密钥验证 candidate-aware evidence 路径，同时确认没有平台来源的自报前测/后测被 `EVALUATOR_PROVENANCE_UNAVAILABLE` 拒绝。测试通过不构成独立人工审批、真实模型质量分数或发布授权。

### 本地复验与仍未关闭项

- Admin Web：`npm test` 通过（URL 12、dialog 7、task panel 10、workbench tabs 21、conversation/draft guards 25 项）；`npm run typecheck`、`npm run build`、`npm run check:runtime` 均通过。
- 本地 pytest：demo ERP/failure 与 visitor ownership/eval dataset/runner/release gates 共 35 项通过；候选知识发布门禁集成另 1 项通过。GitHub Actions [run #36413261026](https://github.com/kayon0209/b2b-ai-support-platform/actions/runs/36413261026) 在实现提交 `8f47c34` 上 9 个 job 全部通过，包含 unit、integration、Release Evidence/零容忍、Admin Web、typecheck、lint、dependency scan、secret scan 和 concurrency guard。该运行验证本节实现提交；后续文档同步的 PR CI 以 Actions 最新状态为准。
- UI-02 仍为部分完成：非空页面 AX/键盘与功能路径已核查，完整原生 VoiceOver 朗读/弹层浏览未被本自动化接口可靠验证。R3 真实 ERP/CRM、人工批准固定评测集、真实模型 holdout/费用、企业身份、生产 staging/多实例 rollback 均未提供；任何模拟结果都不替代这些证据，所有生产 release flags 保持关闭。
- `acceptance.md` 与执行计划按上述证据同步。本轮没有访问客户/第三方系统，没有调用真实模型，没有合并或生产发布。

**Jev 使用记录**：本轮未使用 Jev；Codex 负责实现、合成验证、验收边界判断与文档同步。

---

## 17. Codex follow-up：R2-02 Demo 质量/技术受理闭环与剩余项复核（2026-09-28）

### 对进度评估的复核

- 原评估总体准确：PR #27 在本地 HEAD `b45b846` 时与远端同步、工作区干净、仍是 Draft；那一版最新 CI 9 个 job 全通过。新改动尚待提交，因此该 CI 不能作为本轮代码的 CI 证据。
- 需要区分“真实订单系统未接入”和“本地流程能力未实现”：通用支持旅程原本已有 Tool Gateway + Demo ERP。此次也把受控查询接入 R2-02 `order_status` task：先核对会话账户与 Demo 订单 owner，再经 Tool Gateway 查询，最小回执带 `source=demo`/`fetched_at` 写入 task。真实订单系统和真实 owner proof 仍未接入。
- 获批评测集、真实 provider/权威映射、生产近似 staging、多实例运维、真人 VoiceOver 朗读和 PNG 归档仍是外部/人工证据门禁，不能由本地模拟替代。

### 本轮代码与边界

- `repair_quality_intake` 和 `technical_escalation` 的 local/test Demo 路径现可从会话唯一关联的 tenant Account 解析 `business_system_refs.business_api`，核对客户提供的 `product_ref` 是否属于该账户；未核验 ID 不会写为 verified slot。
- `order_status` 也有显式 local/test 查询动作：订单号先按 Demo 账户归属核验，查询经 `order.get_status` Tool Gateway 执行；只有执行 read receipt、来源标记和 owner 复核通过才把 task 标为 succeeded。未找到/异常/unknown 不生成成功回执，也不会自动发送客户消息。
- 经过核验的流程可准备 `case.create` 提案，分别创建 `quality_issue` / `technical_escalation` 内部 Case，并路由到同租户 `quality` / `engineering` Department。提案要求 `tool.write.confirmed`，部门须至少有一个 active support owner；Tool Gateway 执行前再次核验账户映射、合成产品归属、Demo 环境和路由部门，之后读回 Case。创建 Case 只代表平台内部登记/分派，不代表质量裁定、维修或外部工程系统已受理。
- 客户 issue/question 中符合规则的手机号、邮箱会在任务和 Case 持久化前脱敏。Demo verifier 的目录只包含代码内合成记录，部署开关默认关闭，且不允许 staging/production 使用此路径。
- 文档已同步 `r2-02-standard-flow-catalog.md`、`r2-r3-execution-plan.md` 和 FLOW-01 acceptance。真实 ERP/MES/WMS/CRM、真实质量/工程连接器和发布门禁状态没有被改写为已完成。

### 本地验证与余项

- 隔离 PostgreSQL 数据库 `codex_r2flow_test_20260928` 从空库迁移到 `0070_outbox_processing_fence` 后运行标准流程、状态机与 Case Gateway 定向套件：**94 passed**，含订单 owner 拒绝、Tool Gateway query/read receipt、错误账户产品拒绝、账户映射变更后执行阻断、无 active owner 拒绝、PII 脱敏、人工确认、部门路由和 verified task receipt。该临时数据库在验收后删除；共用 `platform` 数据库未被迁移。
- Ruff check/format、10 个源文件的 Mypy 均通过；Admin Web 的 `npm test`、typecheck、production build 和 runtime guards 均通过。
- 剩余主要是外部证据和真实集成：ERP/MES/WMS/CRM sandbox/authority、质量与工程系统 connector、人工批准固定评测集、真实模型/预算、staging 多实例/rollback、VoiceOver 真人走查和受浏览器安全策略阻止的 PNG 归档。Demo 流程只验证平台控制面，不代表外部业务已执行。
- 本轮 GitHub CI 仍待更新代码提交后重跑；所有 production evaluator/release/standard-flow flag 继续关闭，没有连接客户或第三方系统，也没有调用真实模型。

**Jev 使用记录**：本轮未使用 Jev；Codex 核对当前 PR/CI 与代码路径，完成本地 Demo 流程实现、复验和验收边界修订。

---

## 18. Codex follow-up：九域 synthetic canonical authority 复核（2026-09-28）

- 新增 local/test-only `DemoCanonicalBusinessAdapter`，为 customer account、order、invoice、work order、shipment、opportunity、product specification、inventory、quote 返回代码内固定样本。每个结果带 `source_version=demo-fixture-v1`，并继续经 `read_verified_fact()` 核对 tenant、binding/version、connector、record ref、freshness TTL 和 account ownership proof。
- Adapter 只读取合成数据；不会创建或批准 `AuthorityBinding`，不会访问数据库、网络或外部系统，也不会触发写入。它拒绝 staging/production 配置和 domain/system-kind 不匹配。Demo ERP 的 `inventory.check_stock` 另有 `PCB-DEMO-100` 合成 stock record，回执标记 `source=demo`。
- `scripts/demo_r3_authorities.py` 可本地运行并列出九域事实及售前证据包；输出明确标记 synthetic、客户报价禁止和没有 CRM write，临时 fixture actor 不代表审批人。
- `test_demo_canonical_business.py` 覆盖九域读取、身份/时效证明、跨账户拒绝、不兼容 system-kind 拒绝、环境 fail-closed、Demo ERP 库存回执，以及一个必须提供产品规格/客户库存/有效报价三份证据的合成售前复核包。输出明确禁止作为客户报价并要求人工 handoff。该测试验证协议与模拟器，不构成人工批准数据集或真实 authority 验收。
- 新增合成 OIDC 边界测试：有效测试 identity 在 tenant membership 从 active 改为 suspended 后，下一次平台 API 请求返回 401。它证明应用每次请求重解 membership；不模拟或证明 Keycloak/外部 IdP 的 refresh/access token 吊销。
- 本地 performance smoke：100 条合成 webhook 并发写入、warm pool 10，最新一轮 P50 31.2ms/P95 49.0ms/P99 49.1ms，100 rows 持久化且 duplicate replay 命中。此结果只度量单机 webhook ACK 的 warm-pool 路径；不代表模型、队列、shadow/off 对照或生产容量门禁通过。
- 最终组合复验：local/test isolated database 上 **149 passed**，包括 R2 标准流程、OIDC membership deactivation、R3 九域 canonical fake、pre-sales evidence guard 与 migration-independent webhook perf smoke；Ruff/Mypy 检查通过。上述 synthetic webhook 指标不能替代 off/shadow、多轮负载、模型 p95 或生产拓扑验收。仍需完整 HEAD CI。
- R3-02 的 local/test Workbench 有只读 synthetic 证据面板；它不自动判断产品适配度，也无 CRM opportunity 写入。真实产品/库存/客户报价 source、CRM sandbox 和外部 owner proof 仍待确定。真实评测审核、生产性能、企业 OIDC 撤权、真人 VoiceOver 和浏览器安全策略阻止的 PNG 归档仍是独立验收门禁。

**Jev 使用记录**：本轮未使用 Jev；Codex 编写并验证了 local/test synthetic canonical provider 和 R3 边界文档。

---

## 19. Codex follow-up：Workbench 售前证据、权限与全量静态复验（2026-09-29）

- **远端评估核对**：PR #27 仍为 Draft，目标分支仍是 `codex/ai-support-v2-r1-fix`；远端 `b45b846` 的最新 Actions run #36415996078 九个 job 成功。当前隔离分支 `codex/r2-r3-implementation` 在该远端 head 之后另有本地提交和本轮代码改动，因此这次实现尚无对应 GitHub CI，也没有推送到远端。
- **R3-02 Workbench 本地入口**：增加 local/test Demo 只读证据接口和任务面板，按当前会话关联 Case 在服务端解析账户，分别核对产品规格、账户库存和客户报价的记录引用、来源版本、有效期与账户归属。接口同时要求 `case.read` 和 `tool.read`；任务列表只在具备 `tool.read` 时显示面板。账户不明、来源不匹配、未知商品和无业务读取权限均 fail-closed。合成 authority binding 只能在 local/test Demo 环境创建，仍是临时 fixture，不记录或声称真实审批。
- **不可对客承诺**：面板清楚标记样例为 synthetic，显示 `customer_quote_allowed=false` 和销售人工复核要求；不创建 CRM opportunity、不发送消息、不触发外部写入。非空 Workbench 浏览器会话含三条合成对话；键盘 Enter 可读取三份 evidence，状态区用 `aria-live` 播报，产品输入有可访问名称与帮助文字。标准流程说明中发现的旧“执行器尚未接入”提示已改为当前 Demo 边界。
- **全仓类型检查修复**：Mypy 2.3.1 在 258 个 Python 源文件中报告了 5 个既有问题：知识候选查询的 ORM 行被推断为 `Any`，两个 worker 将普通 Python 值传给 SQLAlchemy `tuple_`。现已显式收窄候选 ORM 类型，并用等价的时间戳加 UUID 词典序谓词查询历史消息；全量 Mypy 复验通过。
- **PostgreSQL 集成复验**：随机隔离数据库从空库迁移到 `0070_outbox_processing_fence`，运行标准流程、知识发布 gate、客户旅程任务规划、shadow side-effect 和 Demo authority 测试：**73 passed**，1 条 Starlette/httpx deprecation warning。随后在另一新隔离库重跑标准流程及售前权限显隐用例：**25 passed**。临时库随后删除，共用 `platform` 数据库未迁移。
- **前端与静态检查**：Admin Web 测试通过（12、7、10 项三个小套件，WorkBench tabs 21 项，身份/幂等/版本/草稿同步保护通过）；`npm run typecheck`、`npm run build`、`npm run check:runtime` 通过。7 个变更 Python 文件 Ruff check/format 通过；Mypy 全仓 258 个源文件通过。
- **R3 Demo 脚本**：`scripts/demo_r3_authorities.py` 成功列出九个固定 synthetic domain facts 和组合证据；结果标明没有人类 authority approval、客户报价禁止、没有 CRM write。
- **仍未关闭的门禁**：真实 ERP/MES/WMS/CRM/质量/工程 sandbox 与 authority 映射、经人工批准的评测集/主体映射、真实模型质量及费用上限、企业 IdP 撤权、真实 staging 多实例/回滚和外部写入 unknown 对账仍需相应人员/环境。完整原生 macOS VoiceOver 浏览器与弹层朗读/键盘验收仍需真人。本节记录时 PNG 尚未归档；后续本地 Playwright 截图见 §20。生产发布、评测和标准流程 feature flags 保持默认关闭。

**Jev 使用记录**：本轮未使用 Jev；Codex 核对 PR/CI、实现售前只读面板与权限边界，修复全仓类型错误并完成本地复验。

---

## 20. Codex follow-up：R3 Demo CRM、Worker/性能演练、Keycloak 与可访问性证据（2026-09-29）

### 本轮实现

- **R3 Demo CRM 写入闭环**：新增 `crm.create_opportunity`，风险级别为 `confirmed_write`，要求 `tool.write.confirmed` 和坐席人工确认。执行器仅在 `APP_ENVIRONMENT=local|test`、`APP_BUSINESS_API_ADAPTER=demo` 且 connector 配置为 `mode=synthetic` 时建立。模拟机会保存在进程内，不访问网络或外部数据库；记录标记 `synthetic=true`、`customer_contacted=false`，不生成价格。执行后从模拟记录读回并由 Tool Gateway 验证。提案幂等按 tenant/key 加事务 advisory lock，重复相同行为回放原提案，不同 payload 返回 `IDEMPOTENCY_CONFLICT` 并审计。
- **可重复多 Worker 演练**：`infra/compose/docker-compose.r2-r3-drill.yml` 启动独立临时 PostgreSQL、Redis 与两个同类 outbox Worker。脚本在账务处理事务中杀死一个 Worker，验证另一 Worker 恢复同一 event；随后重放 event 确认只有一条账务行，并测量 1/10/50 个 synthetic usage event 的队列排空时间。最后运行目标集成套件及 `0070_outbox_processing_fence` 单步 downgrade/upgrade。
- **本地 off/shadow 性能对照**：新增 `scripts/run_local_synthetic_performance.py`，独立数据库上两种模式、并发 50/100、每格 3 轮；分别测量 warm-pool inbox persistence 与真实 shadow enqueue gate，不调用模型。该对照的影子 outbox 数为 off 0、shadow 450。原 pool 5/50 性能断言与本机 macOS 测量方向相反，现改为记录主机实测结果并保留 1500ms 本地宽松上限，避免把 Windows 的单机观测硬编码成跨主机门禁。
- **本地 Keycloak membership 撤权**：新增 `scripts/run_local_keycloak_revocation.py`。临时 realm 签发的真实 JWT 经平台 OIDC/JWKS 验证；active membership 下 `/v1/cases` 返回 200，membership 暂停后复用同一 JWT 返回 401。realm 与隔离数据库由脚本清理；不会保存 token 或密码。
- **截图与自动可访问性**：新增 `scripts/run_local_browser_acceptance.py` 与 Playwright/axe 检查；用 3 条合成消息打开非空 Workbench，键盘 Enter 读取证据并检查控件名称、描述及 `aria-live`。修复 axe 检出的状态、空队列、渠道标签和页签低对比度颜色。已生成 1536×1024、1280×800、390×844、320×844、768×512 CSS 200% 模拟共 5 张截图，以及 ARIA snapshot/JSON 报告。

### 本地复验结果

- Worker 故障恢复演练：两个 Worker 启动；被杀 Worker 的同一 event 被 peer 恢复，重复重放仍只有 1 条账务记录。1/10/50 个队列项排空分别约 1021ms/1018ms/1055ms。定向集成和 CRM 用例 **49 passed**；`0070_outbox_processing_fence` downgrade/upgrade 往返通过且保留账务行。
- off/shadow 对照共 12 轮。Inbox persistence P95（ms）：off/50 并发 `51.476, 18.642, 23.588`；off/100 并发 `37.321, 36.629, 36.258`；shadow/50 并发 `18.896, 41.440, 18.411`；shadow/100 并发 `35.768, 35.862, 34.983`。该路径测量 inbox 持久化函数，不含真实 HTTP socket、签名验证、模型调用或生产网络；数值是本机合成观测，不用于容量承诺。
- Keycloak 演练：已签 token 200 → membership suspended 后同 token 401。它证明平台每次请求复核 membership；**不**证明企业 IdP 会吊销已签发 token/session。
- Workbench 浏览器：5 个截图视口均为 0px 横向溢出；桌面 1536×1024 和移动 390×844 的 axe WCAG 2/2.1 A/AA 扫描均为 0 violations；键盘路径、输入 label/description 和 polite live region 检查通过。报告中的 `manualVoiceOverConfirmed=false`；完整原生 VoiceOver 朗读/键盘走查仍待真人。
- Admin Web `npm run test`、`npm run typecheck`、`npm run check:runtime` 与 `npm run build` 通过；Ruff check/format 通过，Mypy 全仓 259 个 Python source 文件通过。

### 证据位置与仍未关闭项

- Worker：`evidence/local-worker-drill-2026-09-29/worker-drill-report.json`
- 性能：`evidence/local-perf-2026-09-29/performance-comparison.json`
- Keycloak：`evidence/local-keycloak-2026-09-29/keycloak-membership-revocation.json`
- 浏览器：`evidence/local-browser-2026-09-29/accessibility-report.json`、`workbench-aria-snapshot.yml` 与五张 PNG。

仍未完成的边界包括真实 ERP/MES/WMS/CRM、真实外部写入超时后的 unknown 对账、生产近似多主机 staging、获批评测数据/真实模型质量与费用、企业 IdP token/session 撤销和完整真人 VoiceOver。所有生产发布开关仍关闭；这些本地合成演练不作为生产容量或企业连接验收。

**Jev 使用记录**：本轮未使用 Jev；Codex 负责实现 Tool Gateway Demo 写入、演练脚本、浏览器修复、验证和验收状态同步。

## 21. 代码评审修复与最终复验（2026-09-29）

本节记录对近期前端与工程代码评审中可在本地完成的问题的修复。企业业务数据源、connector 和外部系统接入代码按用户要求不在本轮修改范围内。

- **任务数据隐私**：标准流程 `FlowField.sensitive` 会在收集和响应投影中生效；任务自由文本统一执行 PII 形状脱敏，历史行在响应前再次净化。电话正则现保留并完整替换国际区号前缀。
- **草稿一致性**：回复成功后按原会话和提交快照版本清理 sessionStorage；广播带新版本的清除消息，不覆盖发送期间新编辑的内容。跨标签接收端拒绝较旧时间戳。任务命令在前端同一请求重试时复用幂等键。
- **任务命令幂等**：新增 `0071_task_command_idempotency`，在 append-only task event 中保存幂等键哈希及请求哈希。同键同请求返回当前任务作为重放结果；同键不同请求返回冲突。隔离集成测试覆盖这两条路径。
- **模块接口**：Task 模块通过 Tool Gateway 的 catalog/receipt/proposal-ID 接口工作；Knowledge 路由通过 Outbox service 读取回执和幂等入队，不再直接访问这些模块的 ORM 模型。
- **工作台 UI**：任务操作错误与列表加载错误分离；标准流程目录有重试；任务和流程按钮有一致状态样式；暗色任务/流程/队列/对话元数据/页签对比度修复；移动抽屉公开 `aria-expanded` 并在关闭时恢复焦点。TaskPanel、标准流程、售前证据及副驾页签支持中英文，用户文案不再显示字段 key 或报价布尔字段。
- **本地性能证据**：Worker 演练报告现在包含 pool 5/50 对比结果；两种 pool 的本机 P50 绝对上限保留，并新增 pool 50 相对 pool 5 不得慢超过 10 倍的宽松异常保护。报告仍明确标记为单机合成数据。

### 最终本地验证

- Admin Web：`npm test` 通过（12 + 7 + 10 组件/状态测试，21 项页签键盘检查，19 项草稿同步检查）；`npm run typecheck` 通过。
- API：任务隐私和规划器单测 **18 passed**；全仓 Mypy **260 source files** 通过；涉及的 16 个 Python 文件 Ruff check/format 通过。
- 隔离 Docker Worker 演练：**80 passed**；Worker 崩溃恢复后保持单条账务记录；1/10/50 任务队列均排空；`0071` downgrade/upgrade 往返后数据保留。
- Playwright/axe：中文桌面亮色、桌面暗色、移动端和英文桌面共 4 组 axe 扫描，均为 **0 violations**；6 张截图，页面横向溢出均为 0；键盘焦点恢复、非空任务行和禁用操作检查通过。
- 截图、ARIA snapshot 与报告：`evidence/local-browser-review-final-2026-09-29/`。Worker/migration/幂等/池对比报告：`evidence/local-worker-drill-review-final-2026-09-29/worker-drill-report.json`。

真人 macOS VoiceOver 完整朗读/弹层走查仍需人工完成；真实企业 IdP、外部连接器、staging 和生产容量门禁仍按各自验收条件处理。本节报告绑定本地隔离 worktree，尚无覆盖当前未提交改动的远端 CI。

**Jev 使用记录**：未使用 Jev；Codex 负责代码实现、隔离数据库/浏览器复验和证据归档。
