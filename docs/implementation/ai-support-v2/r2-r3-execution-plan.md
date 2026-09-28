# R2 / R3 实施计划与验收门禁

日期：2026-09-27

基线：R1 修复分支 `83968f38acfaebf907ef9e56708e2100dbe99db1`（GitHub CI 已通过）。本分支基于该提交继续，不把未完成的生产门禁伪装成通过。

## R1 尚未关闭的门禁

R1 的实现 head `247290e5df86c529dde17465efaab64765439764` 和文档 head `5a077649faccf6cca66ac8a8206bd37def61cd76` 已通过 CI；下面项目仍需真实审查或环境，不能靠本地 stub 替代：

| 门禁 | 状态 | 完成所需条件 |
|---|---|---|
| DOC-01 / ADR | Proposed | 产品/安全负责人正式接受或修改 ADR |
| EVAL-01 / EVAL-02 | 阻塞 | 两位独立领域/安全审阅人复核语料、分歧裁决；锁定后运行当前 `semantic-v7` holdout |
| PERF-01 / PERF-02 | 未执行/阻塞 | 批准的生产近似拓扑、负载目标、真实模型延迟与成本观测 |
| SEC-02 / SEC-03 | 部分完成 | `test_orchestrator_lease_race.py` 已在隔离 PostgreSQL 中用独立应用会话验证“生成挂起时人工接管、恢复后无 AI 外发”；真实 OIDC 撤权、不同坐席身份的浏览器旅程和外部 transport 故障对账仍待演练 |
| OPS-01 / OPS-02 | 部分完成 | 多主机 Worker/外部写入对账故障、staging 应用回滚和待确认动作故障注入；隔离库子进程 hard-exit 后恢复已通过 |
| UI-01 / UI-02 / UX-03 | 部分完成 | 真实 200% 浏览器缩放、逐规格截图、屏幕阅读器和双窗口走查 |
| TOOL-03 / R3 | 无真实业务连接器 | 由用户提供授权的 ERP/CRM sandbox、字段契约和归属证明 |

## R2：服务流程与运营能力

### R2-01 情绪趋势与优先级建议

- 先实现确定性、可解释的文本证据和最近 5 条客户消息的趋势摘要。引用/转述和显式否定不作为当前情绪证据；讽刺/混合信号标记为不确定，建议人工看原文。
- 结果只给出 `monitor` / `review` / `urgent_review` 等建议、趋势方向、reason code 和 turn span；不得自动改 Case priority、SLA、所有权、赔付或客户回复。
- 通过新 tenant flag 默认关闭。队列建议排序只影响当前展示页，必须随项显示排序理由，不重写数据库顺序。
- `support_admin` / `tenant_owner` 可对建议作分类纠正；写入租户隔离、幂等、append-only 审计，理由使用枚举，禁止复制客户原话。
- 验收覆盖中文/英文、强情绪、降温、否定、引用、讽刺、反例和跨轮趋势；引用/否定/讽刺误报分别统计。用主管纠正数据衡量精度，但不把纠正样本自动用于训练。
- 当前：确定性规则、默认关闭的 tenant flag、队列/详情 API、当前页排序、主管更正的幂等审计存储与队列/详情 UI 已实现；RLS、无 UPDATE/DELETE 授权、超期建议拒绝、同键重放和跨租户 API 验收通过。详细契约与本地证据见 `r2-01-emotion-advice-contract.md`。生产 flag 保持关闭；实现提交 `25e6c21` 的 PR CI run #36378299874 全部通过；浏览器与辅助技术验收仍未完成。

### R2-02 四种标准流程模板

模板：查订单、报修/质量问题、发票申请、技术升级。每个模板必须声明：任务意图、场景/产品线、所需字段和来源、可用能力白名单、写入风险、确认步骤、超时/取消/部分完成、人工退出条件和负责人角色。

- 查询只读工具通过 Tool Gateway；写任务只生成绑定参数版本的提案，待人工确认，不绕过已有 proposal/confirmation/postcondition。
- 当前租户没有对应工具或业务负责人时，模板显示 `unsupported`/`needs_human`，不显示假成功。
- 多意图按子任务推进；已完成部分不重复执行，依赖阻塞时不跳过前置条件。
- 验收：模板 schema 合同测试、每模板状态机测试、权限负例、同键重放/不同 payload 冲突，以及浏览器补参/取消/人工接续。
- 当前：四种模板可由当前会话 owner 发起真实 task，服务端绑定最新 customer turn、模板 key/version 和 append-only 幂等 receipt；字段采集、取消/转人工写事件并经 lease 复核。实例停在专用 `manual_flow`（不进入 scheduler），默认关闭 flag `agent.standard_flow_instances`。`invoice_application` 有一个受限本地路径：从现有关联 Case 唯一解析 tenant account 后，可经 Tool Gateway 创建需独立确认的内部 `case.create`；撤回、并发执行和 verified receipt 回写已有验收。它不调用开票系统。订单查询、质量和技术升级的 executor、产品字段权威核验与真实连接器仍待完成。详细范围见 `r2-02-standard-flow-catalog.md`。

### R2-03 知识改进追踪

- 扩展现有 `knowledge_gaps` / `knowledge_drafts` 到“缺口 → 审核草稿 → 固定语料前测 → 双人审核 → 发布/灰度 → 同口径后测 → 回滚/关闭”的可追溯闭环。
- 评测绑定数据哈希、知识版本、检索配置和 commit；新知识导致核心切片退化时禁止推广，并能恢复上一版活动版本。
- 只允许审核后的知识文档参与检索；任何未审核会话都不得自动生成或发布知识。
- 验收：发布前后相同授权语料、tenant RLS、可回滚版本、指标审计，以及并发发布冲突。
- 当前：评测记录/批准/post-test/event 已持久化并强制 RLS；新增候选暂存、ACL-aware overlay、固定集加载校验、candidate-aware 前后测 runner、Ed25519 签名持久化、验签后的双人批准与 snapshot 复核发布。专用 outbox worker 已实现 owner-only claim、tenant RLS payload、fencing token、stale recovery 与三次有界重试；API publish 可事务化排队后测。自动运行默认关闭，每次样本 ceiling 默认 0；真实获批数据、对象 key、公钥/worker 私钥、费用预算尚未配置，因此无生产评测分数或自动模型调用。staging 灰度/多实例演练未做；2pp 阈值仍需知识/安全负责人签字。见 `r2-03-knowledge-release-contract.md`。

## R3：行业业务联通

### R3-01 ERP/CRM 与受控业务写入

- 统一 adapter contract：tenant connector、credential_ref、目标记录归属验证、字段 allowlist、超时与 bounded retry、breaker、稳定 idempotency key、受控 confirmation 和读回 postcondition。
- 对超时/断连返回 `unknown` 并提供对账流程；不得把“请求已发出”写成“业务成功”。外部 payload 只在 adapter 边界做 canonical projection。
- 先实现 fake contract tests 与协议文档；真正 provider adapter 需有用户授权的 sandbox、版本化 REST schema、归属证明和重放预算。
- 当前：已新增覆盖九种 authority domain 的 canonical read schemas（账户、订单、发票、工单、物流、商机、产品、库存、报价）、供应商中立的来源/时效/归属验证，以及携带 proof 的 `CanonicalBusinessReadResult` / `read_verified_fact()` adapter seam 和假 provider 边界测试。各业务域权威系统仍待企业负责人定版；具体 ERP/CRM API、授权 sandbox、生产 adapter、归属证明实施和验收仍未提供。

### R3-02 售前选型与商机交接

- 定义产品目录、规格、库存、报价版本/有效期和销售交接契约；每个建议带来源与抓取时间，缺失或过期时转人工。
- 仅使用企业授权的权威产品/ERP/PLM 数据；现有公开参考价不能伪装成客户报价，不允许模型生成折扣、交期或库存承诺。
- CRM 商机创建是受控写入，经 Tool Gateway proposal/confirmation/idempotency/读回；没有真实 sandbox 时仅测 canonical contract，不宣称真实落地。
- 当前：契约禁止无来源的客户报价、库存和交期承诺；具体产品目录、库存和报价权威绑定尚未选定，因此没有真实售前推荐或 CRM 商机写入。

## 集成发布顺序

1. 完成 R2-01 证据服务和人审反馈边界，继续保持 tenant flag 默认关闭。
2. 完成 R2-02 流程目录和状态机，先支持现有的查询/建工单/转人工能力。
3. 完成 R2-03 知识版本前后评测与回滚保护。
4. 收到 ERP/CRM、身份和数据来源后，再实施 R3 provider adapter 和 sandbox 验收。
5. 每阶段单独 CI、合约/跨租户/幂等测试及交付报告；生产放量仍需 R1 阻塞门禁全部关闭。
