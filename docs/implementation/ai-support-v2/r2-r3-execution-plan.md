# R2 / R3 实施计划与验收门禁

日期：2026-09-29

基线：R1 修复分支 `83968f38acfaebf907ef9e56708e2100dbe99db1`（GitHub CI 已通过）。本分支基于该提交继续，不把未完成的生产门禁伪装成通过。

## R1 尚未关闭的门禁

R1 的实现 head `247290e5df86c529dde17465efaab64765439764` 和文档 head `5a077649faccf6cca66ac8a8206bd37def61cd76` 已通过 CI；下面项目仍需真实审查或环境，不能靠本地 stub 替代：

| 门禁 | 状态 | 完成所需条件 |
|---|---|---|
| DOC-01 / ADR | Proposed | 产品/安全负责人正式接受或修改 ADR |
| EVAL-01 / EVAL-02 | 阻塞 | 两位独立领域/安全审阅人复核语料、分歧裁决；锁定后运行当前 `semantic-v7` holdout |
| PERF-01 / PERF-02 | 本地部分通过 / 生产阻塞 | 本地 off/shadow 各 3 轮、并发 50/100 的 warm-pool inbox 与 shadow enqueue 对照已归档；真实模型 p95/成本、10k 会话/租户、持续负载与生产近似拓扑仍需环境和预算 |
| SEC-02 / SEC-03 | 部分完成 | 本地 Keycloak 实签 JWT 在 active membership 下请求 200；暂停 membership 后复用同一 JWT，下一请求 401。`test_orchestrator_lease_race.py` 与两个 Chrome origin/token 并发认领 smoke 继续通过。真实企业 IdP token/session 撤销与外部 transport timeout/unknown 对账仍待演练 |
| OPS-01 / OPS-02 | 部分完成 | 新增隔离 Docker Compose 两 Worker 演练：处理中的 Worker 被杀后 peer 恢复；重复 outbox 仅一条账务行；1/10/50 事件批次排空；49 项集成/单测通过；0070 单步回滚往返保留数据。真实 staging、多主机 Worker、provider/connector 故障和外部 unknown 对账仍待授权环境 |
| UI-01 / UI-02 / UX-03 | 部分完成 | 非空合成 Workbench 的桌面/移动/320px/CSS 200% 截图已归档，均无水平溢出；桌面与移动 axe WCAG 2/2.1 A/AA 为 0 violations，键盘 Enter、输入名称/描述和 live status 通过。完整原生 VoiceOver 朗读/键盘走查与弹层焦点仍需真人复核 |
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
- 当前：四种模板可由当前会话 owner 发起 task，服务端绑定最新 customer turn、模板 key/version 和 append-only 幂等 receipt；默认关闭 flag `agent.standard_flow_instances`。`order_status` 在 local/test Demo 模式下经 Tool Gateway 查询并把 verified receipt 回写 task；订单 owner 不匹配时拒绝。`invoice_application` 只能创建需确认的平台内部 Case，不会开票。`repair_quality_intake` / `technical_escalation` 在 Demo authority 下校验账户映射与合成产品 owner，需租户 staffed Department、受控提案/人工确认；执行时重核归属并读回 Case，PII-shaped 联系方式在任务/Case 前脱敏。这不代表连接真实 ERP、质量或工程系统。真实业务 authority、外部连接器及真实 staging 验收仍待完成。详细范围见 `r2-02-standard-flow-catalog.md`。

### R2-03 知识改进追踪

- 扩展现有 `knowledge_gaps` / `knowledge_drafts` 到“缺口 → 审核草稿 → 固定语料前测 → 双人审核 → 发布/灰度 → 同口径后测 → 回滚/关闭”的可追溯闭环。
- 评测绑定数据哈希、知识版本、检索配置和 commit；新知识导致核心切片退化时禁止推广，并能恢复上一版活动版本。
- 只允许审核后的知识文档参与检索；任何未审核会话都不得自动生成或发布知识。
- 验收：发布前后相同授权语料、tenant RLS、可回滚版本、指标审计，以及并发发布冲突。
- 当前：评测记录/批准/post-test/event 已持久化并强制 RLS；候选暂存、ACL-aware overlay、固定集加载校验、candidate-aware runner、签名证据与双人批准仍保留。额外复验的语义合成数据/runner/release gates 33 passed，知识发布集成门禁 1 passed，证实未有平台来源的自报评测返回 `EVALUATOR_PROVENANCE_UNAVAILABLE`。测试数据、模型回答和密钥均为 synthetic/fake；不代表独立人工批准或真实模型成绩。自动运行/发布 flag 默认关闭，真实获批语料、key/预算以及真实 staging 多实例/rollback 仍阻塞。见 `r2-03-knowledge-release-contract.md`。

## R3：行业业务联通

### R3-01 ERP/CRM 与受控业务写入

- 统一 adapter contract：tenant connector、credential_ref、目标记录归属验证、字段 allowlist、超时与 bounded retry、breaker、稳定 idempotency key、受控 confirmation 和读回 postcondition。
- 对超时/断连返回 `unknown` 并提供对账流程；不得把“请求已发出”写成“业务成功”。外部 payload 只在 adapter 边界做 canonical projection。
- 先实现 fake contract tests 与协议文档；真正 provider adapter 需有用户授权的 sandbox、版本化 REST schema、归属证明和重放预算。
- 当前：已新增覆盖九种 authority domain 的 canonical schemas、来源/时效/归属验证和 fake-provider boundary tests；新增 local/test-only `DemoCanonicalBusinessAdapter`，九域全部经 `read_verified_fact()` 校验并标记 `source_version=demo-fixture-v1`。shipped Demo ERP 另经 Tool Gateway 完成 synthetic order/stock read；标准订单流程也记录 verified Demo receipt。该模拟验证平台路径，不替代真实 ERP/MES/WMS/CRM authority、sandbox 或客户归属证明。

### R3-02 售前选型与商机交接

- 定义产品目录、规格、库存、报价版本/有效期和销售交接契约；每个建议带来源与抓取时间，缺失或过期时转人工。
- 仅使用企业授权的权威产品/ERP/PLM 数据；现有公开参考价不能伪装成客户报价，不允许模型生成折扣、交期或库存承诺。
- CRM 商机创建是受控写入，经 Tool Gateway proposal/confirmation/idempotency/读回；没有真实 sandbox 时仅测 canonical contract，不宣称真实落地。
- 当前：契约禁止无来源的客户报价、库存和交期承诺；local/test Demo canonical facts 可在 Workbench 只读查看，证据包同时绑定产品、账户库存、客户报价有效期，且只有具备 `case.read` 与 `tool.read` 的坐席可查看。另增加仅限 local/test + `business_api_adapter=demo` 的 `crm.create_opportunity` 模拟工具，经 Tool Gateway 执行权限检查、人工确认、幂等提案/执行、异 payload 冲突拒绝和模拟 CRM 读回。回执明确 `synthetic=true`、不联系客户且不含报价金额；这不代表真实 CRM 集成、真实 authority 或 sandbox。

## 2026-09-29 本地合成模拟结果

| 工作 | 结果 | 仍需真实/人工证据 |
|---|---|---|
| CRM 写入闭环 | Tool Gateway 模拟机会创建：权限、确认、幂等、读回与审计路径已集成验证 | 真实 CRM、获批 authority 与 sandbox |
| Worker 与恢复 | 两个 Compose Worker；中断恢复、重复投递、1/10/50 队列批次及单步迁移回滚；49 项用例通过 | 多主机 staging、真实 provider/connector 失败与 unknown 对账 |
| off/shadow 性能 | 两种模式、50/100 并发、每组 3 轮；off 未新增影子项，shadow 新增 450 条 synthetic outbox 请求 | 生产拓扑/容量、真实模型和持续负载 |
| Keycloak 撤权模拟 | 本地签名 token 的请求从 200 变成 401（暂停 membership 后复用原 token） | 企业 IdP 自身 token/session 撤销 |
| 浏览器与无障碍 | 5 张 PNG、ARIA snapshot、两视口 axe 0 项违规；横向溢出均为 0 | 完整真人 VoiceOver 走查 |

证据目录：`evidence/local-worker-drill-2026-09-29/`、`evidence/local-perf-2026-09-29/`、`evidence/local-keycloak-2026-09-29/`、`evidence/local-browser-2026-09-29/`。

## 集成发布顺序

1. 完成 R2-01 证据服务和人审反馈边界，继续保持 tenant flag 默认关闭。
2. 完成 R2-02 流程目录和状态机，先支持现有的查询/建工单/转人工能力。
3. 完成 R2-03 知识版本前后评测与回滚保护。
4. 收到 ERP/CRM、身份和数据来源后，再实施 R3 provider adapter 和 sandbox 验收。
5. 每阶段单独 CI、合约/跨租户/幂等测试及交付报告；生产放量仍需 R1 阻塞门禁全部关闭。
