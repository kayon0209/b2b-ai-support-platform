# R2-02 四种标准服务流程目录

状态：领域目录、租户/坐席能力判定、只读 Workbench API 和 Tasks 面板流程卡片已接入；流程模板实例化、与多意图 task 绑定及每个流程的执行状态图仍待实现。目录不直接发起工具调用。

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

`GET /v1/workbench/standard-flows` 要求 `case.read`，返回四个模板及当前租户/坐席的可用状态。`resolve_flow_availability()` 验证只读工具为 `read`、内部写入为 `confirmed_write`、所需 Connector 处于 active，并且质量/财务/工程负责人组至少有一位 active 的坐席、支持管理员或租户负责人。可选只读能力缺失时展示缺失原因，不阻断基础订单查询。模板声明的工具名只能来自现有 registry，不能靠模板新增连接器能力。

Workbench「任务」页展示可展开的流程卡片，包括所需信息来源、工具白名单、确认要求、部分完成/超时/取消规则和转人工条件。状态“配置具备”代表当前租户/坐席具备基础配置；每次真实执行仍需 Tool Gateway 再检查权限、连接器、凭据、参数和读回结果。卡片当前是只读流程说明，没有“开始流程”按钮。

## 安全边界

- 模板目录不直接读外部系统、调用模型或执行写入。
- 写任务必须保持现有 `ready → awaiting_confirmation → executing` 流程；成功态仍需真实 postcondition 或人工记录。
- 外部写能力没有 connector 和正式 schema 时，流程必须是 `needs_human`。发票申请目前就是这种情况。
- `unknown` 只能进入人工对账，不能作为失败后盲目重试信号。
- 缺少业务负责人时不靠模型猜测队列，直接显示 `FLOW_OWNER_UNASSIGNED`。

## 本地验收

`test_standard_flows.py` 与 `test_standard_flow_catalog.py` 覆盖四模板完整性、registry 白名单、坐席权限、租户 RLS、active Connector、活跃负责人组、订单只读和可选物流、发票写能力缺失，以及技术升级不伪称完成。TypeScript/Vite production build 通过；真人浏览器/辅助技术验收、端到端自动填槽和每种流程状态图仍待实现。
