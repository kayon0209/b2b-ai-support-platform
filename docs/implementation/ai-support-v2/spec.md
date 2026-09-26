# R1 实施规格与接口契约

本文件为新增能力的目标契约，以下新模型、表、路由和开关尚未实现。与既有接口冲突时，以 T00 的差异记录和经评审的最终 OpenAPI 为准。

## 1. 用户旅程

客户：“查一下 SO-240918 到哪了，如果没发货就改成上海办公室，另外补一下发票。”

1. 根据已验证身份读取授权订单。不能因为客户提供订单号就判定记录属于他。
2. 提取三个候选任务；“若未发货”保存为显式依赖条件，“上海办公室”标记为缺少完整地址。
3. 查询完成后用真实结果解释可继续的任务；主动询问地址及开票所缺字段。模型不可补造。
4. 地址变更仅在真实连接器声明支持并完成授权时形成待确认提案。当前目录不支持的地址修改/补票写动作显示“待人工”，不得虚构工具或已执行结果。
5. 坐席接入后看到任务、已核验事实、缺失字段、待确认动作与来源；无需重复向客户收集已确认信息。
6. AI 建议由坐席插入、编辑、通过既有人工作台发送；草稿生成成功不等于已发送或业务已办结。

R1 必须实现该旅程的“查询 + 缺参 + 不支持动作转人工”闭环。真实修改地址/补开发票的执行属于 R3 连接器交付，不能靠 mock 计为外部业务成功。

## 2. 模块落点

| 现有位置 | 新增/扩展责任 |
|---|---|
| agent_runtime/intent.py、complaint.py、emotion.py | 规则基线与硬约束；不以模型标签跳过现有判断 |
| agent_runtime/orchestrator.py | 调用独立语义/任务应用服务，控制路由与 lease；避免继续内联大段新逻辑 |
| agent_runtime/semantic/（建议新包） | Pydantic 契约、provider 调用、校验、裁决、影子记录 |
| agent_runtime/tasks/（建议新包） | 有界任务图、状态机、补参、恢复与幂等协调 |
| llm/provider.py、factory.py | 复用 ChatProvider 与 ChatTask.CLASSIFY、llm_model_classify；为副驾按任务扩展配置 |
| tool_gateway/selector.py、registry.py、gateway.py | 能力过滤、候选校验；授权、提案、确认与执行的唯一边界 |
| identity/lease_service.py | 暴露必要的当前 owner/version 应用接口 |
| cases/service.py、support_bridge | 关联工单和消息投递，通过现有接口复用 |
| Workbench.tsx、styles-workbench.css | 任务面板、摘要、建议生成、来源展示与失败恢复 |
| evaluation/、tests/evals/ | 分类与工具选择评测、灰度报告 |
| apps/worker/ | 使用现有持久队列执行影子/副驾工作，明确处理器、重试和过期策略 |

## 3. 语义接口

输入由服务端构建：已脱敏当前 turn、最多最近 8 轮授权历史、已核验事实引用、当前任务状态、租户可用工具描述。截断顺序优先保留当前请求和待补字段；限制输入总预算 4096 tokens，保留截断原因。令牌、身份凭据和模型思维链不传入。

建议接口：`SemanticUnderstandingService.analyze(context) -> SemanticAssessment`。沿用当前场景、IntentKind、BusinessLine 枚举，新增枚举须同时改契约、评测与 UI 映射。字段至少包括：

| 字段 | 契约 |
|---|---|
| schema_version / assessment_id | 固定版本如 v1；ID 由服务端生成 |
| primary_intent / secondary_intents | 受约束枚举；禁止自由创造路由或工具名 |
| scene / business_line | 对齐现有分类体系 |
| intents[] | 最多 5 个，各含 task_kind、source_turn_id、证据位置、slots、missing_slots、depends_on、condition |
| slots | 标量或受限结构，必须带来源；区分用户明确输入、可信业务回执、待确认推断 |
| evidence_spans | 相对“发送给模型的脱敏版本”的 turn_id、字符起止；服务端检查边界，不能引用其他租户或不存在 turn |
| confidence_band | low/medium/high，只表示模型自报信号；不能作为授权或上线准确率 |
| needs_clarification | 是否缺失、矛盾或指代不明 |
| emotion_signal | 可选建议标签及证据位置；R1 不据此新增自动转接行为 |
| tool_candidates | 只能来自本次服务端提供的能力集合；含 tool_name 与参数建议 |
| rule_result / effective_decision / reason_codes | 服务端填入；模型不得指定最终裁决 |

拒绝未知字段、超限数组、深层嵌套、无效枚举、未知工具、非法证据位置、未注册条件算子。解析失败统一映射为 `SEMANTIC_INVALID_OUTPUT`，按原规则或澄清降级；不可用宽松正则猜测执行意图。

条件只允许服务端注册的字段与算子，如订单状态等于某值。不能执行模型返回的 Python、SQL、模板表达式或 URL。

## 4. 模式与裁决

| 模式 | 模型用途 | 允许影响 |
|---|---|---|
| off | 不发起语义分类调用 | 原规则行为 |
| shadow | 脱敏、授权样本上异步分类对比 | 仅评估记录；业务状态、外部工具、客户回复不得变化 |
| assist | 供坐席查看候选任务、摘要和建议 | 必须由坐席显式采纳；禁止生成时顺便发送 |
| semantic_read | 在批准租户内补充非敏感意图和只读工具选择 | 经身份、连接器、政策、Schema 与 lease 校验后执行有限读取；语义写任务停在提案/人工 |

控制方式：复用租户 Feature Flag，建议 `agent.semantic_shadow`、`agent.semantic_assist`、`agent.semantic_read`、`agent.conversation_tasks`、`agent.copilot_generate`，默认 false。冲突时权限最小模式生效并记录配置冲突。另有进程级 kill switch，可以立即阻止新的增强工作。

规则和模型冲突时：明确人工请求/敏感/索赔规则优先；已有 human、queue、closed 状态按原状态机处理；模型仅建议潜在敏感风险时先澄清或人工复核，不得扩大数据访问。R1 保留原情绪规则，同时单独记录其误报；优化词表属于独立变更。

shadow 工作入现有持久队列，使用 outbox 确保提交一致性；不可通过阻塞客户请求等待模型结果，也不可启动无生命周期管理的 fire-and-forget 任务。记录过期样本并跳过，而非无限积压。外发模型前执行数据最小化和配置校验。

模型调用建议初始预算：分类整体 deadline 2 秒（含排队后实际调用阶段的重试），至多 1 次仅对可重试传输错误重试；副驾 deadline 8 秒。该预算是待验证设计目标，需 provider 实测后记录。默认无自动模型回退；任何回退模型必须明确列入允许清单、隐私边界和成本预算。

## 5. 持久数据与状态机

建议新增以下表，均带 tenant_id、排序 UUID、UTC 时间、版本字段，并启用/强制 RLS及最小授权；最终命名与迁移编号由 T00 确认。

- semantic_assessments：run/turn/conversation 引用、规则/模型/提示版本、模式、校验状态、裁决原因、用量和耗时。普通 JSON 快照只留标签、证据位置、槽位名称/来源类型与哈希，不复制消息正文或实际地址等敏感槽位。
- conversation_tasks：conversation_ref、source_turn、kind、sequence、status、version、depends_on、受限 condition、关联 proposal/execution、完成证据引用。
- conversation_task_events：追加式状态变化、actor、reason_code、trace_id、旧/新版本；无消息原文。
- copilot_drafts：conversation_ref、actor、依据 timeline revision、lease_version、授权引用 ID、脱敏草稿内容、状态和版本。草稿是受控业务数据，不进普通日志。
- 任务必需的敏感字段：通过有租户 RLS 的专用字段/表或批准的加密字段保管；按用途返回给有权限 actor，列明保留与删除策略。与审计/指标字段分离。

所有跨表业务引用校验同 tenant；可用复合外键 (tenant_id, id) 防止错连。任务幂等唯一约束至少含 (tenant_id, conversation_ref, source_turn_id, task_local_key)，并绑定内容哈希，防止同键不同 payload 静默复用。

状态：proposed → awaiting_input / ready / needs_human；ready → executing（只读）/ awaiting_confirmation（写）；executing → succeeded / failed / unknown；支持 cancelled，终态禁止任意回退。unknown 必须先对账，不能换幂等键盲重试。succeeded 需要对应工具 verified 回执或授权人工处理证据，不能仅由 LLM 文本写入。

task version 用于乐观并发；并发执行用数据库唯一约束或事务锁保证只有一个执行所有者。工具 idempotency_key 基于稳定 task_id + action_revision，禁止用每次重试的新 run_id 重新生成。参数或目标变化增加 action_revision、撤销旧确认，重新授权。

人工接管禁止派发新的 AI 任务；派发前与客户发送前复核 owner/version。已发往外部系统的请求可能不能撤回，应明确记录在途与 unknown 状态并对账，不能宣称租约能原子撤销外部副作用。

## 6. 建议 HTTP 契约

沿用统一响应信封、错误代码及 require_write_idempotency。所有新 POST 需 Idempotency-Key；同键不同请求返回冲突，重放返回原结果。版本冲突 409，越权采用现有 403/隐藏资源 404 规则，限流 429，不可用 503。不得接受请求体 tenant_id。

| 路由（新增提案） | 输入/输出 | 权限和语义 |
|---|---|---|
| GET /v1/workbench/conversations/{ref}/tasks | 分页任务、缺失字段、版本、可用动作 | CASE_READ + 会话同租户/资源授权 |
| POST /v1/workbench/conversations/{ref}/tasks/{task_id}/commands | command、expected_version、expected_lease_version、fields | CASE_UPDATE + 当前人工 owner；命令只允许 collect_fields、cancel、handoff、prepare_proposal；不能通过此接口标记工具成功 |
| POST /v1/workbench/conversations/{ref}/copilot/jobs | kind=summary/reply、timeline_revision、lease_version、source_turn_ids；返回 job_id | 当前人工 owner + CASE_UPDATE + Idempotency-Key；新任务返回 200。相同 key/相同输入重放原 job；同 key 不同输入 409；新 key 才表示重新生成 |
| GET /v1/workbench/conversations/{ref}/copilot/jobs/{job_id} | queued/running/succeeded/failed/stale/expired；完成后给 draft 与授权来源 | CASE_READ + job 与会话资源授权；跨会话 ID 不可串用 |
| POST /v1/conversations/{ref}/replies | text、origin、可选 copilot_job_id | CASE_UPDATE + Idempotency-Key；只传 job id，不接受客户端来源列表。服务端校验 tenant、conversation、actor、timeline revision、lease version 与 source turn，过期 job 返回 409；回复行保存来源引用 |

当前部署只使用受控的默认提示词。API 保留 `instructions` 字段以便向后兼容，但非空值返回 `COPILOT_INSTRUCTIONS_UNAVAILABLE`，不写数据库、不发送给模型；恢复该能力前需审批模型目的地和坐席指令的数据边界。

新建任务、执行已确认工具的内部接口不直接暴露给访客；客户端确认与管理者审批是不同角色语义，不能借用户一句“确认”代替有权限人的批准。既有 tool-proposals API 继续做写动作提案/确认/执行，不增加绕过 Gateway 的快捷接口。

副驾使用当次授权知识和已验证业务回执；摘要只归纳可见历史并带来源 turn 引用。若新消息/owner/版本变化，旧 job 标为 stale，拒绝自动覆盖或插入；保留操作者当前编辑草稿。建议“生成/重新生成”“插入草稿”“发送”三个动作清楚分离，插入文本携带 origin 与 sources 到现有发送流程。纯手写回复仍可使用。

## 7. UI / UX

工作台沿用会话为最大区域的布局。任务以中栏顶部紧凑条或右栏任务页呈现，避免压缩中央对话成单列窄栏。每项显示客户需求、状态、下一步、缺失字段与阻塞原因；分组展示低风险读取和待确认变更。

必须覆盖：没有模型、没有连接器、无建议、排队、生成中、超时、拒绝、来源失效、租约变化、重复点击、任务部分成功、页面刷新恢复。错误后保留人工草稿；按钮禁用同时有文字解释。模型建议标为“建议”，核验回执标为“已核验”，模拟数据明显标记。

桌面 1536×1024、1280×800、移动 390×844、200% 缩放验收；页面无水平溢出。键盘可进入任务、补参、草稿与来源；抽屉/弹窗有焦点管理、Escape 关闭和回到触发点。状态不只用颜色，动态加载使用适当 live region，禁止每次轮询播报整段对话。

## 8. 观测、成本与回滚

记录语义请求量、校验失败、降级原因、规则分歧、任务状态变化、重复执行拦截、副驾过期与 token/耗时。指标标签仅用受控枚举，不带 tenant_id、conversation_id、用户文本或槽位值；租户细粒度追踪通过访问受控的业务/审计查询提供。

部署方设置分类/生成模型、单请求 token 上限、租户日预算和并发限制。缺少模型配置不调用；预算耗尽显式降级。使用用户自有凭据，禁止生成新付费账号或把假密钥当成模型验收通过。

关闭开关后停止新增强任务；待执行写提案不能因此被自动批准。已在执行的调用按结果落库或 unknown，对账后再恢复。回滚优先关闭开关与恢复旧应用；新增表采用 expand 迁移保留，禁止生产自动降级删表。备份/还原测试用隔离数据库。

## 9. 后续批次的进入条件

R2：情绪结果单独表达强度/趋势/证据，结合等待、重复联系、SLA 和客户等级形成建议；规则可解释、主管可纠正。先做查单、报修、发票申请、技术升级模板，每个模板明确支持工具与人工步骤。知识改进必须“缺口 → 审核知识版本 → 发布 → 同口径前后对比”，不从未审核会话自动学习。

R3：建立产品目录、报价/库存权威来源、ERP/CRM 的真实能力和归属验证契约。外部变更工具需独立审批/幂等/对账；售前推荐提供依据和商机交接，不生成无来源折扣或保证交期。
