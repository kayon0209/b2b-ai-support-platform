# 架构决策索引

ADR 保存决策及其当时的理由。被取代的决策保留历史，不再作为当前部署要求。

## 当前主要边界

- [0002 模块化单体](0002-modular-monolith.md)
- [0003 身份与租户隔离](0003-identity-and-tenant-isolation.md)
- [0004 权限感知检索](0004-permission-aware-retrieval.md)
- [0005 引用与支持性](0005-claim-support-not-citation-existence.md)
- [0006 实时业务数据走工具](0006-realtime-data-via-tools.md)
- [0007 场景路由而非通用多 Agent 分发](0007-no-multi-agent-scene-dispatch.md)
- [0008 内部工单工具风险](0008-case-create-risk-class.md)
- [0009 拒答指标范围](0009-abstention-rate-scope.md)
- [0011 访客会话身份](0011-customer-chat-is-a-visitor-session.md)
- [0012 自有客服渠道](0012-remove-chatwoot.md)
- [0013 入站渠道适配器](0013-inbound-channel-adapters.md)
- [0014 出站渠道传输](0014-outbound-channel-delivery.md)
- [0015 会话优先工作台](0015-conversation-first-workbench.md)

## 被取代的历史方向

- [0001 Chatwoot 客服内核](0001-chatwoot-as-support-kernel.md)：由 0012 取代。
- [0010 Chatwoot 客户入口与内部 /chat](0010-platform-chat-is-internal.md)：渠道方向由 0012 取代，旧界面由 0015 后续实现退役；operator API 仍受权限保护。

当前能力与限制统一见 [能力矩阵](../capabilities.md) 和 [生产接入条件](../production-readiness.md)。
