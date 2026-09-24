# 当前平台架构

状态：现行架构。历史决策保留在 `docs/adr/`；渠道自建决定见 ADR 0012，坐席租约收件箱见 ADR 0015。

## 系统边界

平台自己承载 `/support` 客户页面与 `/admin/workbench` 坐席页面。FastAPI 模块化单体拥有租户、会话投影、工单/SLA、知识、AgentRun、工具执行、引用、评估和审计。渠道 provider 只通过版本化适配器、签名 webhook 和出站接口连接；不读写外部系统数据库。

```text
客户 /support ─┐                         ┌─ 管理台 /admin/*
渠道 Webhook ──┴─> FastAPI 控制平面 ─────┴─> PostgreSQL + RLS
                     │       │                    │
                     │       └─ Tool Gateway       └─ pgvector
                     ├─ Outbox / Inbox ─> Worker
                     ├─ Redis（限流/短期协调）
                     └─ MinIO/S3（原始文件）
```

## 会话与工单

- `ConversationTurn` 保存最小化、脱敏的对话副本；外部渠道的原始消息仍由其来源系统持有。
- `ConversationControlLease` 决定 AI、人工、队列或已结束状态。客户可见的 AI 发送必须在派发前比较租约版本。
- 坐席收件箱以人工队列租约为主，不依赖 Case 存在。Case 可关联一个或多个会话，Case 仍单独管理状态和 SLA。
- 出站回复由渠道适配器按幂等键投递；网页渠道从会话时间线读取。API 返回接收不等于渠道送达，通道结果必须分别记录。

## 安全边界

每个租户业务行带 `tenant_id`，生产数据库使用非 BYPASSRLS 的应用角色。请求中的租户由认证成员或已签名访客令牌解析。原始客户内容、完整文件和凭据不得进模型外日志；检索在模型前执行租户与知识 ACL 过滤。

## 当前扩展原则

先测量查询、队列与模型路径，再决定是否拆服务或加组件。新渠道必须实现现有渠道接口和契约测试；不得复制一套对话主流程。PG RLS、幂等 outbox、租约 CAS、审计和可回滚开关属于所有写路径的组成部分。
