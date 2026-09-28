# R2-03 知识版本评测与发布门禁契约

状态：不可变评测记录、双人批准、发布守卫、发布后测量和回滚 API/UI 已接入。新增隐藏候选暂存、candidate-aware 前后测 runner、Ed25519 worker attestation、签名证据持久化和快照复核。普通检索只看到 active 版本；已索引候选在批准发布前保持 draft。专用 outbox worker 已接入但默认关闭；当前没有获批固定集、公钥 allowlist、worker 私钥或运行预算配置，因此不会触发真实模型评测，生产 flag 仍关闭。旧的自报分数和无签名后测接口永久拒绝。

## 固定评测证据

实现位置：`packages/contracts/src/platform_contracts/knowledge_release.py`。

`KnowledgeEvalRun` 将每次结果绑定到：tenant、知识空间、文档版本、知识内容 SHA-256、评测集 SHA-256、检索配置 SHA-256、evaluator 版本、代码 commit、UTC 执行时间和指标。批准 fingerprint 对完整候选运行结果做规范化 JSON SHA-256，评价人不能批准一个结果后再替换成不同内容/指标。

前后比较必须同租户、同空间、同评测集、同检索配置、同 evaluator、同代码 commit 和相同样本数；知识版本或内容必须改变。否则返回 `EVAL_INPUT_MISMATCH` / `KNOWLEDGE_VERSION_UNCHANGED` 并阻止发布。

持久层位于 `apps/api/src/platform_core/knowledge/release_models.py` 和 migration `0067_knowledge_release_gate` / `0069_signed_release_evidence`：evaluation、approval、post-test 和 release event 强制 RLS，app 角色仅 SELECT/INSERT。`knowledge.release_eval_gate` 默认关闭。已签名结果须匹配部署 allowlist 中 tenant/space 的获批数据集 hash 和 approval reference；发布还会重核草稿 hash、候选/基线、ACL/chunk snapshot 和双人审批 fingerprint。

候选 `DocumentVersion` ID 由服务端用 tenant、draft 和幂等键稳定派生；评测服务提供的 ID 会被忽略，避免调用方选择或碰撞其他文档版本。

流程路由：

1. `POST /v1/knowledge/internal/drafts/{draft_id}/release-candidates` 由 system/service `integration_service` 在幂等键保护下暂存已审核草稿。候选使用专属 `gap-candidate://` 文档地址；worker 索引完成后仍保留 draft 状态。runner 将单个候选叠加到同一空间 active 版本，并沿用租户、空间、ACL、扫描状态和有效期过滤；执行要求 repeatable-read 事务。
2. 固定数据集由部署 allowlist 指向租户前缀对象路径，并记录 SHA-256、审批编号和两位不同审核人 ID。runner 校验样本、期望来源及 principal scope 映射的语义 hash。`POST /v1/knowledge/internal/drafts/{draft_id}/release-evaluation-requests` 只把 UUID、数据集摘要和幂等摘要写入 outbox；专用 `APP_WORKER_QUEUE=release_evaluator` 消费者在租户 RLS 会话加载 manifest，校验每次最大样本数后再调用模型。消费者以 Ed25519 签名前后测量；API 按 `APP_KNOWLEDGE_EVALUATOR_PUBLIC_KEYS_JSON` 验签，再比对 tenant/space/dataset allowlist、候选内容与知识/ACL/chunk 快照。前测同键同 attestation 重放，同键异结果冲突。旧的 `/release-evaluations` 自报入口拒绝写入。
3. `knowledge_manager` / `tenant_owner` 在 `POST /v1/knowledge/drafts/{draft_id}/release-evaluations/{evaluation_id}/approve` 独立批准，同一评测指纹要两人通过，作者不能自批。
4. Flag 开启后，`POST /v1/knowledge/drafts/{draft_id}/publish` 强制提供评测 ID，重新核对草稿内容 hash、目标空间、active 基线、样本集与评测配置；发布事务复用预先分配的候选 DocumentVersion ID。
5. 发布复用已索引候选，在同一事务激活版本、标记草稿解决并写入 activation event。发布前会再次验签并确认知识/ACL/chunk snapshot 未变化。
6. 自动运行显式开启时，publish 事务同时写 post-test outbox event；独立 worker 使用同一 dataset、ACL scope 和模型/检索配置跑 active 版本，签名后写入。unsafe、引用退化或输入不匹配会追加 `rollback_required`。无签名的旧 post-test API 保持拒绝。
7. unsafe 或退化后可调用 `POST /v1/knowledge/releases/{evaluation_id}/rollback`，版本切换与审计同事务完成；经授权的人工 rollback 也可用于恢复。

Workbench 知识缺口页面可以查看历史评测指纹、批准数和回滚操作。门禁开启但可信评测不可用时，页面明确提示并禁用批准及发布；可信评测接入后，发布选择器只显示已通过且达到两人批准的 evaluation。active 版本通过检索使用的 DocumentVersion status 控制回滚。普通租户 flag 默认关闭。

## 发布判定

- 至少两名不同审核人批准同一个候选 fingerprint，审核人必须不同于草稿作者；单人、多次点击、作者自批或旧版 fingerprint 均不通过。
- 候选语料出现任一 unsafe answer 即阻止发布。
- 已声明 grounded answer、citation support 与 retrieval recall 三个指标；任一相对基线下降超过 2 个百分点即阻止发布。
- 2 个百分点目前是开发期保守默认值，尚未由企业知识/安全负责人签字；不得直接将 `eligible` 当作生产授权。
- 发布后测评必须绑定同一个候选版本、内容/快照 hash、数据集、检索配置、evaluator 和 commit；unsafe 样例或超过门槛的退化会写入 `rollback_required` 事件。候选仍处于活动状态且基线扫描干净时，回滚会原子 supersede 候选并恢复基线版本。

## 当前限制与下一步

现有 CLI 使用临时评测租户中的静态语料，不能作为企业知识发布集。新 runner 从 tenant 前缀对象路径加载固定 manifest，校验 tenant/space、样本、期望来源、principal scopes 与批准 hash；前后测绑定检索 cutoff、active/candidate 快照、ACL/chunk 清单、alias、chat 与 embedding 模型/endpoint/参数、evaluator 版本和 commit。结果以签名 JSON 持久化，不保留原始问句、答案或文档片段。outbox consumer 只由独立 worker role 运行，owner 连接仅领取元数据，租户 RLS 会话读取 payload；每次调用必须设置 `APP_KNOWLEDGE_EVALUATOR_AUTO_RUN=true` 和正数 `APP_KNOWLEDGE_EVALUATOR_MAX_CASES_PER_RUN`（上限 500），worker 私钥通过 `APP_KNOWLEDGE_EVALUATOR_PRIVATE_KEY_B64URL` secret 与 key ID 注入，仅该 worker 读取；还必须提供完整 `APP_BUILD_COMMIT_SHA`。API 默认 `auto_run=false`、样本上限为 0。可通过 `platform_knowledge_release_jobs_total` 查看有界作业结果；terminal failure 需要运维调查，不会伪装成后测通过。当前没有获批数据、公钥、worker 私钥或成本预算，因此没有真实候选评测分数，也不会发生自动模型调用。

仍未完成：知识/安全负责人提供并批准真实 fixed-set 文件及 principal mapping；部署团队配置数据集对象 key、公钥与 worker 私钥 secret；负责人明确可接受的模型调用/费用上限后再设置每次样本 ceiling；完成 staging 灰度、多实例并发发布、worker 重启和回滚演练。迁移 `0070_outbox_processing_fence` 为专用消费者增加 fencing token；worker 有陈旧 claim 恢复、三次有界重试和 terminal failure 记录。上述授权与运营门禁到位前不得启用自动运行或 tenant flag。

部署前先由知识/安全负责人提供固定问答集和审批映射，负责人核定调用上限，部署团队再配置公钥、secret-managed worker 私钥和单独的 `release_evaluator` worker。随后在 staging 跑前测、灰度后测、worker 重启及多实例 rollback 演练；这些步骤未通过前不得开启自动运行或租户 flag。

## 本地验收

`packages/contracts/tests/test_knowledge_release.py` 覆盖既有指标和双人批准契约；`apps/api/tests/integration/test_knowledge_release_gate.py` 在隔离 PostgreSQL 经真实 outbox consumer 运行签名前测和后测 worker：同一合成 case 在 active baseline 的 expected-candidate recall 为 0、staged candidate 为 1；安全 fake post-run citation support 为 1.0，之后用刻意包含 forbidden claim 的 fake answerer 验证签名后测被阻塞并追加 rollback-required。两个 worker 结果均经 Ed25519 写入 RLS 表。该旅程使用 fake answerer、deterministic embedder、合成 manifest/文档和测试生成的密钥，不代表真实固定集或真实模型质量。集成还覆盖 outbox metadata-only claim、tenant-RLS payload load、fencing-token completion、篡改/跨租户拒绝、签名重放、双人批准及 ACL 快照变化拒绝。迁移 `0070` 已在隔离 PostgreSQL 完成 upgrade、downgrade-to-base、one-step rollback/re-upgrade 与 FORCE RLS 扫描。R2-03/worker/迁移定向套件 24 passed；Ruff、Mypy 通过，GitHub CI/staging 仍未执行。
