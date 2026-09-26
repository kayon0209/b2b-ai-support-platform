# 当前开发计划

当前主线是自有客户渠道、会话租约收件箱和证据驱动的 Agent。旧 Chatwoot-first 路线已被 ADR 0012 取代；本计划不要求安装或部署 Chatwoot。

## 现状

- 已有：`/support` 访客窗口、web/email/WeChat adapter 基础、FastAPI 模块化单体、tenant RLS、Case/SLA、知识摄取与引用、Tool Gateway、审计、评估和 worker outbox。
- 本次增加：以 ConversationControlLease 为来源的坐席收件箱，连接没有 Case 的 handoff；认领、释放、转交、结束、人工回复、历史分页、常用话术、引用/客户上下文面板；客户端人工消息轮询和结束后 CSAT。
- 本次移除：旧 `/chat` 内部验证 UI、旧 `CustomerChat` 和重复 `DataCard` 渲染器。`/chat` 旧地址回到客户首页。

## 近期迭代顺序

1. 用 PostgreSQL + RLS 跑 workbench inbox 集成测试、跨租户搜索负例、双坐席并发认领与 stale version 重试。
2. 双浏览器跑客户/坐席端到端：handoff → claim → send → customer sees message → wait/reply → close → survey。
3. 完成真实 IdP 与前端托管配置，确认 CSP、回调、logout 与静态 sourcemap 门禁。
4. 连通一个正式 ERP 和一个 customer channel sandbox，验证真实 delivery、失败重试与工单状态。
5. 对 10k conversations/tenant 与 1000 active tabs 做队列查询和轮询压力测量，基于证据调整索引/轮询频率。
6. 试点逐租户灰度；采集首响时间、队列等待、人工回复可见时延、送达成功率、CSAT 与转人工失联率。

每个功能交付都更新 API schema、权限/RLS、审计、失败重试、指标、验收和回滚文档。只有测试环境模拟数据通过不能代替真实渠道与真实数据库验收。
