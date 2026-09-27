# R2-02 四种标准服务流程目录

状态：领域目录与能力判定器、单测已实现；尚未接入 Workbench 流程卡片或生成式 planner。流程目录只做声明和可用性判断，不发起工具调用。

## 当前目录

| Key | 场景 | 当前可用边界 |
|---|---|---|
| `order_status` | 元器件采购、PCB/PCBA 订单进度 | 仅使用当前已注册的 `order.get_status` 只读能力；`shipment.track` 是可选补充。缺少订单查询能力时转人工；物流事实缺失时不推断发货/签收。 |
| `repair_quality_intake` | PCB/PCBA/元器件报修与质量问题 | 使用 `case.create` 建立内部工单；要求客户/产品来源核验、质量负责人组配置，并经过已有 Tool Gateway 确认。受理不代表已维修或完成质量判定。 |
| `invoice_application` | 发票申请、变更或问题登记 | `billing.get_invoice` 只读，`case.create` 仅能建内部申请单；当前没有开票写能力，所以实际开票始终 `needs_human`。税号标记敏感，不写进普通任务日志。 |
| `technical_escalation` | 制造工艺、产品规格和技术问题 | `case.create` 仅创建已确认的内部升级工单；需配置工程负责人组。模型不能生成未经核验的工艺参数、制造承诺或工程结论。 |

## 模板字段

实现位置：`apps/api/src/platform_core/agent_runtime/tasks/standard_flows.py`。

每个版本内置模板声明业务线、intent code、必填/选填槽位、槽位来源、敏感字段、只读工具白名单、受控写白名单、负责人组、确认要求、部分完成、超时、取消和人工退出条件。槽位值仍走现有语义验证、PII withholding、任务存储和 Tool Gateway；该目录不保存客户字段值。

`resolve_flow_availability()` 根据当前租户/坐席过滤后的 `CapabilityView` 判定 `available` 或 `needs_human`，并验证只读工具仍为 `read`、内部写入仍为 `confirmed_write`。可选只读能力缺失时能展示缺失原因，不会阻断基础订单查询。模板声明的工具名只能来自现有 registry，不能靠模板新增连接器能力。

## 安全边界

- 模板目录不直接读外部系统、调用模型或执行写入。
- 写任务必须保持现有 `ready → awaiting_confirmation → executing` 流程；成功态仍需真实 postcondition 或人工记录。
- 外部写能力没有 connector 和正式 schema 时，流程必须是 `needs_human`。发票申请目前就是这种情况。
- `unknown` 只能进入人工对账，不能作为失败后盲目重试信号。
- 缺少业务负责人时不靠模型猜测队列，直接显示 `FLOW_OWNER_UNASSIGNED`。

## 本地验收

`test_standard_flows.py` 覆盖四模板完整性、registry 白名单、订单只读和可选物流、风险分类篡改拒绝、质量受理负责人/确认门禁、发票写能力缺失，以及技术升级不伪称完成。模板还未通过浏览器 UI 验收，端到端自动填槽与每种流程状态机仍待实现。
