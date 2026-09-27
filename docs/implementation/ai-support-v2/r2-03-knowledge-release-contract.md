# R2-03 知识版本评测与发布门禁契约

状态：不可变评测记录、双人批准、发布守卫、发布后测量和回滚 API/UI 已接入并有 PostgreSQL 集成验收；真实固定评测集和受信任 evaluator worker 尚未接入，生产 flag 保持关闭。

## 固定评测证据

实现位置：`packages/contracts/src/platform_contracts/knowledge_release.py`。

`KnowledgeEvalRun` 将每次结果绑定到：tenant、知识空间、文档版本、知识内容 SHA-256、评测集 SHA-256、检索配置 SHA-256、evaluator 版本、代码 commit、UTC 执行时间和指标。批准 fingerprint 对完整候选运行结果做规范化 JSON SHA-256，评价人不能批准一个结果后再替换成不同内容/指标。

前后比较必须同租户、同空间、同评测集、同检索配置、同 evaluator、同代码 commit 和相同样本数；知识版本或内容必须改变。否则返回 `EVAL_INPUT_MISMATCH` / `KNOWLEDGE_VERSION_UNCHANGED` 并阻止发布。

持久层位于 `apps/api/src/platform_core/knowledge/release_models.py` 和 migration `0067_knowledge_release_gate`：evaluation、approval、post-test 和 release event 都强制 RLS，应用角色只有 SELECT/INSERT。`knowledge.release_eval_gate` 默认关闭；开启后草稿发布必须有绑定当前草稿哈希、目标知识空间和仍然 active 的基线版本的通过记录，并有两名不同审核人签署同一候选 fingerprint。受信任 system/service 上下文写入评测结果；浏览器坐席无法提交自报分数。

候选 `DocumentVersion` ID 由服务端用 tenant、draft 和幂等键稳定派生；评测服务提供的 ID 会被忽略，避免调用方选择或碰撞其他文档版本。

流程路由：

1. Trusted evaluator 调用 `POST /v1/knowledge/internal/drafts/{draft_id}/release-evaluations` 写入前测；身份必须是服务端解析的 system/service `integration_service`。同键同证据为重放，同键异证据冲突。
2. `knowledge_manager` / `tenant_owner` 在 `POST /v1/knowledge/drafts/{draft_id}/release-evaluations/{evaluation_id}/approve` 独立批准，同一评测指纹要两人通过，作者不能自批。
3. Flag 开启后，`POST /v1/knowledge/drafts/{draft_id}/publish` 强制提供评测 ID，重新核对草稿内容 hash、目标空间、active 基线、样本集与评测配置；发布事务复用预先分配的候选 DocumentVersion ID。
4. Ingestion 将版本设为 active 时追加 release activation event。Trusted evaluator 使用 `POST /v1/knowledge/internal/releases/{evaluation_id}/post-test` 记录同版本后测。
5. unsafe 或退化后可调用 `POST /v1/knowledge/releases/{evaluation_id}/rollback`，版本切换与审计同事务完成。关闭 Feature Flag 会阻断新发布；post-test 和 rollback 仍可用作恢复操作。

Workbench 知识缺口页面可以查看评测指纹、批准数，按角色批准候选；发布选择器只显示已通过且达到两人批准的 evaluation。active 版本通过检索使用的 DocumentVersion status 控制回滚。普通租户 flag 默认关闭。

## 发布判定

- 至少两名不同审核人批准同一个候选 fingerprint，审核人必须不同于草稿作者；单人、多次点击、作者自批或旧版 fingerprint 均不通过。
- 候选语料出现任一 unsafe answer 即阻止发布。
- 已声明 grounded answer、citation support 与 retrieval recall 三个指标；任一相对基线下降超过 2 个百分点即阻止发布。
- 2 个百分点目前是开发期保守默认值，尚未由企业知识/安全负责人签字；不得直接将 `eligible` 当作生产授权。
- 发布后测评必须绑定同一个候选版本、内容/快照 hash、数据集、检索配置、evaluator 和 commit；unsafe 样例或超过门槛的退化会写入 `rollback_required` 事件。候选仍处于活动状态且基线扫描干净时，回滚会原子 supersede 候选并恢复基线版本。

## 当前限制与下一步

仍未完成：真实固定评测集的维护与授权、可信 evaluator worker 对发布候选执行前/后评测、CI artifact 的受控导入、灰度百分比和多实例并发发布演练。评测写入 API只接受 system/service 的服务器身份；当前仓库没有生产 evaluator 调用方，所以该开关须保持关闭，不能通过人工伪造 metrics 放行。

下一步需要确定并审核知识评测集、实现受信任 evaluator worker，并在 staging 用真实固定语料跑发布前测、灰度后测及多实例 rollback 演练。当前 evaluator endpoint 可存受信任服务结果，但本仓库没有生产 evaluator 调用方；多实例发布锁和灰度百分比也尚未演练，因此这些生产步骤未通过前不得开启租户 flag。

## 本地验收

`packages/contracts/tests/test_knowledge_release.py` 的 12 项测试覆盖同一评测输入、同一审批 fingerprint、双人独立批准、作者自批拒绝、unsafe 样本拒绝、阈值边界/回归、知识内容版本未变、发布后测试版本绑定和 UTC 时间边界。`apps/api/tests/integration/test_knowledge_release_gate.py` 在隔离 PostgreSQL 上验证 service-only evidence、RLS、审批、publish replay、激活、失败 post-test、append-only 回滚和版本状态恢复。
