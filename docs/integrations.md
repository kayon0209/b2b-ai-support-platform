# 渠道与业务系统集成

状态：适用于平台自有渠道架构；历史第三方客服内核映射只作为 ADR 历史，不是当前接口。

## 入站渠道

网页客户窗口直接写入 `/v1/support/*`，使用绑定单一租户与会话的签名访客令牌。邮件、微信等适配器在 `apps/api/src/platform_core/channels/` 实现 `channels/base.py` 约定；适配器验证 provider 签名、限制 payload、规范化联系人/会话引用并持久化 InboxEvent 后再入队。

共享路径处理意图、权限过滤检索、工具网关、会话租约与审计。添加渠道不得增加第二个 Agent 运行分支。

## 出站

坐席回复与 Agent 回复先在本平台事务中写入 turn/outbox，再由对应 adapter 发送。每条 outbox 事件要求幂等处理、有限重试、超时、结构化错误与可观测 delivery state。渠道未配置或错误时，界面显示未送达/等待重试，不把“已入库”显示为“客户已收到”。

## 业务连接器

ERP/CRM/工单等通过 Tool Gateway 与 connector adapter 调用。读路径要求租户绑定、身份核验（必要时）、超时/重试/熔断和数据来源说明。写路径需策略、确认、幂等和后验验证；模型只提出建议。

凭据使用 secret reference，不返回到浏览器、不记录到日志。连接状态与“当前可执行”状态分开，健康检查失败会阻止发送路径。

## 渠道上线清单

1. 签名与重放防护、租户解析、payload 最小化。
2. 入站事件 schema 与重复 delivery 契约测试。
3. 通道到 `conversation_ref` 的确定性映射和联系人绑定。
4. 出站线程 key、幂等、delivery receipt、有限重试与死信操作。
5. 真实 sandbox 收发、失败注入和租户间负例。
