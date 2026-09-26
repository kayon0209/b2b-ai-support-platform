# 测试与评估策略

当前系统自有客户渠道。旧 Chatwoot e2e 清单为历史记录，不是现行验收路径。平台验收分为可重复的 unit/contract、PostgreSQL+RLS integration、浏览器 journey、LLM eval 与生产近似负载测试。

## 工作台发布关键用例

1. **无 Case handoff**：访客进入人工队列后出现在坐席队列，打开详情不返回另一个租户数据。
2. **并发认领**：两坐席同时认领同一 ref，只有一个租约 owner 成功；另一个拿到 409，不产生第二个 owner。
3. **容量控制**：同一活跃坐席并发认领不超过 AgentProfile.max_concurrent。
4. **人工/AI 竞态**：AI 生成中人工接入，AI pre-send CAS 必须阻止 customer-visible send。
5. **回复幂等**：同一 idempotency key 并发或超时后重试只产生一个 turn/outbox；相同 key 不同文本返回 409。
6. **转交/结束**：只有当前 owner 可操作；expected_version 过期失败；关闭后旧访客 token 不可继续写，开启新会话取得新 ref。
7. **客户轮询/CSAT**：人工回复在前台自动出现；转人工时不评分；结束且有人工回复后才能评分，response_rate 只计算合格会话。
8. **分页与隐私**：新会话加载最近 turn，游标向前不重不漏；搜索仅限租户且内容已脱敏。
9. **附件**：未关联工单不能上传；不允许跨租户读取预签名 URL；上传只计作工单证据，不冒充对客投递。

## 运行方式

- 前端类型与构建：`cd apps/admin-web && npm run build`
- 发布环境变量门禁：`cd apps/admin-web && npm run build:release`
- Python lint/typecheck：按仓库 CI 的 `ruff check`、`ruff format --check` 与 mypy 目标运行。
- 集成套件需要生产近似 PostgreSQL、`platform_app` 非 BYPASSRLS 角色、迁移和测试配置；SQLite 只能补纯状态逻辑，不能证明 RLS 或租约并发。
- 浏览器验收必须使用真实 FastAPI/worker 与客户/坐席双会话；mock API 只可检查排版与前端状态，报告中要明确标注。

## AI 质量门禁

检索召回、引用支持、拒答、写操作确认、人工接管 race、语言一致性和用户数据最小化分别维护固定评估集。模型或 prompt 发布先跑评估与 shadow/灰度；高风险失败时按 runbook 关闭 flag 或回滚版本。
