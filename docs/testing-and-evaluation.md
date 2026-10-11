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

## 本地语义评测基线

安装可选依赖：`python -m pip install -r requirements-evals.txt`。本地确定性基线使用现有 `semantic-v2` 合成 holdout，不调用被测模型或 LLM-as-judge：

```bash
bash scripts/run_local_evals.sh tests/evals/test_rules_baseline.py --identifier semantic-v2-rules-holdout-r1
```

该命令生成固定数据集上的规则路由分数和 DeepEval 本地 trace；报告与 trace 写入 Git 忽略的 `tests/artifacts/`，权限为仅当前用户读取。脚本禁用 dotenv、旧 keyfile、Confident AI 上传与 SDK 遥测。DeepEval 精确匹配指标的阈值为 0，仅用于记录基线分数，不作为质量门禁；pytest/DeepEval 的测试 pass rate 不代表质量通过，需查看 exact-match 与切片分数。数据集仍是模板合成且未独立人工审核，因此报告会明确标成不可用于生产质量声明。被测模型、端到端工具/业务结果以及 live provider 成本需通过获批的模型评测或独立集成集继续验收。

同案例工具轨迹 smoke eval 使用实际 Workbench API、Tool Gateway 与 PostgreSQL receipt，执行器为合成只读订单 fixture。它只可运行在新建隔离 PostgreSQL 上，并需显式设置 `APP_TEST_DATABASE_ISOLATED=1`；`APP_DATABASE_APP_URL` 与 `APP_TEST_DATABASE_URL` 必须使用 `platform_app` 非 owner 角色。测试会拒绝开发库 `platform`，且临时库应由测试 harness 在连接关闭后删除。示例命令（在已配置隔离测试环境的进程中）：

```bash
bash scripts/run_local_evals.sh apps/api/tests/integration/test_semantic_task_read.py \
  --mark eval --identifier synthetic-task-trajectory-r1
```

该 smoke case 不代表独立人工 holdout、真实 ERP 认证或真实 provider 延迟/费用测量。

## 任务级与线上结果指标

`platform_core.evaluation.task_trajectory` 提供确定性任务轨迹评分和 `build_task_quality_record`。汇总器要求语义、QA、轨迹等组件都带相同的安全 `case_id`；组件失败得到 `failed`，缺少必需观测得到 `incomplete`，两者都不会被折成通过。`test_semantic_task_read.py` 的冻结合成案例现在在同一个 `case_id` 下聚合确定性 semantic route、来自已验证 receipt 且逐字出现在证据中的 claim/citation，以及 Workbench/Gateway/PostgreSQL 最终任务轨迹，并将三项连同 task-quality record 写入本地 DeepEval trace。该 citation_support 用例证明的是严格 exact-span fixture；它不证明模型一般自然语言蕴含。通用 claim entailment、独立人工 holdout 和真实客户轨迹仍是独立验收工作。DeepEval 本地 trace/JSON 仅用于合成调试，没有 LLM-as-judge 或托管上传。

`POST /v1/support/resolution-feedback/requested` 记录问题确实展示，`POST /v1/support/resolution-feedback` 记录客户明确回答；两者都由 visitor token 绑定会话、需幂等键、写入 tenant-RLS 追加事件并审计。`GET /v1/quality/outcomes` 区分确认、拒绝、等待和成熟后无回复；无回复永不算作确认。同工单再次联系只统计服务端能验证同一联系人、同一 Case 的后续会话；未建立关联的事件不被算成“没有再次联系”。

`POST /v1/quality/reviews/batches` 建立可复现的风险分层样本，并可选限定租户内的 `target_prompt_version_id`；抽样、决策与最终证据均绑定租户及幂等键。reviewer 由服务端身份与 `case.review` 权限解析，agree/override 和受限原因码以追加事件保存并审计。只有全部抽中项有结论、且每个活跃分层都有样本时，`human_review_metrics.summarize_stratified_review` 才按真实抽样层和 `population / selected` 权重给出 override 率，否则返回 null/unavailable。Finalized evidence v2 是不可变哈希快照，含 prompt/code/policy 版本范围、样本量、加权结果和受限 override 原因计数，不含 prompt、答案或自由文本 reviewer notes。

Prompt promotion 在 tenant-bound 事务中读取并验证候选版本的最新人工复核证据。默认门槛为最近 30 天、总体至少 30 条或低流量全量审核、每个活跃分层至少 5 条或该层全量、安全类 override 原因 `unsafe_action`/`unsupported_claim`/`citation_gap` 为零、总体加权 override 率不超过 10%。门禁失败时发布前不改 active 版本，并写拒绝审计事件；rollback 仍保留紧急恢复路径。A/B 实验支持按 arm 指定 Prompt UUID；服务端验证租户归属，并将所有启用实验中未发布 Prompt 的合计曝光限制为 10%，用于小流量收集审核样本。

这些阈值是首轮代码默认值，不是经独立人工 holdout 或真实客户流量校准的生产 SLO。Prompt 发布 API 中的 evaluation summary 仍是调用方声明，外部 CI 签名和 artifact provenance 尚未接入，因此这些外部门槛没有通过。

semantic 评测的 `business_goal_cost` 按成功目标对应的 provider token 用量和显式配置单价估算每个已验证成功目标的费用；失败请求的账单若 provider 不返回 usage，则标为 unavailable。它不能代替账单对账，也不会在模型未运行时伪造测量值。
