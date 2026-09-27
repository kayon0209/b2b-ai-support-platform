# R2-01 情绪趋势建议：接口与验收契约

状态：本地实现和隔离验收通过；生产开关保持关闭。CI 尚未运行本分支。

## 产品边界

- 情绪分类只提供坐席参考，不能修改 Case 优先级、SLA、负责人、工具提案或客户回复。
- 不调用模型；基于最近最多 5 条客户消息运行确定性规则。
- 引用内容和显式否定不作为当前情绪证据；混合语气/反讽标记为不确定并提示人工查看上下文。
- 响应只返回分类、趋势、枚举原因和现有会话消息的 turn/span 定位，不复制客户原文。
- 新租户功能键 `agent.emotion_priority_advice` 默认关闭。关闭时详情字段为 `null`，队列情绪排序返回 `FEATURE_DISABLED`。

## API 契约

### 队列

`GET /v1/workbench/conversations?tab=queue&sort=activity|emotion`

- 缺省为 `sort=activity`，保持现有最近活动顺序。
- 开启情绪功能后，队列项可带 `emotion_advice`；排序只重排当前响应页。
- 响应标明 `sort_mode`、`sort_scope=current_page` 和 `emotion_advice_enabled`。
- 每个情绪排序项同时显示建议级别和原因，不能把建议伪装成 Case 当前优先级。

### 会话详情

`GET /v1/workbench/conversations/{conversation_ref}`

功能开启时返回 `emotion_advice`，其中 `advice_id` 绑定 tenant、conversation 和完整 timeline revision；`evidence` 仅含 `turn_id`、Unicode 码点起止位置、情绪级别和原因码。服务端的租户 RLS 与当前会话租户共同限制读取范围。

### 主管纠正

`POST /v1/workbench/conversations/{conversation_ref}/emotion-advice/reviews`

- 仅 `support_admin` / `tenant_owner`；必须提供 `Idempotency-Key`。
- 请求字段：`advice_id`、`corrected_level`、`reason_code`。级别和原因均为固定枚举。
- 新消息使建议版本变化时拒绝旧 `advice_id`，返回 `EMOTION_ADVICE_STALE`。
- 同一幂等键、同一请求重放返回原记录；同键异参为 `IDEMPOTENCY_CONFLICT`。
- 持久化 `tenant_id`、会话引用、建议/更正级别、原因码、审核人、版本摘要和幂等键；不持久化客户句子、模型提示词或自由文本。
- `emotion_advice_reviews` 使用 FORCE RLS；应用角色仅有 `SELECT`、`INSERT`，没有 `UPDATE`、`DELETE`。成功写入同事务审计事件。

## 运营与回滚

功能默认关闭，必须通过现有租户 Feature Flag 运维流程显式开启；回滚时关闭 `agent.emotion_priority_advice`。新增 Prometheus 指标 `platform_workbench_emotion_advice_total{action,outcome}` 不带 tenant、会话或客户标签；写入审计使用请求 trace id。指标标签由代码固定为低基数值。

纠正样本只供人工质检和后续评估，不会自动训练、更新词典或改变线上行为。模型/规则更新需另走固定语料评测与发布审查。

## 验收记录

在 `r2r3_restart_accept_20260927` 隔离 PostgreSQL 数据库上迁移到 `0066_emotion_advice_reviews`，测试结束后该库按清理计划销毁。

- 最近一次组合复验中，74 项 R2 API/策略/情绪、R2-02 流程目录和 shared-contract 测试通过；覆盖默认关闭、功能开启、当前页排序、主管权限、重放冲突、过期建议拒绝及合同边界。
- 14 项跨租户、数据库权限、迁移回滚/重放和入站性能验收通过；完整迁移从 0065 到 0066、downgrade/re-upgrade 与全链回滚/重建通过。
- 管理端 TypeScript/Vite production build、36 项现有前端行为检查和 runtime guards 通过。
- Ruff 与目标模块 Mypy 通过。

尚未覆盖：CI 对本分支运行、浏览器尺寸/截图验收、200% 缩放与屏幕阅读器走查，以及任何真实生产租户的数据质量评估。以上不改变默认关闭和“仅供人工参考”的产品边界。
