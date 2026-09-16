# 项目接手摘要（Zcode 开发工作交接）

**接手时间：** 2026-09-16
**项目：** B2B Enterprise AI Customer Support（Chatwoot 内核 + FastAPI AI 控制面）
**当前版本：** 0.1.0
**结论一句话：** 安全关键的领域逻辑层已基本建成且测试全绿（158/158），但**端到端 AI 链路仍处于"零件齐备、尚未组装"状态**——LLM 未接入、worker 为空、admin-web 为空、核心链路未挂路由。

---

## 一、项目现状总结

### 1.1 技术栈（已落地）

| 层 | 技术 | 状态 |
|---|---|---|
| 语言/框架 | Python 3.12、FastAPI、Pydantic v2 | ✅ 已用 |
| ORM/迁移 | SQLAlchemy 2 async、Alembic | ✅ 11 个迁移全部已应用 |
| 数据库 | PostgreSQL 16 + pgvector + RLS | ✅ 运行中（端口 5435） |
| 缓存/队列 | Redis（独立于 Chatwoot） | ✅ 已部署，未接 Celery |
| 对象存储 | MinIO | ✅ 已部署 |
| 身份 | Keycloak（OIDC） | ✅ 容器运行中 |
| 客服内核 | Chatwoot（不改内核） | ✅ web + sidekiq 运行中 |
| 前端 | React + Vite + TypeScript | ❌ 目录为空 |
| 质量工具 | ruff、mypy(strict)、pytest、detect-secrets | ✅ CI 已配置 |

### 1.2 模块完成度

| 模块 | 代码量 | 状态 | 说明 |
|---|---:|---|---|
| `identity` | ~600 行 | 🟢 完成 | 租户上下文、中间件、OIDC、控制租约、仓储 |
| `support_bridge` | ~700 行 | 🟢 完成 | HMAC 验签、时间戳/重放窗口、InboxEvent 去重、Chatwoot 客户端（幂等+熔断） |
| `knowledge` | ~600 行 | 🟡 骨架 | 模型/ACL/存储/摄取状态机齐备，**解析器缺失** |
| `retrieval` | ~225 行 | 🟡 占位 | RRF 混合检索**真实可用**，但 embedding 是确定性哈希占位 |
| `agent_runtime` | ~256 行 | 🟡 仅决策层 | 引用校验+弃答决策完备，**无 LLM、无编排** |
| `cases` | ~310 行 | 🟢 完成 | Case/SLA 状态机 |
| `tool_gateway` | ~445 行 | 🟢 完成 | 提议→确认→执行→后置校验全链路，幂等+审计 |
| `integrations` | ~700 行 | 🟡 适配器就绪 | CRM/Jira/IM 适配器与弹性策略齐备，**无对外路由** |
| `audit` | ~350 行 | 🟢 完成 | 追加写审计 + 查询 API（已挂路由） |
| `evaluation` | ~700 行 | 🟡 未收口 | 指标/门槛/PII/队列/运行器齐备，**无数据集、无 CLI、无面板** |
| `worker` | 0 行 | ❌ 空 | 仅 3 个空 `__init__.py` |
| `admin-web` | 0 行 | ❌ 空 | 仅 `.gitkeep` |
| `packages/contracts` | 0 行 | ❌ 空 | 文档要求"OpenAPI 单一事实源"，尚未落地 |

### 1.3 实测验证结果（本次接手实际执行）

```
pytest       158 passed, 0 failed   ✅
ruff check   All checks passed      ✅
ruff format  117 files formatted    ✅
alembic      0011_tool_gateway 已应用 ✅
27 张业务表在 ai-postgres 中 ✅
```

**接手时发现并已修复的 3 类问题：**

1. **【真实缺陷】数据库默认端口错误** — `config.py` 默认连 `localhost:5433`，而 compose 实际映射到 `5435`。导致 webhook 链路 6 个集成测试 500 失败。已修正为 5435 并加注释说明。
2. **【质量债】Phase 4 模块 8 处 lint 错误** — 未使用导入、`zip()` 缺 `strict=`、行长超限、导入未排序。已全部修复。
3. **【质量债】5 个文件未按项目格式规范格式化** — 已执行 `ruff format` 对齐。

> 上述修复使 CI 三项（lint / format / test）全部转绿，构成了可安全继续开发的基线。

### 1.4 关键架构决策（已遵守，勿违反）

- **ADR-0001**：Chatwoot 作为有界外部子系统，**禁止直连其数据库**，只走 REST API + 签名 webhook + 版本化事件。
- 租户隔离三重防线：**服务端解析 tenant_id** → 应用层过滤 → **PostgreSQL RLS**（应用层过滤只是补充）。
- **LLM 只能提议，代码负责裁决**：写操作必须过 Tool Gateway 的授权/确认/幂等/后置校验。
- 每次客户可见的 AI 发送前，必须对控制租约做 **compare-and-set（版本比对）**。
- 禁止因"架构好看"引入 Kafka / Milvus / OpenSearch / Temporal / K8s。

---

## 二、待开发功能列表

### A 类：端到端链路缺口（**阻断 MVP，最高优先**）

| # | 待办 | 现状 | 影响 |
|---|---|---|---|
| A1 | **接入真实 LLM** | `AnswerGenerator` 仅有 Protocol，无实现 | AI 无法真正回答任何问题 |
| A2 | **实现 `apps/worker`** | 目录为空，Celery 未配置 | InboxEvent 落库后**无人消费**，消息流断在第二步 |
| A3 | **编排层（orchestrator）** | `qa_path` / `hybrid_search` 无任何业务调用方 | 零件齐备但未组装成流水线 |
| A4 | **接线核心路由** | 仅挂载 `webhooks` / `audit` 两个路由 | knowledge / cases / tools / integrations 全部无 HTTP 入口 |
| A5 | **真实 embedding** | `embed_deterministic` 是哈希占位 | 向量检索无真实语义能力，RAG 质量不可用 |
| A6 | **发送前租约重检** | 租约服务与 CAS 异常已就绪 | 缺"发送前最后一道校验"调用点，存在人机竞态风险 |

### B 类：阶段计划内未完成项

| # | 待办 | 依据 |
|---|---|---|
| B1 | 文档解析器（PDF/DOCX/MD → 结构化分块） | Phase 1 epic 3；`ingest.py` 缺解析落点 |
| B2 | 重排序器（reranker）带超时降级 | Phase 1 epic 4；架构图明确要求 |
| B3 | `AgentRun` / `Citation` / `PromptVersion` 落盘 | 文档硬性要求"每次运行记录 prompt/模型/检索版本" |
| B4 | MinIO 租户前缀对象键 + 不可变原件上传 | Phase 1 epic 3 |
| B5 | OpenTelemetry 追踪 + 脱敏 JSON 日志接入 | 包已就绪（`observability.py`），未接入运行链路 |
| B6 | 知识缺口队列 + 复核草稿工作流 | Phase 4 |
| B7 | PII 脱敏/保留策略接入模型与日志边界 | Phase 4 |

### C 类：交付物缺口

| # | 待办 | 说明 |
|---|---|---|
| C1 | **admin-web 前端**（React/Vite/TS） | 目录为空，企业管理员无界面可用 |
| C2 | **`packages/contracts`** 落地 OpenAPI/事件 schema | 当前 0 行，与"OpenAPI 为单一事实源"冲突 |
| C3 | 评估数据集（50–100 条真实问题） | Phase 0 要求，当前只有运行器无数据 |
| C4 | 生产级 Compose / K8s 模板 | 仅本地 compose，缺 worker 服务定义 |
| C5 | 关键性能基准（50–100 并发会话、首 token P95<2.5s） | 全部性能指标未实测 |

---

## 三、建议开发优先级

排序原则：**先打通纵向端到端闭环，再横向补全能力**。当前最大风险不是"某个模块弱"，而是"没有一条能端到端跑通的链路"——这会让所有后续工作的验收都失去依据。

| 优先级 | 任务 | 理由 |
|---|---|---|
| **P0-1** | A2 worker + A3 编排 + A1 LLM 接入 | 三者必须**一起做**，缺任何一环链路都不通；建议作为一个里程碑交付 |
| **P0-2** | A6 发送前租约重检 | 安全红线。人机竞态一旦出错是"AI 抢答"级别的生产事故 |
| **P0-3** | A4 核心路由接线 | 让已有模块可被调用与验收 |
| **P1-1** | A5 真实 embedding + B2 重排序 | RAG 质量的真正来源，直接决定回答准确率 |
| **P1-2** | B3 AgentRun/Citation 落盘 | 可观测性与可追溯性的前置条件 |
| **P1-3** | C3 评估数据集 → 建立回归基线 | 有了数据集才能量化每次改动的收益/退化 |
| **P1-4** | B1 文档解析器 | 知识入库的实际入口 |
| **P2** | C1 admin-web | 面向使用者，可在链路稳定后并行 |
| **P2** | C2 contracts、B5 OTel、B4 MinIO 前缀 | 工程完备性 |
| **P3** | B6 知识缺口、B7 PII 策略、C4/C5 | Phase 4 收尾 |

---

## 四、下一步具体实施计划

### 里程碑 M1：打通"客户消息 → 有引用回答 → 回发 Chatwoot"闭环

这是**当前唯一正确的下一步**。目标产物：一条真实客户消息能被 AI 检索、生成带引用的回答、并通过 Chatwoot API 回发。

**实施顺序（建议按此依赖链推进）：**

1. **定义 LLM 边界实现**（替换 `AnswerGenerator` 占位）
   - 新增 `agent_runtime/llm.py`，实现 `async def generate(question, evidence) -> DraftAnswer`
   - 要求：外部调用必须带超时、有界重试、结构化错误映射（AGENTS.md 硬性要求）
   - 输入必须是**已脱敏的最小化上下文**，不得整段复制对话
   - 返回的 `claims` 必须回填引用的 `chunk_id`，供现有 `validate_citations` 校验

2. **实现 `apps/worker`（Celery）**
   - 明确使用**独立于 Chatwoot 的 Redis**（当前 `6380`）
   - 消费 `inbox_events`，按 `delivery_id` 去重
   - 注册**交互优先队列**，保护交互类任务不被批量任务挤占（`evaluation/queues.py` 的准入逻辑可复用）
   - 任务需幂等：重复投递不得产生重复客户回复

3. **编写编排器 `agent_runtime/orchestrator.py`**
   - 严格按 `docs/agent.md` 请求流水线实现：
     ```
     解析租户/会话 → 获取控制租约 → 脱敏最小化 → 意图与风险分类
     → 选路（answer/确定性流程/转人工） → 检索授权证据
     → 生成草稿 → 校验引用与策略 → 【重检租约】→ 经 Chatwoot 回发
     → 落盘 run / citations / audit / metrics
     ```
   - **第 7 步"重检租约"是安全红线，不可省略**：发送前比对 `lease_version`，若已变更则丢弃本次输出
   - 编排器应保持确定性，LLM 调用只是其中一个可替换环节

4. **接线路由**（`main.py`）
   - 为 knowledge / cases / tools / integrations 增加各自 `router.py` 并 `include_router`
   - 每个写命令必须支持 `Idempotency-Key`，响应必须带 `trace_id`
   - 统一错误信封：`{"error": {"code","message","retryable","details"}, "trace_id"}`

5. **补测试（Definition of Done 要求）**
   - 重复 webhook → 单次回复（现有测试已覆盖入库侧，需补回复侧）
   - 人机竞态：转人工后，在途生成不得发出
   - 跨租户负向测试扩展到新路由
   - 迁移须对生产近似快照做验证

**M1 验收门槛（取自文档，不可协商）：**
- 重复 webhook 投递绝不产生重复客户回复
- 每个企业事实性回答都带有效引用
- 证据缺失时弃答或转人工，绝不猜测
- 转人工可阻止在途 AI 输出
- 每次运行可识别 prompt / 模型 / 检索 / 代码版本
- 首 token P95 < 2.5s

### 里程碑 M2（紧随其后）
接入真实 embedding 与重排序器 → 建立评估数据集与回归门槛 → 用数据校准检索与弃答阈值。

---

## 五、风险提示

| 风险 | 说明 | 建议 |
|---|---|---|
| **人机竞态** | 发送前租约重检缺调用点，是当前最严重的安全缺口 | 列为 P0，与 M1 同期完成 |
| **占位 embedding 混入生产** | `embed_deterministic` 命名清晰但是哈希占位，易被误当作可用能力 | 替换前禁止对外宣称 RAG 可用；建议加显式告警或 `NotImplementedError` 边界 |
| **窗口内配置漂移** | 已发现 `5433/5435` 端口不一致，说明配置存在多源 | 建议统一由 `.env` 单一来源驱动，CI 增加"配置与 compose 一致性"校验 |
| **mypy strict 未真正生效** | `packages/*/src/__init__.py` 造成模块名重复，mypy 在报错后即停止检查 | 需调整 `mypy_path`/`explicit-package-bases` 或移除空 `__init__.py`，否则类型门禁形同虚设 |
| **无 Git 仓库** | 当前目录**不是 git 仓库**，无版本历史、无回滚能力 | **强烈建议立即 `git init` 并首次提交**，这是所有"回滚/迁移/审计"承诺的基础 |
| **worker 未定义在 compose** | compose 中无 `ai-worker` 服务 | M1 实现 worker 时同步补 compose 服务定义 |

---

## 六、接手后的建议动作（下一步）

1. **立即**：`git init` 并做基线提交，锁定当前全绿状态。
2. **本次已修**：端口默认值、lint、格式化（已完成，可直接提交）。
3. **启动 M1**：按上述依赖链推进——LLM 边界 → worker → 编排器（含租约重检）→ 路由接线 → 补测试。
4. **并行可选**：修正 mypy 配置，让 strict 类型检查真正生效。

---

## 七、进度更新（后续轮次）

本节记录本文档写成之后实际完成的开发，供后续接手者对齐。

### 已完成

| 提交 | 内容 | 效果 |
|---|---|---|
| `11519e5` | 建立 git 基线 | 锁定 158 全绿状态 |
| `586a5ce` | **M1 垂直切片**：Gitee AI（`qwen3.8-flash`）接入、答案生成器、真实 embedding + reranker、编排器（**含发送前租约重检**）、worker（SKIP LOCKED 幂等） | 打通"客户消息 → 有引用回答 → 回发"闭环；关闭**安全红线缺口 A6** |
| `868b673` | `.env` 模板与 gitignore、真实供应商冒烟脚本、role 序列化修复 | 凭据不入库；`ProviderRole` 序列化正确 |
| `3b57cd9` | **M2 HTTP 接口层**：`cases` / `retrieval` / `agent_runtime` / `tool_gateway` 四组路由 + `api.py` 共享信封与策略闸门 | 12 个 endpoint，`main.py` 挂载 6 个 router |
| `6709499` | **工具执行器注册**：从租户 `connectors` 解析适配器 | 关闭"确认式写入永远执行不了"缺口 |

### M1 阶段发现并修复的真实缺陷

**弃用词击败弃答闸门。** `qa_path._term_overlap` 把虚词也计入重叠度，
只要片段含 "the" 就能越过 `MIN_EXCERPT_OVERLAP = 0.12`，
使无关问题带着不相关证据进入模型。修复后：

| 查询 | 修复前 | 修复后 |
|---|---|---|
| "how long is the refund window?"（相关） | 0.600 | **0.667** |
| "who won the world cup in 1998?"（无关） | 0.167 通过 ❌ | **0.000** ✅ |
| "what is the capital of the moon?"（无关） | 0.250 通过 ❌ | **0.000** ✅ |
| "the the the the"（退化） | 1.000 通过 ❌ | **0.000** ✅ |

### M2 阶段发现并修复的真实缺陷

1. **policy 表缺 `TOOL_WRITE_CONFIRMED`** —— `support_admin` 未被授予该 action，
   导致确认式写入对所有非 `tenant_owner` 角色**完全不可达**，
   确认闸门形同虚设。已在 `RBAC_TABLE.support_admin` 补上
   （`TOOL_HUMAN_APPROVAL` 仍保留给 `tenant_owner`）。
2. **`CrmReadAdapter` 不满足 `ToolExecutor` 协议** —— 其继承基类的
   `execute(command, parameters, idempotency_key) -> ExecutionResult` 与
   `verify_postcondition(execution: ExecutionResult)` 与协议要求的签名
   **完全不同**；一旦注册，首次执行必然 `TypeError`。
   已从注册表与工具词表中同步移除 `crm.update_account`，
   并新增双向一致性回归测试。（由 mypy strict 发现）

### M3 阶段：真实端到端验证发现并修复的缺陷

这一轮第一次把 worker 接到**真实 Chatwoot** 上跑通全链路，发现的都不是单测能发现的
问题——前三个都是"单测全绿但线上一定出事"的类型。

1. **worker 组装了 4 个 `None` 的 `OrchestratorDeps`（静默空转，最高严重级）** ——
   `runner.main()` 构造 `OrchestratorDeps(embedder=None, generator=None, sender=None)`。
   编排器把「无 generator」当 abstain、把「无 sender」当**"没发出去但不是失败"**
   （`_dispatch`: `if self._deps.sender is None: return ""`）。
   后果：worker 领走真实客户消息 → 标 COMPLETED → **一个回复都不发**，
   且任何日志/指标都显示成功。新增 `worker/wiring.py` 作为唯一组装点，
   缺 LLM key 直接 fail closed（`SystemExit` 子类，容器非 0 退出）。
   另有 **空 token 陷阱**：compose 写 `${APP_CHATWOOT_API_TOKEN:-}`，
   变量永远"已设置"（常为空串），只判 `is not None` 会绑一个每次 401 的 client。

2. **AI 回复自己的回复 → 无限循环（严重）** ——
   Chatwoot 对 **outbound 消息同样发 `message_created` webhook**，
   而 `inbox_consumer` 只检查 `event_type`，**从不检查消息方向**。
   实测证据：**一条**客户提问使 agent_runs 一路写到 message id **17**，
   payload 里 `message_type=outgoing`、`sender_type=user` 明明都有，但没人看。
   修复：`CUSTOMER_MESSAGE_TYPES = {"incoming"}` + `is_customer_message()`，
   非客户消息记 `event_skipped_not_customer` 并 ack。
   方向判定刻意**不对称**：只有显式 `incoming` 才答，缺失/未知一律不答
   （漏答可由人工补救，回复循环不可挽回）；同时兼容 REST 的整数编码
   （0=incoming, 1=outgoing）与 webhook 的字符串编码。

3. **崩溃后 `PROCESSING` 行永久卡死** —— 没有任何代码把它转回 `RECEIVED`，
   一次 worker 崩溃会**静默丢单**。新增 `reclaim_stale_processing()`（默认 600s），
   在 `drain_once` 每轮开头执行。重跑安全的前提是**出站 command_id 由 event id 派生**，
   同事件重跑不会产生第二条客户可见消息。

4. **`support_bridge/inbox.py::mark_processing` 是坏的死代码** ——
   内部先做一次 `.values()` 为空的 `pg_insert`（注释自称 "placeholder"），
   编译出来是 `INSERT INTO inbox_events (minimized_payload, status, id) VALUES
   (NULL, NULL, NULL)`，一旦被调用必触发 NOT NULL 违约。全仓库无调用点，已删除。

5. **测试可重复性**：`test_outbox_relay.py` 全部 8 例会被**正在运行的 worker 抢走**
   outbox 行，表现为莫名其妙的 `claimed == 1`（期望 3）。新增哨兵行探针
   `_assert_no_live_relay()`，改为带明确指引的 skip。

### 当前测试基线

```
263 passed（M0 基线 158 → M1 199 → M2 232 → M3 263）
ruff check / ruff format  全绿
mypy strict              新增/改动模块全绿；历史遗留错误集中在老 ORM 模型
                         （裸 dict/list 缺类型参数），非本次引入
```

### 真实供应商实测结论（`scripts/smoke_gitee_ai.py`，5/5 通过）

```
[1] chat     qwen3.8-flash       -> OK
[2] embed    dims=1536           -> 与 chunks.embedding vector(1536) 一致，无需迁移
[3] rerank   bge-reranker-v2-m3  -> 相关文档排第一（0.409 vs 0.0/0.0）
[4] cite     有据问题            -> 1 条 claim 引用真实 chunk UUID，validate=True
[5] abstain  无法回答的问题      -> 0 条 claim，validate=False (NO_CLAIMS)
```

关键结论：**Qwen3-Embedding-8B 原生 1024 维，但会遵从 `dimensions: 1536` 请求**，
故现有向量列无需迁移。第 4、5 项共同证明"模型无法通过本流水线发布无支撑答案"。

### 真实 Chatwoot 端到端实测结论（本轮首次跑通）

环境：compose 的 Chatwoot（account 1 `E2E Tenant`，API Inbox 1，
contact 1 `E2E Customer`）。token 通过
`docker compose exec chatwoot-web bundle exec rails runner "puts User.first.access_token.token"` 获取。

```
✅ 收：webhook → inbox_events 落库（minimized_payload 含 message_id /
      chatwoot_account_id / conversation_id / message_type，且不含 content）
✅ 生成：worker 日志 run_completed route=knowledge_qa status=completed
        chunk_count=1 latency_ms=15039
✅ 发：Chatwoot 会话中出现真实 AI 回复（message_type=1）
✅ 引用门禁：无支撑答案被判 NO_CLAIMS 并 abstain，未发出
✅ 控制租约：人类接管后（QUEUED_FOR_HUMAN）发送被拦，
  日志 send_blocked_lease_conflict reason_code="owner is queue"
✅ 自回环防护：outbound 事件被 event_skipped_not_customer 拦截
```

### 仍未完成（下一轮候选）

| 优先级 | 项 | 说明 |
|---|---|---|
| 中 | `admin-web` | React/Vite 目录仍为空 |
| 中 | CRM 写入适配器 | `CrmReadAdapter` 只读；如需 CRM 写入工具，需实现符合 `ToolExecutor` 协议的适配器 |
| 中 | `packages/contracts` | 目录仍为空，OpenAPI/事件 schema 生成客户端未落地 |
| 中 | E2E 关键旅程 #5–#8 | CRM 读取→有据回答、确认写入→一次执行+验证、越权→拒绝并审计、工单 SLA→升级→重开 |
| 中 | 跨租户负向测试扩充 | 现有套件未覆盖 缓存命中 / 文件下载 URL / 后台任务 / 导出看板 |
| 中 | LLM 评估数据集 + 指标 + 发布门禁 | `docs/testing-and-evaluation.md` 规定 12 类，尚未落地 |
| 中 | 故障注入 | 工具执行中杀 worker / 轮换凭据 / Redis 中断 / 模型超时 / 含糊成功 / 引用文档过期 / 流式输出中转人工 |
| 中 | mypy 历史欠债 | 老 ORM 模型类型参数缺失，建议按模块分批清理 |
| 低 | 知识缺口队列、PII 策略、迁移测试、性能测试 | 见第四节 P3 |

