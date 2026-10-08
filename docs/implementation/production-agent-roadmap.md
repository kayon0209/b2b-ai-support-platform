# 企业客服 Agent 生产化阶段清单

基线：`origin/master` / `081a7ae`，2026-10-05。工作区：隔离 worktree。

状态含义：**已具备**代表主线有实现；**待补**代表代码路径尚未接通；**待验收**代表需要真实模型、获批数据、生产拓扑或外部系统证据。合成测试不算生产验收。

清单标记：`[x]` 已实现且验证；`[~]` 部分实现；`[ ]` 未开始；`[!]` 外部条件阻塞。运行 `scripts/roadmap_progress.py` 可显示当前加权进度；该百分比按清单项计算，不代表时间或工作量。

执行授权：阶段一完成并达到本清单验收要求后，直接开始阶段二，不需要再次询问。
实时清单查看：`python3 scripts/roadmap_progress.py docs/implementation/production-agent-roadmap.md --phase 1 --watch`。

## 当前进度（2026-10-08）

阶段二代码与本地合成验收已通过：真实 API 与 Worker 子进程 SIGKILL 恢复、外部写入 UNKNOWN fail-closed 与人工对账、跨层重试预算、权限撤销竞态和 case 专用补偿均有实现及测试。Jira search 修复后再次在全新隔离 PostgreSQL 上运行 142 项 API/RLS/Worker/Gateway/迁移/Outbox 预算/告警集成，全部通过；`platform_app` 确认为非 superuser、非 BYPASSRLS，连接关闭后临时库删除。Workbench TypeScript 与组件测试也通过。阶段二实现清单 100%，阶段三、四本地清单也已完成；四阶段路线图加权清单当前为 100%。独立人工 holdout、签名 CI provenance、获批模型调用及真实流量校准仍是生产准入门槛。

阶段三 rules-only baseline 使用冻结的 semantic-v2 synthetic holdout，340 条 case 生成 340 条 DeepEval 本地 trace；intent exact-match 为 25.88%，完整路由 exact-match 为 11.18%，规则耗时 p50/p95/p99 为 24/68/78 ms。本轮模型请求数为 0，报告明确标为不可用于生产质量声明。新增同案例 DeepEval golden 将 route、exact-span receipt citation 和真实 Gateway task trajectory 聚合为完整 task-quality record，三项本地 metric 为 1.0；它是合成 fixture，不是一般自然语言 entailment 或独立人工 holdout。线上客户结果采集、分层人工复核、版本化 hash evidence 已接入 Prompt promotion 强制门禁；门禁要求最近 30 天的候选版本证据、总体至少 30 条或小总体 census、每活跃分层至少 5 条或该层 census、无安全类 override、整体加权 override ≤10%。未发布 Prompt 的 A/B 曝光在所有启用实验间累计封顶 10%，tenant mismatch 会被拒绝。旧轮次本地验证见阶段三交付记录；本轮最终变更验证按下方阶段四证据执行。

阶段三、四遗留的本地评测/演练项已补齐：同案例 semantic route + exact-span citation support + verified task trajectory 的 fresh-isolated-PostgreSQL DeepEval golden 1/1 通过，local fake-provider 主模型 outage → 显式 fallback 测试和 fallback 未配置时不重试测试通过。API/unit、contracts 和仓库级 `tests/unit` 全量单测也通过。四个阶段本地加权清单均为 100%；通用 claim entailment、真实主/备用模型故障演练、独立人工 holdout 和获批 provider 校准仍需外部证据。

**阶段一和阶段二代码与本地合成验收完成。** Worker 现只把租户活动连接器实际能解析的、schema 完整且任务风险类型匹配的工具交给多意图规划器；只读工具不会附到写子任务。Gateway 写操作前会实时重查条件；父任务回执会持久推进无条件子任务或在可证明条件为假时跳过。TaskPanel 可显式执行按 schema 唯一绑定的只读语义工具，经 Tool Gateway 留存核验回执；同 key 重放不重复执行，catalog 风险变化后会在提案前拒绝。阶段二已为同会话 Inbox 事件增加 FIFO claim；队列请求将 `agent_run_id` 精确写入事件；新消息在旧结果生成期间被接受时，旧结果在发送前标记 superseded；linked-task proposal 现在持久保存 lease version，confirm/execute 会在 side effect 前重查。Agent Runtime/Tool Gateway 单测 666 passed；正确使用 `platform_app` RLS 角色的阶段一/二相关 PostgreSQL 集成 118 passed；Admin Web typecheck、组件 tests、Ruff、Mypy 通过。同一会话的真实 Worker 任务已在桌面浏览器执行只读 Demo ERP 查询，并在 PostgreSQL 留下 `executed/verified` 回执。该证据是本地合成验收，不代表真实 ERP 生产认证。

阶段一的同数据浏览器旅程已完成：脱敏客户 turn → 持久 task-planning outbox → Worker → Workbench TaskPanel → Gateway → PostgreSQL/RLS。写子任务因归属证明不足保持人工处理，没有外部写入。Docker 与 PostgreSQL 使用独立临时数据库；现有开发库未迁移。

## 阶段一：多意图任务闭环（代码验收完成）

- [x] 将任务分析从客服请求事务移入持久队列；Worker 恢复陈旧 claim；将任务持久化到租户范围任务表。
- [x] 任务状态通过 Gateway 回执更新；只有验证完成证据才可标记成功。
- [x] 执行入口检查依赖完成情况；订单条件以已验证且账户/订单匹配的历史回执作提案前判断，并在 Gateway 确认后重新读取、再次验权和核验账户/订单，再允许写执行。其它来源和不确定读均 fail closed；集成测试覆盖读后状态变化时零写调用。
- [x] 任务列表按父任务回执动态显示 blocker；父任务成功后持久清除无条件依赖，条件已核验为假则撤回提案、终止并记录 skipped 事件；条件为真仍需执行时实时重查。
- [x] 每个子任务按已授权 schema 唯一绑定工具并保留参数来源；Gateway receipt 关联 proposal/execution，工具歧义、跨类型或 schema 不匹配时 fail closed；写操作继续经过人工确认。
- [x] 同一合成会话数据走过 customer-turn 持久化、task-planning outbox、Worker、TaskPanel、Gateway 和 PostgreSQL；桌面浏览器执行只读查询后见到 `SO-9002 · shipped` 已核验回执。自动化集成覆盖 RLS、重复投递、风险拒绝和回执关联。

当前实现已在命令入口核对同租户、同会话、同来源轮次的依赖，并可从已验证的 `order.get_status` 回执判断条件是否满足。只有客户明确提供的订单号且回执账户匹配会话账户时才采信读取结果。其它工具、缺少 owner proof、未知状态、多个条件来源或无法解析的条件均拒绝。父任务成功后，子任务会持久推进或按已核验的 false 分支跳过。条件 true 时，Gateway 在确认、权限、过期与幂等检查通过后重读订单，再核对条件和 owner proof，最后才调用写执行器。

真实企业连接器的 ETag/版本条件写演练仍是**生产准入外部门槛**：当前仓库未配置客户 ERP 沙箱，演示适配器标记为 `source=demo`；在真实连接器具备原子 `If-Match`/版本条件写并通过竞态演练前，不启用依赖该边界的外部写操作。此门槛不阻塞不依赖真实 ERP 的阶段二代码工作。

## 阶段二：恢复、并发和人工审批（代码与本地合成验收完成）

- [x] 同会话采用 FIFO；Ingress 和发送前过期检查通过事务级 advisory lock 线性化；Worker 对较新请求只在旧事件完成后取件。若新消息在旧响应生成期间被接受，旧结果在发送前 supersede；人工接管会 supersede queued run，预发送租约检查阻止正在运行的旧回复。测试覆盖同会话事件顺序、旧答案零发送和接管竞态。
- [x] 待补字段任务、审批提案、确认和 `proposal_lease_version` 持久化在 PostgreSQL；linked-task 的 Gateway confirm/execute 锁定并重验 current human lease/version，stale 时在新增 ToolExecution 前 fail closed。真实 API 子进程被 SIGKILL 后重启，集成演练覆盖待补字段恢复、待审批提案恢复、确认状态跨重启保留，以及 lease version 移动后 409 拒绝且零执行记录。
- [x] Tool Gateway 已在 provider call 前提交执行意图；重复 execute 不删除 EXECUTING 记录或重发。超出工具窗口后转为 UNKNOWN；新增租户隔离、append-only、幂等的人工对账 API，可在原执行记录上记录 applied/not_applied/unresolved，并且不调用 executor。Workbench 提供人工证据录入和对账历史；未知结果不自动重放。此处的本地安全闭环已完成，具体 provider 自动查询仅在具备可验证的操作键语义时启用。
- [x] AgentRun 的模型、连接器和出站请求共享每轮 attempt/deadline budget；Inbox 首次执行时间与 claim 次数持久化，最多 3 次 claim，逻辑 deadline 跨重启不刷新；单次处理最多 11 次外部请求。task-planning/shadow 最多 3 次、每次最多 1 次模型请求，其首次 claim 时间和绝对 deadline 跨 Worker 重启保留。普通 Outbox 的总调用 cap 与绝对 deadline 跨 5 次安全重试共享，不可安全重放事件失败即停。Copilot 每个 durable claim 最多 1 次模型请求、固定草稿 TTL 截止且最多 3 次 claim；release-evaluator 将模型/embedding 共用外部请求上限分配给三次 durable attempt，首次 claim 写入绝对 deadline 和 attempt cap，超时后不刷新。最新隔离 PostgreSQL 集成 142 项通过。预算、跨重启固定期限和安全重试策略的代码验收已完成。
- [x] 已用真实 Worker 子进程提交 Inbox claim 后 SIGKILL，再由替代进程回收并重新领取同一事件；两个 OS Worker 同时竞争 PostgreSQL `SKIP LOCKED` claim 时目标 InboxEvent 只被一个取到。真实 API 在假 provider 接受写入后 SIGKILL，重启后同一 proposal 转 UNKNOWN、返回 409 且 provider 只收到一次，随后人工对账可关闭原执行。通用 Outbox 已拆分 owner metadata claim 与 tenant-bound app-role handler，外部回复 unknown 不会自动重送。确认和执行会锁定当前 membership 并重评权限；撤权先持锁时请求等待后拒绝且零执行；慢的执行前检查越过 confirmation expiry 时也会在创建执行意图前拒绝。接管旧回复的发送前租约竞态已有测试。最新隔离库 API/RLS/Worker/Gateway/迁移/Outbox 预算/告警集成 142 项通过。
- [x] 定义并实现唯一安全可逆动作 `case.create.close_unmodified`：当前负责人提出原因码后，只能关闭仍为 `NEW/version=1` 的原工单，保留记录并使用 CaseService 状态机；后续已改动的工单 fail closed，追加失败补偿记录和审计，发出有界 Prometheus 指标并由 `PlatformToolCompensationFailed` 告警。审批页展示补偿入口与历史。Jira、Linear、CRM、通知和放行确认明确没有通用撤销，不自动反写；操作员走外部系统对账或业务流程。PostgreSQL 集成覆盖成功、重复请求、幂等冲突、后续改动时拒绝，告警规则也被验证。

当前已有 outbox Worker、stale claim 回收、乐观版本控制、幂等和人工控制租约。多主机部署及外部 provider 认证仍需生产拓扑验收，见文末外部准入门槛。

## 阶段三：任务级评测和线上指标（本地代码与门禁闭环完成）

- [x] 冻结 1,700 条 synthetic semantic-v2 数据集，按对话家族分层成 60/20/20 dev/validation/holdout，并记录 hash、schema 与 prompt 版本；首轮 340 条 holdout 已评测。报告保留 synthetic provenance 和生产声明禁用标记。
- [x] 实现同案例 end-to-end trajectory scorer，并以真实 API/Worker/Gateway/PostgreSQL 合成集成样例验证最终 task 状态、顺序工具调用/参数、权限决策、verified receipt 关联和最终业务状态；DeepEval trace 同时执行 trajectory 与 task-quality record 指标。
- [x] 新增同一 `case_id` 的 task-quality 汇总契约，将语义、QA citation_support、轨迹检查合并前校验样例身份；失败优先于缺失，缺少必需证据标为 incomplete。fresh isolated PostgreSQL DeepEval golden 将 semantic route、verified receipt 的 exact-span citation support 与真实 Gateway 任务轨迹合并；每个组件的同案例证据都通过才记为 passed。该 fixture 不等于通用 claim entailment；真实业务 holdout 仍属外部数据门槛。
- [x] semantic-v2 报告输出 p50/p95/p99、queue wait、token 数、model attempt/retry 数和按配置价格估算的总成本，报告每个 exact-intent match 的平均 token 用量；新增按已验证成功业务目标计算的 token 成本估算。provider 未返回的失败请求账单明确保持 unavailable。
- [x] 将客户 outcome 接入租户范围的线上事件采集；明确记录确认问题展示和客户 yes/no 回答，指标区分确认、拒绝、等待、成熟后无回复及同联系人/同 Case 的可验证重联。无回复不会被算作确认；无链接观测不冒充“没有重联”。
- [x] 将分层人工复核接入线上 API、reviewer 身份与 `case.review` 授权、审计和幂等追加记录；按 `population/selected` 加权 override 率只在所有活跃层都有样本且样本全部审核后可用。批次可限定租户内不可变 Prompt 版本，终结证据含确切 Prompt/code/policy 版本范围及哈希，不存 Prompt/回答文本。
- [x] 将版本化人工审核证据接入候选 Prompt promotion；服务端在 tenant-bound transaction 中重查同版本的最新 30 天 evidence 并验证 hash、sample completeness、stratum coverage 和 override reason。总体样本至少 30 条或人口 census，每活跃层至少 5 条或层内 census；`unsafe_action`/`unsupported_claim`/`citation_gap` 任一 override 阻断，其他原因加权 override rate ≤10%。门禁失败不改 active prompt，返回稳定 422 并写 `prompt.promotion_blocked` 审计；成功记录 evidence ID/hash、样本数、阈值。未发布 Prompt 在启用 A/B 实验中的合计曝光上限为 10%，用于门禁前收集候选运行。单测和隔离 PostgreSQL API/RLS 集成验证缺失/不完整/阈值超限拒绝与清洁样本通过。

当前外部生产准入门槛：独立人工标注 holdout、签名 CI artifact provenance、获批的被测模型调用及真实延迟/成本校准仍未通过；10% review override 与 canary 默认阈值须用获批流量复核。合成或 self-reported CI evidence 不能替代这些验收。

## 阶段四：记忆与检索验收（本地代码与合成验收完成）

- [x] 记忆按租户、渠道联系人、会话和类型隔离；ConversationTask 保留在独立的操作状态表。会话摘要只按请求确定性生成，不落库；支持对话不缓存 query embedding，检索结果集不缓存。90 天 retention sweep 与 TENANT_ADMIN 的 contact-memory erase 同时清理事实和关联 redacted turns，并在同一事务写审计。
- [x] 长期记忆只允许明确表达的 allowlist 事实/偏好，保存 source customer turn、来源时间、revision 和 expires_at；新旧冲突按来源时间做原子比较。数据库触发器在滚动发布期间继续拒绝迟到旧 turn；服务层及 DB key allowlist 均拒绝 permission/role 等授权事实，授权仍由当前 request context、policy 和 control lease 决定。
- [x] 当前客户问题完整保留且不被静默截断；旧轮次确定性摘要、否定约束、承诺、审批、未完成事项和 tool outcome 有显式优先级。human ownership/lease 保存在控制面，不从对话记忆推断；渲染后 Prompt 有总长度上限，超过时在模型调用前弃答。
- [x] 知识问答运行混合检索；动态业务查询先走 Gateway，验证 receipt 后直接作为本轮证据；澄清、人类请求、敏感及 out-of-scope 路径在检索前结束。
- [x] 新增中文错误码/型号、精确 ID、过期版本、tenant 隔离、撤销 ACL 后下一次查询立即失效和真实 chunk citation 用例；多 ACL grant 使用任意一条完整 principal pair 匹配，避免错误 AND 判定及 principal type/id 交叉匹配。

阶段四差异与边界：跨渠道同人只有在未来接入经验证身份映射后才可共享记忆；目前 channel-local IDs 不互相合并。Erasure 删除平台记忆，不删除外部渠道原始 transcript、provider address mapping、经验证 task history 或 append-only audit；这不是完整的数据主体擦除工作流。支持请求不缓存个人 query embedding，可能增加重复查询的 embedding 调用与延迟，仍需在获批的真实 provider/流量下校准。测试只用本地合成内容，没有真实客户数据或模型调用。

验收证据：API/unit、contracts 和仓库级 `tests/unit` 全量单测通过；新建隔离 PostgreSQL 上 61 项 Memory/Retention/RLS/Hybrid retrieval/ACL/identity HTTP/Channel webhook/业务读集成通过，另有同案例 DeepEval 1/1 golden 通过。连接角色为非 owner `platform_app`、NOSUPERUSER、NOBYPASSRLS；确认临时数据库无连接后已删除。Ruff check/format、Mypy 15 个源文件和 `git diff --check` 均通过。

## 本轮进展

- Tool Gateway 在调用外部适配器前先提交执行意图；进程中断留下 EXECUTING 时，重复请求只返回处理中，超过工具 deadline/grace 后转为 UNKNOWN，绝不删除记录后盲目重发。新增 append-only 的租户级对账记录和 API：当前会话负责人可提交外部记录/查询引用，标记 applied、not_applied 或 unresolved；按请求幂等键回放不重复记账或执行。40 项新鲜 PostgreSQL API/RLS/迁移往返集成通过，Admin Web typecheck 与组件测试通过。此处是人工核验入口；真实连接器自动查询 provider operation key、原子条件写仍需适配器和沙箱验收。
- 新增共享 ExecutionBudget：AgentRun 内模型/工具/出站请求共用 deadline 与 attempt 上限，预算摘要写入 run lineage；Inbox 首次 claim 时间和尝试数跨 Worker 重启持久保留，task-planning/shadow/outbox 各自有总尝试和每次调用上限。普通 Outbox 已改为 owner 只领取 metadata 并提交 claim，handler 在 tenant-bound platform_app session 运行；外部回复 unknown/crash 停放，不自动重复发送。真实 API 假 provider 接收后 SIGKILL 集成验证新进程不重发；本轮 105 项隔离 PostgreSQL API/RLS/Worker/Outbox/Gateway/迁移测试通过并清理临时库。
- 新增 Tool Gateway 权限撤销竞态保护：确认与执行在 tenant RLS session 中锁定 actor 的当前 active membership 并重新评估对应 action；锁冲突导致 repeatable-read 快照失效时映射为 409 `AUTHORIZATION_STATE_CHANGED`。该锁与执行意图同一事务，意图提交前先完成 permission gate；之后撤权无法召回已越过 no-return point 的 provider request。审批和提案 expiry 在慢的 live guard 后重新检查。新鲜隔离 PostgreSQL 定向 API/RLS/Gateway 集成 **61 passed**，包含撤权先持锁时 0 ToolExecution/0 adapter call；确认过期单测也断言 0 dispatch。Ruff 对 54 个改动 Python 文件通过，Mypy 262 个源文件通过。
- 新增业务专用人工补偿：当前人类负责人可对已验证的 `case.create` 调用 `close_unmodified`，固定版本/状态前置条件阻止覆盖后续 Case 工作；成功用 CaseService 关闭工单而非删除，失败结果 append-only、审计并触发 Prometheus 告警。Workbench 显示可用原因和补偿历史。其他外部写入保持 no-return/reconciliation。迁移 `0075_tool_exec_comp` 为记录表启用 FORCE RLS，并仅授予应用角色 SELECT/INSERT。
- release-evaluator 的外部模型/embedding 调用现在共享 ExecutionBudget；每条 outbox job 在首次 claim 持久化绝对 deadline 与总请求上限，再按最多 3 次 durable delivery 分配剩余上限。Worker 重启、改配置或 stale reclaim 不会刷新该 job 的 deadline/cap；过期事件进入 FAILED 并留下稳定错误码。迁移 `0076_outbox_job_deadline`；PostgreSQL 集成验证 retry claim 复用原 deadline 和 call cap。
- 普通 Outbox、task-planning、shadow 与 release-evaluator 现共用 OutboxEvent 持久化的首次 claim 时间、绝对 deadline 和总 external-attempt cap；普通 relay 按 5 次安全投递切分最多 20 次请求，task-planning/shadow 按 3 次恢复尝试切分 3 次模型请求，release-evaluator 的模型/embedding 请求共享 600 次上限并按 3 次尝试分配。过期 queued/stale claim 转 FAILED；无法安全重放的普通事件在首次错误或 hard-exit 后停放。隔离 PostgreSQL 集成覆盖预算跨重试不刷新、5 次模拟重试恰好 20 次调用和 deadline 过期零 handler 调用。
- 第一阶段初始审计曾发现依赖自动推进缺失；后续已补齐父任务 verified receipt 驱动的无条件推进和已核验 false 条件的 skipped 分支，当前状态以阶段一清单和下方完成证据为准。
- 订单条件依赖现由应用层和 Gateway 双重保护：缺少依赖时拒绝命令并审计；订单回执须为风险 read、后置验证通过、订单号和会话账户匹配。条件为 true 时在写执行前实时重读并重验权限；状态改变、其它来源或无法证明时 fail closed。其它业务域需各自拥有可信读取/条件契约，不会复用订单规则。
- 新增确定性 task-to-tool 绑定：仅从服务器过滤后的候选工具中按 task kind 和输入 schema 匹配；唯一匹配时持久化 `server_capability` 选择标记，缺失必填参数由 schema 补为待收集字段；多个工具歧义、工具不匹配或模型伪造 `tool` slot 时转人工，不猜测、不执行。
- 新增依赖闭环：父任务 verified receipt 到达后，ready 子任务在同一事务内持久推进或因已核验 false 条件跳过；条件 true 的写 proposal 在 Gateway 实际执行前单独执行一次有审计的 verified order read。新集成用例覆盖读结果改变后拒绝已确认写提案、且写 executor 调用为零。
- 新增 Workbench 语义只读工具执行：UI 只为服务端 schema 唯一绑定的只读工具显示显式执行入口；API 重查 catalog 风险、要求 `TOOL_READ`、通过 Tool Gateway 执行并将已核验回执摘要及 execution id 链接到任务。PostgreSQL 集成验证成功回执、同 key 重放只产生一条 execution、以及工具风险变为写时零提案拒绝。
- 阶段一同数据浏览器验收通过：Worker 生成的 3 个子任务由桌面 Workbench 显示；坐席执行只读订单工具后，`status=executed`、`verification_status=verified`、`source=demo` 回执写入 PostgreSQL。写任务因 account-owner proof 不足继续人工处理，没有外部写入。
- Agent Runtime/Tool Gateway 全量单测 666 passed；正确使用 `platform_app` 的阶段一/二 PostgreSQL/RLS/审批选定集 118 passed；Ruff、Mypy、Admin Web typecheck 和组件测试通过。真实 ERP 沙箱与原子 ETag/版本条件写仍是生产准入门槛；本地 Demo 证据不计为真实集成认证。
- 阶段二同会话顺序/旧结果过期、linked-task 重新核验 lease version 和跨 TestClient 恢复均有 PostgreSQL 测试；新增 `0072_task_proposal_lease_version` 可逆迁移及生产量级迁移往返通过。选定集正确使用 `platform_app` 非 owner 连接，118 个 PostgreSQL/RLS/审批集成用例通过。
- 本轮恢复验收已升级为真实进程故障演练：API 子进程在持久化待补参、待审批及已确认提案后被 SIGKILL；新 API 进程从 PostgreSQL 恢复状态，租约版本变化后 Gateway 返回 `TASK_LEASE_STALE` 且执行记录数为 0。Worker 子进程提交 Inbox claim 后被 SIGKILL，替代进程回收陈旧 claim 并重新领取同一事件。全新临时 PostgreSQL 已迁移到 head；`platform_app` 验证为 NOSUPERUSER NOBYPASSRLS，API/测试角色通过 `APP_DATABASE_APP_URL` 与 `APP_TEST_DATABASE_URL` 使用该角色；跨租户队列记账仍走 Worker 专用 owner bookkeeping session。API 标准流与 Worker 恢复集成共 35 项通过；确认无连接后删除临时数据库。
- Provider 自动对账评估（2026-10-07）：Jira 创建 issue 的接口允许随创建设置 entity property，但按 property 做 JQL 查找需要租户安装/配置 Forge `jira:entityProperty` 索引；entity properties 还可由有权限的用户或其它 app 修改。Linear 的公开 `issueCreate` 用例没有 create 幂等键；其 URL 幂等说明只适用于已知 issue ID 下的 attachment。当前不把 provider 查询零结果当作 `not_applied`，也不自动重放 UNKNOWN；保留人工证据对账。依据：[Jira entity properties](https://developer.atlassian.com/cloud/jira/platform/jira-entity-properties/)、[Jira create issue API](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/)、[Linear GraphQL](https://linear.app/developers/graphql)、[Linear attachments](https://linear.app/developers/attachments)。
- Jira `search_issues` 已修正为将 JQL 放入 GET query parameters，并把项目键和搜索文本作为转义后的 JQL 字符串字面值；旧测试只检查响应 body，未检查 query 参数，因而漏掉请求 filter 丢失。新增 wire-level 子句注入回归测试：修复前稳定失败，修复后通过；整个 adapter unit 文件及 Ruff 检查通过。依据：[Atlassian JQL text search](https://support.atlassian.com/jira-software-cloud/docs/search-for-work-items-using-the-text-field/)。
- 全阶段最终单测复核发现两个此前无测试捕获的契约问题并补上回归：Outbox stale-claim 日志字段不在结构化日志 allowlist，已改为明确的计数字段；Channel webhook 的 GET/POST 共用 operationId，已拆分为唯一 OpenAPI operationId。全量单测和唯一 operationId 契约测试通过，OpenAPI 不再产生重复 ID 警告。
- 跨阶段复核刷新了 `TODO.md` 中已过期的 Agents 路由、AgentRun terminal version、Workbench Dialog、rate limit、Redis outage、Run lineage 和 agent rerun 状态。API Docker 镜像现已真实 build 并通过 7 项产物/runtime probes；镜像只在本地生成，未推送或部署。Prometheus Operator、集群 proxy/headroom、真实模型/IdP/provider/ERP 与 distributed load 仍是独立外部/运维 backlog。
- 2026-10-08 TODO 本地闭环：Workbench/会话分页深链接、全 Admin 数据页 skeleton、API database `/readyz` 和认证 Case 查询数据库断连 fail-closed 注入、标记 contract suite、Worker 私有 `/metrics`/ServiceMonitor/网络策略/availability alert、AgentRun 人工 rerun API + 确认 UI + SHA-256 幂等回执均已完成。新增可逆迁移 `0080_agent_run_rerun_idempotency`。新建随机隔离 PostgreSQL 的 Worker drill 97 passed，含 0080 downgrade/upgrade、`platform_app` 会话路径、AgentRun rerun 重试复用、Worker SIGKILL 接管及重复投递只记账一次；临时 Compose 项目/卷和数据库已清理。全量 API/unit+contract pytest、Admin Web tests/typecheck/build、272 个源文件 Mypy、Ruff check/format、API image 7 probes 均通过；全量单测保留一个上游 Starlette/httpx deprecation warning。
- TODO 中仍未闭环的条目已限定为需生产集群/产品范围的部分：Prometheus Operator 与真实 OTLP receiver、数据库连接 headroom、Ingress trusted-proxy 实值、跨租户公平排队及多主机负载校准、泛化业务 artifact graph，以及含外部附件字节/客户数据的合规导出范围。未将本地合成验证写成生产验收。

## 外部生产准入门槛（不计入阶段代码清单）

- Provider-side 自动幂等查询仅对具备稳定、可核验 operation key 的适配器启用；Jira 当前需要客户安装/配置 Forge property index 才能按 entity property 查询，Linear 公开 issueCreate 没有 create 幂等键保证。没有精确命中时仍须 UNKNOWN/人工对账，不允许盲目重放。
- 真实 provider 延迟/成功率预算校准需要获准 sandbox 或线上流量；当前固定上限是安全默认值，不是生产 SLA 校准结果。
- 真实 ERP 沙箱以及原子 ETag/版本条件写仍是独立生产准入门槛；本地 Demo 和合成测试不能替代该验收。
