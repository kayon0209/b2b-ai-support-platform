# R2-02 四种标准服务流程目录

状态：目录、租户/坐席能力判定与默认关闭的会话任务入口已接入。坐席可将模板绑定到当前会话最新客户消息，记录客户来源字段、取消或转人工。`invoice_application` 可在服务端从当前会话关联 Case 唯一核验客户账户后，准备受 Tool Gateway 确认的**平台内部申请工单**；这不执行开票。其它模板仍停在人工接续，外部业务 executor 未接入。

## 当前目录

下表描述模板声明的目标能力；它不表示这些工具已由标准流程实例调用。实例目前只做人工字段收集和转交。

| Key | 场景 | 当前可用边界 |
|---|---|---|
| `order_status` | 元器件采购、PCB/PCBA 订单进度 | 仅使用当前已注册的 `order.get_status` 只读能力；`shipment.track` 是可选补充。缺少订单查询能力时转人工；物流事实缺失时不推断发货/签收。 |
| `repair_quality_intake` | PCB/PCBA/元器件报修与质量问题 | 使用 `case.create` 建立内部工单；要求客户/产品来源核验、质量负责人组配置，并经过已有 Tool Gateway 确认。受理不代表已维修或完成质量判定。 |
| `invoice_application` | 发票申请、变更或问题登记 | `billing.get_invoice` 只读；在 customer account 由已有 tenant Case 关联且无歧义、order_id/invoice_type 已记录时，可用 `case.create` 准备待确认的内部申请工单。Tool Gateway 确认执行后才报告内部工单已核验创建；**不会开票**。账户未关联或冲突时停在人工接续。税号标记敏感，不写进普通任务日志。 |
| `technical_escalation` | 制造工艺、产品规格和技术问题 | `case.create` 仅创建已确认的内部升级工单；需配置工程负责人组。模型不能生成未经核验的工艺参数、制造承诺或工程结论。 |

## 模板字段

实现位置：`apps/api/src/platform_core/agent_runtime/tasks/standard_flows.py`。

每个版本内置模板声明业务线、intent code、必填/选填槽位、槽位来源、敏感字段、只读工具白名单、受控写白名单、负责人组、确认要求、部分完成、超时、取消和人工退出条件。当前实例不运行语义模型；必填 customer-source 字段由当前坐席从对话整理录入，任务行保留 `agent_collected` 来源。敏感字段按现有策略 withheld。录入值只用于人工接续，不能被误作已验证字段或 Tool Gateway 参数。

`GET /v1/workbench/standard-flows` 要求 `case.read`，返回四个模板及当前租户/坐席的可用状态，并返回 `instances_enabled`。能力判定只表示目录配置，不表示流程 worker 可执行。发起入口要求新 flag `agent.standard_flow_instances` 明确开启（默认 `false`）。

Workbench「任务」页展示可展开的流程卡片和“发起到当前会话”按钮。`POST /v1/workbench/conversations/{conversation_ref}/standard-flows/tasks` 要求 `case.update`、当前人工 owner、匹配的 lease version 和 `Idempotency-Key`。服务端锁定并复查 lease，从同租户/同会话读取最新 customer turn；浏览器不能传 tenant、actor 或 source turn。持久化 task 带 `flow_key`/`flow_version`，请求 receipt 只存幂等键 hash 与请求 hash，并受 FORCE RLS 保护。

所有模板实例初始状态为 `manual_flow`，该状态不属于 `can_progress()` 调度集合。必填 customer-source 字段由当前坐席记录并保留 `agent_collected` 来源；verified-business-record/human-review 字段不能由普通文本字段冒充核验值。订单查询、质量受理和技术升级仍不生成提案。

发票流程是受限例外：`cases.service.verified_account_for_conversation()` 只在所有关联 Case 都指向同一个非空 tenant account 时返回账户。补齐客户字段后，具有 `tool.write.confirmed` 权限的支持管理员/租户负责人可从 `manual_flow` 直接准备参数绑定的 `case.create` 提案，不经过 `ready`；task 才进入 `awaiting_confirmation`。普通 `support_agent` 看不到准备按钮，服务端也会再次鉴权。这只创建平台内部申请工单，绝不代表外部发票已开具。确认和执行沿用现有审批页；Gateway 经 postcondition 验证后把 task 更新为 `succeeded` 并记录 receipt。`cancel`/`handoff` 会先在 ToolProposal 行锁下撤回未执行提案，与并发 execute 串行；已开始或已有最终执行结果的提案不能伪装成已取消。工具结果 `failed`/`unknown` 分别映射到失败/待对账状态。

## 安全边界

- 模板目录不直接读外部系统、调用模型或执行写入。
- 写任务必须保持现有 `ready → awaiting_confirmation → executing` 流程；成功态仍需真实 postcondition 或人工记录。
- 外部写能力没有 connector 和正式 schema 时，流程必须是 `needs_human`。发票申请目前就是这种情况。
- 流程实例 flag 默认关闭；即使显式开启，目前仍是人工接续任务，不代表支持外部查询/写入。
- 普通 flow 字段采集不能填入 `customer_account_ref`、`product_ref` 等 verified-source 字段；必须补齐服务端业务记录核验接口后才可解除阻塞。
- `invoice_application` 只允许准备平台内部 `case.create`，账户必须从唯一一致的现有 Case 关联解析；confirmation 只授权创建该 Case，不能授权开票。
- `unknown` 只能进入人工对账，不能作为失败后盲目重试信号。
- 缺少业务负责人时不靠模型猜测队列，直接显示 `FLOW_OWNER_UNASSIGNED`。

## 本地验收

`test_standard_flows.py` 与 `test_standard_flow_catalog.py` 覆盖四模板完整性、registry 白名单、坐席权限、租户 RLS、active Connector、活跃负责人组、订单只读和可选物流、发票外部写能力缺失，以及技术升级不伪称完成。`test_standard_flow_instances.py` 覆盖 server-resolved turn、lease owner、默认关闭、幂等/跨租户/RLS、字段来源、支持角色限制、唯一账户解析、发票内部提案、确认执行后的 task receipt、撤回与并发执行状态。真人浏览器/辅助技术验收和其它三种 flow 的真实 executor 仍待实现。
