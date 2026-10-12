# 渠道与业务系统集成

状态：适用于平台自有渠道架构；历史第三方客服内核映射只作为 ADR 历史，不是当前接口。

## 入站渠道

网页客户窗口直接写入 `/v1/support/*`，使用绑定单一租户与会话的签名访客令牌。邮件、微信等适配器在 `apps/api/src/platform_core/channels/` 实现 `channels/base.py` 约定；适配器验证 provider 签名、限制 payload、规范化联系人/会话引用并持久化 InboxEvent 后再入队。

共享路径处理意图、权限过滤检索、工具网关、会话租约与审计。添加渠道不得增加第二个 Agent 运行分支。

## 出站

网页客户从会话时间线读取已持久化回复，不需要外部传输。人工回复写入 turn 与 `conversation.agent_reply` outbox，由 relay 处理所选渠道；AI 主链路通过 `ChannelSender` 派发邮件/微信等外部回复。

发送检查共享租约、执行预算和幂等边界。缺传输记录 `OUTBOUND_NOT_CONFIGURED`，地址缺失/传输失败/结果不确定分别记录；不把已入库或 provider 受理解释为客户已读。普通 outbox 对不可安全重放的 unknown/crash 停放，不盲目重送。内置传输不是通用 provider 送达/已读回执系统；需要相应 provider 契约与真实渠道验证。

## 业务连接器

ERP/CRM/工单等通过 Tool Gateway 与 connector adapter 调用。读路径要求租户绑定、身份核验（必要时）、超时/重试/熔断和数据来源说明。写路径需策略、确认、幂等和后验验证；模型只提出建议。

凭据使用 secret reference，不返回到浏览器、不记录到日志。执行器按当前租户的活动 connector、能力和配置解析；无执行器时返回明确错误。凭据失败、死信和健康状态用于运营诊断；不从“有连接器记录”推断可执行或已完成集成。

## 渠道上线清单

1. 签名与重放防护、租户解析、payload 最小化。
2. 入站事件 schema 与重复 delivery 契约测试。
3. 通道到 `conversation_ref` 的确定性映射和联系人绑定。
4. 出站线程 key、幂等、delivery receipt、有限重试与死信操作。
5. 真实 sandbox 收发、失败注入和租户间负例。

## 企业接入范围

订单/物流/发票/库存经 `business_api` 读取，内部 Case 由平台服务。Jira、Linear、CRM 与通知执行能力取决于实际 adapter；`crm.create_opportunity` 当前绑定 local/test 的 `demo_crm`，不能视为真实 CRM 商机接入。接口、归属、条件写及对账按 [生产接入条件](production-readiness.md) 验证。
