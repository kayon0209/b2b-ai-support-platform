# R3 自研业务系统接入与权威数据契约

状态：已实现供应商中立的共享 Pydantic 契约和纯契约测试；尚无真实 provider adapter、sandbox 或业务系统写入。

## 已确定的产品决策

ERP、MES、WMS、CRM 以企业自研为主。标准 ERP 的对象和流程不能假设能覆盖元器件采购、PCB/PCBA 制造、报价、订单和供应链协同。产品/规格、库存、客户报价等权威来源尚未确定，接入契约必须允许按业务域分别绑定来源。

## 建议的 authority binding

以下是待企业负责人确认的初始映射，不代表当前系统事实：

| 数据域 | 候选权威系统 | 关键约束 |
|---|---|---|
| 产品规格与工艺版本 | PLM、MES 或 ERP | SKU/料号、revision、层数、板厚、材料、铜厚、表面处理等规格必须绑定版本 |
| 可用库存 | WMS、ERP 或 MES | 绑定仓库/库位、批次适用范围、单位、预留量和采样时间 |
| 客户报价 | ERP 或 CRM 报价子系统 | 币种、最小数量、报价版本、有效期、客户价目归属；无数据则不得给价 |
| 客户/企业账户归属 | CRM 或 ERP 客户主数据 | 外部订单、工单和报价必须验证记录所属客户 |
| 客户订单 | ERP | 状态与交期只引用当前授权客户的订单记录 |
| 生产工单与制造状态 | MES | 工单、工序、批次、质量状态和计划版本带来源与时效 |
| 发运与物流 | WMS 或 ERP | 发运状态、物流单号和签收事实不得由模型补全 |
| 销售商机 | CRM | 创建/更新需 Tool Gateway proposal、确认、幂等和读回验证 |

每个租户和业务域只激活一条明确批准的 `AuthorityBinding`。冲突数据不按模型判断或“选最新”来消解；进入 `needs_human` 并记录源版本与差异。

## Canonical schema

实现位置：`packages/contracts/src/platform_contracts/business_systems.py`。

- `AuthorityBinding` 记录 tenant、数据域、连接器、系统类型、contract/schema 版本、数据最大时效和审批人。
- `SourceMetadata` 随每个规范化产品、库存、报价事实返回：tenant、binding id/version、connector id、系统类型、外部记录引用/版本、读取时刻和失效时刻。
- `AuthorityBinding.freshness_deadline()` 将过期时刻限制为 `retrieved_at + max_age_seconds` 与源系统业务有效期两者中更早的一个。
- `CanonicalProductSpecification` 使用规范化料号/版本和 bounded primitive 属性；例如 PCB/PCBA 属性可明确为 `layer_count`、`board_thickness_um`、`copper_weight_millioz`、`material_code` 和 `surface_finish`。
- `CanonicalInventorySnapshot` 要求数量、单位、库位及可选客户归属；数量不是价格，不使用货币字段。
- `CanonicalQuote` 强制 3 位大写 ISO 币种、整数最小货币单位、最小数量、来源记录和业务报价有效期。
- 全部 canonical 模型拒绝未知字段；原始 provider payload 和密钥不属于共享契约。
- 每个 tenant 字段必须和 `SourceMetadata.tenant_id` 一致。租户实际身份由服务端 connector/context 解析，不能接受客户端 tenant id 作为授权依据。

## 受控外部写入

`OwnershipProof` 要求请求账户与 provider 返回的账户归属匹配。写入仍必须走现有 Tool Gateway：allowlisted capability、actor/resource policy、稳定幂等键、参数版本绑定的 proposal、人工确认、有限超时/重试、breaker 和 postcondition read-back。

`ExternalWriteReceipt` 只允许四种状态：

- `needs_confirmation`：尚未执行。
- `unknown`：超时、断连或 read-back 不完整。必须进入对账，不能告诉坐席或客户“已成功”。
- `verified_applied`：只有目标记录存在且 postcondition read-back 通过，才能作为成功。
- `rejected`：权限、归属或 provider 明确拒绝。

回执保存幂等键 hash、稳定 reason enum 和外部记录引用，不把 credential、外部原始 payload 或客户自由文本带进日志/指标。错误与审计按请求 trace id 关联；指标不带 tenant 或客户标签。

## 连接器进入条件

真正 provider 实施前，每个业务域需要：

1. 已签字的权威系统、字段定义、状态含义、主键/版本规则和跨系统归属映射。
2. 用户授权的非生产 sandbox、版本化 REST/webhook 契约、限流、幂等语义和读回查询能力。
3. 由企业安全负责人配置的 secret manager `credential_ref`；聊天、日志、模型请求和响应中不得出现凭据。
4. 每类记录的 owner proof、过期/陈旧数据处理、partial failure 和 `unknown` 对账负责人。
5. Tool Gateway capability allowlist、确认角色、postcondition、跨租户拒绝测试和回滚/补偿动作。

## 当前验收

当前 `packages/contracts` 共 34 项测试通过，覆盖 UTC 与时效、TTL 截断、租户来源一致性、未知字段拒绝、报价币种/金额单位、外部归属证明、`unknown` 与成功读回的状态约束，以及知识评测证据。它们证明 canonical schema 的行为，不代表真实业务 API 已连通，也不代表端到端写入已验收。
