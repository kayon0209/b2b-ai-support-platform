# T00 基线核对与契约差异记录

日期：2026-09-26
执行分支：`codex/ai-support-v2-r1`
基线 commit：`13eef064f8f37c878482669c3bf886b727837ed9`（`master`，与执行包规划基线一致）
本文件回答 T00/DOC-01：现状是什么、与目标契约差在哪里、差异如何处置。

## 1. 基线事实

| 项目 | 实际值 | 核对方式 |
|---|---|---|
| 后端规模 | `apps/api/src` + `packages/*/src` + `apps/worker/src` 共 52,760 行 Python | `find … -name '*.py' \| xargs wc -l` |
| 迁移序号 | 占用至 `0054_workbench_queue_indexes`，共 53 个版本文件 | `ls apps/api/migrations/versions` |
| ADR | 占用至 `0015-conversation-first-workbench` | `ls docs/adr` |
| 单元测试基线 | **1244 passed, 3 failed** | `pytest apps/api/tests/unit packages/contracts/tests -m "not integration"` |
| 既有失败 | 全部在 `apps/api/tests/unit/pricing/test_engine.py`（价格表 provenance 与 2 个参数化 band 用例） | 同上 |
| 本地服务 | PostgreSQL 在 `localhost:5435`、Redis 在 `6380`、MinIO 在 `9100`、API 在 `8000`，容器均运行中 | `docker ps` |

**基线失败的处置**：这 3 个 pricing 失败在开发开始前即存在，与 R1 无关。R1 不修改
`platform_core/pricing` 任何代码或测试，也不删除、不跳过、不放宽其断言。验收时它们应仍为
这 3 项；若数量变化，需说明是本分支引入还是环境差异。

## 2. 工作区与他人改动

- `README.md`、`docs/development-plan.md` 有未提交修改（各 1–2 行，指向本执行包）。
  判定为用户改动，**不纳入本分支提交**，保持工作区原样。
- `.codex/`、各处 `.DS_Store`、`docs/implementation/`（本执行包）均为未跟踪。
  `.codex/` 与 `.DS_Store` 不暂存、不清理。执行包本身是本任务的输入与产出位置，
  按 T09 要求提交脱敏内容。
- 存在另一 Worktree：`/Users/mac/WorkBuddy/Worktrees/b2b-ai-support-platform/master-376d2f63`
  （detached HEAD `90d8eec`）。本分支不触碰该工作区。

## 3. 与目标契约的差异

### 3.1 `ChatTask.CLASSIFY` / `llm_model_classify`：配置入口存在，调用链不存在

任务单称"现有 CLASSIFY 模型配置入口已存在，先核对实际调用链"。核对结果：

- 存在：`config.py:93` 定义 `llm_model_classify`；`factory.py:77-91` 定义
  `ChatTask.CLASSIFY` 与 `chat_model_for(task)`。
- **不存在**：全仓库 `chat_model_for` 的调用点仅出现在
  `apps/api/tests/unit/llm/test_model_routing.py`；生产代码零调用。

处置：T01 把它接上——语义分类走 `chat_model_for(ChatTask.CLASSIFY)`，保持
`llm_model_classify` 未配置时回落 `llm_model` 的既有语义，不修改该函数本身。

### 3.2 结构化输出：协议是文本生成，不是原生 JSON Schema 模式

`ChatProvider.complete`（`llm/provider.py:98-105`）返回 `ChatResult.text: str`，
无 `response_format`、无 schema 参数。`GiteeAiClient` 是 OpenAI 兼容文本接口。

处置：按任务单要求，**在协议外做严格 Pydantic 校验**，不扩展 provider、不声称原生
结构化输出。模型输出一律视为不可信输入经 `semantic/validator.py` 拒绝或降级。

### 3.3 写工具目录中不存在"改地址/补发票"

`tool_gateway/selector.py` 现有写工具仅 `jira.create_issue`、`linear.create_issue`、
`im.send_notification`。`crm.update_account` 被**故意排除**，注释（146-157 行）说明原因：
该工具接受自由 `fields` 补丁，从自然语言推导补丁需要模型，而平台规则要求确定性代码决定
写入外部系统的内容。

**这与 spec §1 第 4 点完全一致**：当前目录不支持的地址修改/补票写动作必须显示"待人工"，
不得虚构工具或已执行结果。R1 不新增这两个写工具。T05 实现为：语义可识别该需求 → 生成
`needs_human` 任务并写明阻塞原因，不进入提案。

### 3.4 提案/确认路径已完整，可直接复用

`tool_gateway/gateway.py` 已实现 propose → confirm → execute → verify 四段，
`action_hash` 绑定确切参数（151-158、305 行），确认过期与执行幂等齐备。
`POST /v1/tool-proposals` 已有 propose/confirm/execute/list/get 路由。

处置：T05 的写任务只调用既有 gateway，**不新增绕过 Gateway 的快捷接口**。

### 3.5 租约接口已提供 CAS 与发送前复核

`identity/lease_service.py` 现有 `lease_snapshot`、`workbench_transition`（CAS
`expected_version`）、`assert_can_send`（发送前复核 owner=AI 且 version 相符）、
`current_owner`、`LeaseConflict`。

处置：T04/T06 直接复用，不新增租约机制。

### 3.6 权限动作沿用现有 Action 枚举

`platform_policy.Action` 已有 `CASE_READ` / `CASE_UPDATE` / `TOOL_READ` /
`TOOL_WRITE_LOW` / `TOOL_WRITE_CONFIRMED` / `TOOL_HUMAN_APPROVAL`。spec §6 的四条新路由
全部可映射到既有动作，无需扩权。

### 3.7 迁移编号与 RLS 样板

下一可用序号 `0055`。新表样板以 `0047_canned_replies` 为准：`tenant_id` 非空、
`GRANT … TO platform_app`、`ENABLE` + `FORCE ROW LEVEL SECURITY`、
`tenant_isolation` policy 用 `NULLIF(current_setting('app.tenant_id', true), '')::uuid`。

### 3.8 spec 建议名与仓库现状的命名差异

spec 建议 `SemanticUnderstandingService.analyze(context)`。仓库既有风格是模块级函数 +
显式参数（`intent.classify(question)`、`lease_service.lease_snapshot(session, ...)`），
服务类只在持有会话时使用（`ToolGateway`）。

处置：语义侧提供 `semantic.service.analyze(...)` 协程函数，`SemanticUnderstandingService`
作为薄封装仅在需要注入 provider/预算时使用。**不为了贴合 spec 文字而改变仓库既有风格。**

## 4. 开关与默认

新增 5 个租户 Feature Flag，全部默认 `False`，走既有 `knowledge.flag_service`
（确定性哈希灰度 + kill switch 优先）：

| Flag key | 作用 |
|---|---|
| `agent.semantic_shadow` | 异步影子分类，仅评估记录 |
| `agent.semantic_assist` | 坐席可见候选任务/摘要/建议 |
| `agent.semantic_read` | 批准租户内的只读工具选择 |
| `agent.conversation_tasks` | 会话任务表与状态机 |
| `agent.copilot_generate` | 副驾异步生成 |

进程级 kill switch：`APP_SEMANTIC_ENHANCEMENTS_ENABLED=false`（默认 true 仅表示
"未被租户开关打开时不工作"；置 false 可立即阻止一切新增强工作）。

## 5. R1 明确不做

- 不新增写工具（改地址/补开发票归 R3 连接器交付）。
- 不做情绪趋势/优先级/流程模板/知识缺口追踪（R2）。
- 不做 ERP/CRM 联通、售前选型、询价交接（R3）。
- 不为通过验收而降低门禁、删除失败测试或把 mock 结果写成生产结论。
- 不自行购买服务、不上传真实客户数据、不扩大付费调用范围。

## 6. 验收就绪声明

本文件不预填任何验收结论。真实模型质量门禁（EVAL-02）、真实 PostgreSQL RLS 负例
（SEC-01）、生产性能（PERF-01/02）在缺少真实凭据与生产拓扑时标记**阻塞**，
不以 fake provider 结果代替。
