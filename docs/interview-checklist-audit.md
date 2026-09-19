# AI 客服面试问题清单 — 项目能力审计

对照清单：`C:/Users/Rose/OneDrive/Desktop/1.txt`（132 条，13 组）
审计对象：本仓库 `b2b-ai-support-plan`
审计日期：2026-09-18

---

## 0. 审计方法与阅读说明

### 结论口径

| 标记 | 含义 |
|---|---|
| ✅ **已实现** | 有生产代码路径（不只是测试或文档），边界条件与异常处理齐备，达到可上线级 |
| ⚠️ **实现不到位** | 有代码但停留在 Demo 级：硬编码、无配置项、无异常分支、有产出方无消费方，或缺边界处理 |
| ❌ **完全缺失** | 仓库中不存在对应能力，或只存在于文档/注释里而没有任何代码路径 |

**判断依据一律给到具体文件**，便于逐条复核。

### 一个贯穿全局的结构性发现

本仓库反复出现同一种缺陷形状——**文档描述了、代码没实现**。最典型的一处：

> `docs/agent.md` 明确规定了 **7 个路由类别**（`KNOWLEDGE_QA` / `CASE_STATUS` /
> `BUSINESS_READ` / `BUSINESS_WRITE` / `SENSITIVE` / `OUT_OF_SCOPE` /
> `HUMAN_REQUIRED`）和 **7 层上下文**（含「近期对话轮次」「更早的摘要记忆」）。
>
> 审计前，代码里只有 **2 个**路由类别（`orchestrator.classify_route` 是一条
> 关键词二分类）和 **1 层**上下文（单个 `question: str`）。

因此本次审计对每一条都做了「grep 文档名词 → 对照 `apps/api/src`」的验证，而不只看
有没有相关文件。**有文档不等于有实现。**

### 关于"本次已改动"的说明

在切换到"仅调研"之前，我已对最高优先级的两个缺失项做了实现（第 6 节如实列出）。
这些改动**尚未完成验证**，其中 `test_intent.py` 有 4 条断言当前失败。这是遗留问题，
不是结论。清单其余条目均为纯调研。

---

## 1. 总体结论：能力雷达

| 能力维度 | 现状 | 量级判断 |
|---|---|---|
| 租户隔离 / RLS / 权限 | ✅ 可上线级 | 全表 FORCE RLS + 48 个集成测试 + 跨租户负向测试 |
| 审计 / 可追溯 | ✅ 可上线级 | append-only 审计 + 引用溯源（chunk/版本/摘录哈希） |
| 工具调用安全（授权/确认/幂等/后置校验） | ✅ 可上线级 | 5 级 risk + 确认绑定 action_hash + UNKNOWN 不上报成功 |
| 转人工 / 人机竞态 | ✅ 可上线级 | 控制租约 + 发送前复检 + 弃权通知 |
| 超时重试 / 熔断降级 | ✅ 可上线级 | 断路器 + 退避 + rerank 硬 deadline + 降级可观测 |
| 幻觉抑制（约束层） | ⚠️ 接近上线 | 引用校验 + 弃权已到位；**引用"存在性"校验 ≠ "支持性"校验** |
| RAG 检索链路 | ⚠️ Demo+ | 混合检索 + RRF + rerank 全链路齐，但**参数全硬编码、无 overlap、无 query rewrite** |
| 知识生命周期 | ⚠️ Demo+ | 版本/过期/状态机齐；冲突解决与可信度评估缺 |
| 评测体系 | ⚠️ Demo | 23 用例 + 发布门禁；**无法区分"检索错"与"生成错"** |
| 意图识别 | ❌→⚠️ 本次补 | 审计前是二分类；本次补了 7 类体系（未接线生产） |
| 多轮对话 / 记忆 / 上下文压缩 | ❌→⚠️ 本次补 | 审计前**完全不存在**；本次补了模块（未接线生产） |
| 成本 / 延迟治理 | ⚠️ Demo | 有预算截断与计量，**无流式、无缓存、无大小模型路由** |
| 业务工具生态 | ⚠️ Demo | 只注册了 4 个工具，**无订单/物流/退款类业务查询能力** |

**一句话结论**：安全与治理维度已明显超过清单要求（可上线级）；RAG 与 Agent 的
"智能"维度（多轮、意图、工具编排、检索调优、评测归因）停留在 Demo 级，清单第三、
五、六、九组是主要缺口区。

---

## 2. 逐条对照表

### 第一组：项目背景、业务价值与个人贡献（1–12）

> 本组是叙述性问题，代码无法直接回答。评估口径改为：**仓库里有没有可支撑讲述的
> 一手材料**（不是能不能编出来）。

| # | 问题 | 状态 | 依据 | 差距与处理动作 |
|---|---|---|---|---|
| 1 | 介绍做过的 AI 客服项目 | ✅ | `overview.md`、`README.md`、`docs/development-plan.md`（Phase 0–5） | 有完整交付报告，可直接讲述 |
| 2 | 解决的核心业务问题 | ✅ | `docs/architecture.md`、`AGENTS.md` Mission | 定位清晰：企业级可信 AI 客服 |
| 3 | 为什么需要 LLM，传统方案不够 | ⚠️ | 无专门论述 | 缺 ADR 或章节对比规则机器人 vs LLM。建议补 `docs/adr/` 一条 |
| 4 | 目标用户与原有流程痛点 | ⚠️ | `docs/agent.md` 运营模式表隐含 | 未显式写出"人工坐席原流程"。建议补 persona 章节 |
| 5 | 个人负责什么 | ❌ | 仓库无贡献记录可区分 | 叙述项，需本人作答；可从 git log 取 |
| 6 | 最终效果与业务价值证明 | ⚠️ | `evaluation/metrics.py` 五指标 + `tests/artifacts/eval_report.json` | 有指标体系，但**无真实线上业务数据**；只能讲离线指标 |
| 7 | 团队分工与协作 | ❌ | 无 | 叙述项 |
| 8 | 需求如何发现与验证 | ⚠️ | `docs/development-plan.md` 有阶段划分 | 缺需求发现过程的记录 |
| 9 | 最大挑战与解决 | ✅ | 代码注释里有多处"found by"真实缺陷记录（可当素材） | 素材充足 |
| 10 | 上线后与预期不一致的问题 | ⚠️ | `overview.md` 提到若干被真实链路发现的缺陷 | 非真实上线，是 e2e 发现 |
| 11 | 重做一次会怎么改 | ❌ | 无 | 叙述项 |
| 12 | 后续优化空间 | ⚠️ | `AGENTS.md` Prohibited shortcuts 隐含 | 可提炼 |

### 第二组：为什么做 AI 客服、产品怎么设计（13–22）

| # | 问题 | 状态 | 依据 | 差距与处理动作 |
|---|---|---|---|---|
| 13 | 从零设计 AI 客服系统 | ✅ | `docs/architecture.md`、`docs/agent.md` 请求流水线 | 设计文档完整 |
| 14 | AI 客服 vs 规则机器人 | ⚠️ | 无对比文档 | 代码侧有"确定性代码拥有一切闸门"理念。建议补一节论述 |
| 15 | 哪些场景适合/不适合 AI | ⚠️ | `docs/agent.md` 路由类别部分覆盖 | 无显式"不适合 AI"清单。建议补 |
| 16 | **售前/售后/投诉/订单查询拆分** | ❌→⚠️ | 审计前无任何场景维度；本次新增 `intent.Scene`（8 类） | **本次已补枚举与词表，未接线生产** |
| 17 | **知识问答/业务查询/业务操作分别处理** | ⚠️→✅ | 审计前 `classify_route` 只有 2 类；本次新增 `IntentKind` 7 类并映射到 7 个路由类 | **本次已补，未接线生产** |
| 18 | 何时回答/澄清/拒答/转人工 | ✅ | `qa_path.decide_abstention`（6 种弃权原因）+ `safe_abstention_text`；本次补澄清路径 | 已覆盖；澄清路径本次新增 |
| 19 | MVP 应包含哪些能力 | ✅ | `docs/development-plan.md` Phase 0–5 | 有完整路线图 |
| 20 | C 端 vs B 端客服差异 | ⚠️ | 项目本身是 B2B，无对比分析 | 建议补一节 |
| 21 | 单 Agent 还是多 Agent | ❌ | 无任何决策记录 | 需补 ADR。当前实现是**单 Agent 固定流水线**（这是事实上的答案，但无论证） |
| 22 | 优先做三个场景 | ❌ | 无 | 建议结合 Scene 词表给出优先级论证 |

### 第三组：RAG 核心（23–40）— 主要缺口区

| # | 问题 | 状态 | 依据 | 差距与处理动作 |
|---|---|---|---|---|
| 23 | 为什么需要 RAG | ✅ | `docs/architecture.md` 检索架构 | 有 |
| 24 | **完整 RAG 链路** | ✅ | `knowledge/ingest.py`（解析→切片→状态机）→ `retrieval/hybrid.py`（FTS+向量+RRF）→ `retrieval/reranker.py` → `agent_runtime/generator.py` → `qa_path.validate_citations` | 链路完整可讲 |
| 25 | RAG vs Fine-tuning | ❌ | 无 ADR/文档 | 建议补 ADR（清单第七组也考） |
| 26 | **Chunk 怎么切，size 怎么定** | ⚠️ | `ingest.chunk_sections`：`MAX_CHUNK_CHARS = 1200` 常量，按段落边界切 | **硬编码、无实验依据、无 overlap**；需改为可配置并记录调优依据 |
| 27 | Embedding 模型怎么选 | ⚠️ | `config.py`：`Qwen3-Embedding-8B`，1536 维 | 有选型，**无选型依据/评测对比文档** |
| 28 | 召回策略（向量/关键词/Hybrid） | ✅ | `hybrid.py`：pgvector + `websearch_to_tsquery` + RRF(k=60)，双路各取 40 候选 | 可上线级 |
| 29 | 为什么需要 Rerank | ✅ | `reranker.py`：cross-encoder + 硬 deadline + `degraded` 契约；由 `agent.rerank_enabled` flag 灰度 | 有降级可观测，设计到位 |
| 30 | 召回不准如何定位优化 | ⚠️ | `retrieval/router.py` 诊断端点 + `RetrievedChunk.ranking` 保留各路分数 | 有诊断面，**无线上召回质量指标**（recall@K、MRR） |
| 31 | **Top-K 怎么定** | ⚠️→✅ | 审计前 `top_k=8` 写在调用处字面量；本次改为 `OrchestratorDeps.top_k` | **本次已改可配置** |
| 32 | **Chunk overlap** | ❌ | 全仓库无 overlap 概念（grep `overlap` 只命中打分函数） | **完全缺失**，需实现并给出取值依据 |
| 33 | **Query Rewrite** | ❌→⚠️ | 审计前无；本次新增 `conversation.rewrite_query`（指代消解 + 省略补全，确定性、可审计） | **本次已补，未接线生产** |
| 34 | 口语化/模糊怎么提升召回 | ⚠️ | FTS 用 `simple` 词典；无模糊匹配、无同义词、无拼写纠错 | 需补 typo 容错或查询扩展 |
| 35 | **Metadata Filter 设计** | ⚠️ | `hybrid_search` 支持 `knowledge_space_ids`；`documents.metadata` / `chunks.metadata` 是 JSONB 但**从未用于过滤** | 字段已存在无消费方——典型仓库缺陷形状，需接线 |
| 36 | 多文档才能回答 | ⚠️ | `DraftAnswer.claims[i]` 可引用多个 chunk；但 `uq_citation_claim` 是 `(run_id, claim_index)`，**落库只存第一条**（`_persist_citations` 有 `break`） | 与 `docs/agent.md`「一条声明可由一个或多个引用支持」不符；需在 `retrieval_config` 记录其余支持 chunk |
| 37 | 表格/PDF/图片 | ⚠️ | PDF（`pypdf`）、DOCX（`python-docx`）文本抽取已实现 | **表格结构化缺失**（按纯文本切会破坏表格语义）；**图片/OCR 完全缺失** |
| 38 | **分别评估 Retrieval 与 Generation** | ❌ | `evaluation/runner.py` 只做端到端（`passed` 布尔） | **无召回指标、无归因**，第九组同样命中 |
| 39 | 降低 RAG 延迟 | ⚠️ | rerank 有 2s deadline；检索/模型延迟有 histogram | 无缓存、无并发控制、无向量索引参数调优记录 |
| 40 | 降低 Token / 成本 | ⚠️ | `MAX_EXCERPT_CHARS=700`、`MAX_TOTAL_EVIDENCE_CHARS=6000`、`token_usage` 落库 + 计费账本 | 有计量与截断；**无 prompt 缓存、无大小模型路由、无成本预算闸门** |

### 第四组：知识库与知识生命周期（41–50）

| # | 问题 | 状态 | 依据 | 差距与处理动作 |
|---|---|---|---|---|
| 41 | 知识从哪来 | ✅ | `knowledge_sources` + 上传 + MinIO 原始件 + 连接器同步 | 有 |
| 42 | **清洗/结构化/切片/标签** | ⚠️ | 切片（结构感知，保留标题层级 `section_path`）有；**清洗无**（无去重/规范化/噪声移除）；**标签无** | 缺清洗与打标环节 |
| 43 | 过期/错误/冲突怎么办 | ✅ | `document_versions.effective_at/expires_at/status` + 检索期过滤；`qa_path._sources_compete` 冲突检测 | 可上线级 |
| 44 | 上线/更新/版本/下线 | ✅ | `DocumentVersion` + `SUPERSEDED`/`EXPIRED` + `ingest` 状态机 + `uq_version_label` | 可上线级 |
| 45 | 谁负责维护 | ⚠️ | RBAC/ABAC 有（`identity`），但无知识条目 owner 字段 | 缺 ownership 模型 |
| 46 | 更新后及时生效 | ✅ | 摄取状态机 + worker 消费 + 索引 | 有 |
| 47 | 多部门知识矛盾 | ⚠️ | `_sources_compete` 检测后转人工 | 只有"发现并转人工"，**无冲突解决工作流**（无合并/裁定/标注） |
| 48 | **判断知识是否可信** | ❌ | 无任何可信度/来源权威度字段 | 完全缺失 |
| 49 | 答案错误反查知识源 | ✅ | `Citation(document_version_id, chunk_id, excerpt_hash, source_uri)` + `knowledge/gap_service.py` | 可上线级，这条是亮点 |
| 50 | 完整 KLM | ⚠️ | `gap_service`：record → draft → review → publish → 关联知识源 | 有闭环骨架；**缺定期复审、过期预警、可信度** |

### 第五组：意图识别、多轮对话与 Memory（51–60）— 最大缺口区

| # | 问题 | 状态 | 依据 | 差距与处理动作 |
|---|---|---|---|---|
| 51 | **用户表达模糊怎么办** | ⚠️→✅ | 审计前只有"弃权转人工"；本次新增 `needs_clarification` + `_finish_clarify`（**不释放租约**，与转人工区分） | 本次已补，未接线生产 |
| 52 | **多轮 Context 怎么管理** | ❌→⚠️ | 审计前 `orchestrator.run` 只接 `question: str`，**全仓库无对话轮次存储**；本次新增 `conversation.ConversationMemory` | 本次已补；**生产未接线**（`inbox_consumer` 不传 history） |
| 53 | **短期/长期 Memory** | ❌→⚠️ | 审计前无；本次新增 `CompactedContext`（短期，带预算与钉住）+ `DurableFact`（长期） | 长期记忆**未持久化到库**，仅内存值对象 |
| 54 | **话题突然切换** | ❌→⚠️ | 本次新增 `topic_shifted`（相对新词比例） | 本次已补 |
| 55 | **Intent 分类体系** | ❌→✅ | 审计前是二分类；本次新增 `intent.py`（Scene 8 × Kind 7 → 7 路由类） | 本次已补，未接线生产 |
| 56 | **一个 Query 多意图** | ❌→✅ | 本次新增 `multi_intent` + `secondary_kinds`（**报告而非擅自消解**） | 本次已补 |
| 57 | **何时主动追问** | ❌→✅ | 本次新增 `needs_clarification`（4 种原因码）+ 澄清文案 | 本次已补 |
| 58 | **Conversation Summary** | ❌→⚠️ | 本次新增 `_summarize`（**确定性、可 diff、可回归**；刻意不用 LLM 摘要） | 本次已补；仅首行抽取式，质量有上限 |
| 59 | **哪些值得长期保存** | ⚠️ | 本次新增 `extract_durable_facts` + `_NEVER_DURABLE` + `_PII_SHAPED` 拒绝清单 + **只读客户轮次**（防模型自反馈） | 策略已定，未持久化 |
| 60 | 历史与当前冲突以谁为准 | ⚠️ | 本次实现"当前表达覆盖历史"（后写覆盖同 key） | 无持久化记忆即无实际冲突场景 |

**根因**：Chatwoot 客户端只有 `send_message` 与 `fetch_message` 两个方法
（`support_bridge/chatwoot_client.py`），**没有"拉取会话历史消息"的能力**；
`inbox_consumer.resolve_question` 只取单条消息正文。这是多轮能力无法落地的直接原因。

### 第六组：Agent、Workflow、Tool Calling（61–76）

| # | 问题 | 状态 | 依据 | 差距与处理动作 |
|---|---|---|---|---|
| 61 | 为什么需要 Agent 而非 LLM+Prompt | ✅ | `docs/agent.md`「LLM proposes, code disposes」+ `orchestrator` 逐道闸门 | 论述与实现一致 |
| 62 | Agent vs 固定 Workflow | ⚠️ | 实际实现是**固定流水线**；理念有，论证无 | 需补 ADR 说明为何选确定性编排 |
| 63 | 哪些用 RAG 哪些调 Tool | ⚠️→✅ | 本次 `IntentKind` 给出判定（KNOWLEDGE_QUESTION / BUSINESS_QUERY / BUSINESS_ACTION） | 判定已补，**未接线到工具执行** |
| 64 | **查订单/物流等实时信息** | ❌ | `tool_gateway/registry.py` 只注册 4 个工具：`jira.create_issue`、`linear.create_issue`、`crm.update_account`、`im.send_notification` | **无任何读类业务工具**（订单/物流/账单查询），清单这条无法演示 |
| 65 | **Agent 如何判断调哪个 Tool** | ❌ | 无工具选择逻辑；`tool_gateway/router.py` 由调用方显式指定 `tool_name` | 完全缺失，需实现 tool selection |
| 66 | Tool 失败/超时/异常 | ✅ | `integrations/resilience.py`（断路器+退避）+ `gateway.classify_execution_error`（区分第三方故障）+ `dead_letter.py` | 可上线级，设计优秀 |
| 67 | 自动执行 vs Human-in-the-loop | ✅ | `ToolRisk` 5 级 + `ActionConfirmation` 绑定 `action_hash` + 后置校验；`docs/agent.md` 有完整契约 | 可上线级，这条是亮点 |
| 68 | Agent Workflow 设计 | ✅ | `orchestrator._run_pipeline` 10 步 + 发送前租约复检 | 有 |
| 69 | 单 Agent vs Multi-Agent | ❌ | 无 | 需补决策记录 |
| 70 | Multi-Agent 价值 | ❌ | 无 | 同上 |
| 71 | 多 Agent 共享 Context | ❌ | 无 | 同上 |
| 72 | 执行中保证业务状态一致 | ✅ | 事务性 outbox + `ON CONFLICT DO NOTHING` 幂等 + 逐行 RLS 绑定 | 可上线级 |
| 73 | Skill 抽象 | ⚠️ | `ToolDefinition`（name/version/input_schema/risk/requires_confirmation）即 skill | 有抽象，**无 skill 编排/组合** |
| 74 | Skill 权限与版本管理 | ✅ | `registry.TOOL_CAPABILITY`（能力→连接器映射）+ policy 引擎 + 版本字段 | 可上线级 |
| 75 | **MCP 的价值** | ❌ | 全仓库无 MCP | 概念题；建议补 ADR 说明取舍（当前用自研 registry，理由是可审计+RLS） |
| 76 | 与 CRM/订单/工单集成 | ⚠️ | Jira/Linear/CRM/IM 适配器齐（`integrations/`） | **订单/物流缺失**；工单有 |

### 第七组：模型选型、Prompt 与 Fine-tuning（77–86）

| # | 问题 | 状态 | 依据 | 差距与处理动作 |
|---|---|---|---|---|
| 77 | 为什么选这个 Base Model | ⚠️ | `config.py`：`qwen3.8-flash` | 有选型，**无选型依据/对比评测** |
| 78 | **大模型 vs 小模型** | ❌ | 单一模型，无路由、无降级链 | 完全缺失；清单这条必考 |
| 79 | Prompt 怎么设计 / System Prompt 内容 | ✅ | `prompts.py` v3（7 条硬规则）+ 版本化 + `prompt_release.py` 发布流程 + `prompt_versions` 表 | 可上线级，优秀 |
| 80 | **Few-shot Example** | ❌ | 模板中无 few-shot | 完全缺失 |
| 81 | Prompt 优化解决 Bad Case 的案例 | ✅ | v2 的 rule 5 有完整成因记录（权威声称型注入） | 素材充足 |
| 82 | Prompt / RAG / FT 各自解决什么 | ❌ | 无文档 | 建议补 |
| 83 | 何时需要 Fine-tuning | ❌ | 无 | 同上 |
| 84 | 微调数据构建与质检 | ❌ | 无 | 完全缺失 |
| 85 | 如何证明 FT 有效 | ❌ | 无 | 完全缺失 |
| 86 | 不同场景用不同模型 | ❌ | 无 | 完全缺失 |

### 第八组：幻觉、风险与安全（87–96）

| # | 问题 | 状态 | 依据 | 差距与处理动作 |
|---|---|---|---|---|
| 87 | 为什么会产生幻觉 | ⚠️ | 无文档论述 | 代码侧防控充分，缺论述 |
| 88 | 从哪些层面降低幻觉 | ✅ | ① 检索前 ACL/租户过滤 ② 弃权闸门 ③ 引用存在性校验 ④ 温度 0 ⑤ 发送前租约复检 ⑥ 提示词规则 1/2/5 | 多层防控已到位 |
| 89 | 知识库没有答案怎么办 | ✅ | `decide_abstention` → `_finish_abstain` → **真的发送弃权通知**（`safe_abstention_text`）+ 转人工 | 可上线级 |
| 90 | 线上大量错误回答怎么应急 | ⚠️ | 有 flag kill switch + `prompt_release.rollback()`（免评估回滚） | 有手段，**无 incident runbook / 无按租户一键停用 AI 的运维入口** |
| 91 | 哪些是高风险场景 | ✅ | `SENSITIVE` 路由 + `RESTRICTED_TERMS` + 5 级 ToolRisk + `PROHIBITED` | 有 |
| 92 | **判断幻觉还是知识错误** | ⚠️ | `qa_path.claim_contradiction_candidates`（声明vs引用矛盾检测） | 已实现但**只作 metric 不作 guard**（注释说明精度未达标，刻意不拦截）——结论正确，但需在 eval 中产出精度数据 |
| 93 | **模型自信地答错** | ⚠️ | 同上 | 同上；且检索分数**明确不视为置信度**（`docs/agent.md`）——这是正确设计，但**没有任何置信度信号**可供 Fallback |
| 94 | **Confidence / Fallback 机制** | ⚠️ | Fallback 有（rerank 降级、模型不可用弃权、检索不可用弃权）；**Confidence 无** | 缺置信度分。本次 `IntentDetection.confidence` 是首个置信度信号（仅意图维度） |
| 95 | 哪些必须强制转人工 | ✅ | 受限请求 / 敏感 / 来源冲突 / 低证据 / 写操作 / 模糊身份 | 有 |
| 96 | 退款赔付账户安全防越权 | ✅ | `tool_gateway` propose→authorize→confirm→execute→**verify**→audit；`docs/agent.md` 有五段式契约 | 可上线级，优秀 |

### 第九组：Evaluation（97–112）— 主要缺口区

| # | 问题 | 状态 | 依据 | 差距与处理动作 |
|---|---|---|---|---|
| 97 | 怎么判断 AI 客服好不好 | ⚠️ | `metrics.py`：5 个指标（有效解决/错误解决/弃权/转人工/引用覆盖）+ 延迟 P50/P95 | 有；"有效解决"用 Case 是否被重开定义（设计好），但**无自动解决率** |
| 98 | 完整 Eval Framework | ⚠️ | `runner.py` + `gates.py` + `dataset.py` + `release_check.py` + `scripts/run_eval.py` | 骨架完整；缺 retrieval 单评、LLM-judge、人工评测 |
| 99 | Dataset 怎么构建 | ✅ | `tests/evals/dataset.py`：23 用例 / 11 类别 + 固定语料 + 强制 `case_id` | 可上线级 |
| 100 | Offline vs Online 评估 | ⚠️ | Offline：`scripts/run_eval.py` 真实链路；Online：`/v1/quality/metrics` | 有两侧，**无自动对比/漂移告警** |
| 101 | **分别评估 Retrieval / Generation / Tool** | ⚠️ | Tool 有（`aggregate_read_tool_outcomes` ≥99% 门禁，且排除第三方故障）；**Retrieval 无** | **需补召回指标** |
| 102 | 人工评测与自动评测结合 | ❌ | 无人工评测流程与界面 | 完全缺失 |
| 103 | **LLM-as-a-Judge** | ❌ | 无（`runner.py` 注释说"插在同一个接缝"，但从未实现） | 完全缺失 |
| 104 | 如何验证 Judge 本身 | ❌ | 无 | 完全缺失 |
| 105 | 覆盖真实用户不同表达 | ⚠️ | 23 用例含 typo、中/法文、对抗、注入 | 规模偏小，无真实日志采样 |
| 106 | Hard / Edge Case | ⚠️ | 有对抗与注入类别 | 数量少（各 2 条） |
| 107 | 模糊与对抗 Query | ✅ | `AMBIGUOUS_IDENTITY`、`INDIRECT_INJECTION`、`REPEATED_ADVERSARIAL` | 有 |
| 108 | Correctness / Relevance / Faithfulness | ⚠️ | Correctness：`required/forbidden_claims` 子串匹配；Faithfulness：引用可解析 + 矛盾候选 | **Relevance 无独立指标**；Faithfulness 未成门禁 |
| 109 | **"检索正确但生成错" vs "检索本身错"** | ❌ | 无任何归因能力 | **完全缺失**，需给 EvalCase 增加期望来源并在 runner 中归因 |
| 110 | 模型升级后如何回归 | ⚠️ | `release_check` + `prompt_release` 门禁 | 有门禁，**无模型版本切换的评价流程** |
| 111 | 如何判断新版本真的优于旧版 | ⚠️ | 门禁是阈值判定，非 A/B 对比 | 缺前后版本对比报告 |
| 112 | 上线验收标准 | ⚠️ | `release_check` 6 个门禁 | 缺"AI 客服上线"专属验收清单（安全/质量/成本/延迟/回滚） |

### 第十组：业务指标、Bad Case 与数据闭环（113–120）

| # | 问题 | 状态 | 依据 | 差距与处理动作 |
|---|---|---|---|---|
| 113 | 最核心业务指标 | ✅ | `supported_resolution_rate` / `wrong_resolution_rate` / `abstention_rate` / `handoff_rate` / `citation_coverage` | 定义严谨（用重开判定"假解决"是亮点） |
| 114 | **自动解决率怎么定义** | ⚠️ | 有 `supported_resolution_rate`，但分母含人工参与的 Case | **无"AI 独立完成且未转人工"口径**；需明确定义 |
| 115 | 转人工率越低越好吗 | ⚠️ | 有指标无分析 | 需论证（低转人工率可能意味着该弃权的没弃权） |
| 116 | 满意度/解决率/成本冲突 | ❌ | 无 CSAT 采集，无成本-质量权衡策略 | 完全缺失 |
| 117 | Bad Case 收集分类归因 | ⚠️ | `knowledge/gap_service.py`（缺口队列 → 草稿 → 评审 → 发布） | 有闭环，**归因维度只有"知识缺口"一种** |
| 118 | **判断 BadCase 来自知识/Retrieval/Prompt/模型/Tool** | ❌ | 无自动归因 | 完全缺失——与 #109 是同一个缺口 |
| 119 | 修复后怎么证明解决 | ⚠️ | 重跑 eval + 门禁 | 无"该 case 已回归通过"的单点追踪 |
| 120 | Data Flywheel | ⚠️ | gap → draft → publish 有；线上→eval→回归**未自动串联** | 需把 eval 归因与缺口队列打通 |

### 第十一组：成本、性能与稳定性（121–126）

| # | 问题 | 状态 | 依据 | 差距与处理动作 |
|---|---|---|---|---|
| 121 | 效果/成本/响应速度冲突取舍 | ❌ | 无显式策略 | 需补策略文档 + 可配置档位 |
| 122 | **高并发怎么设计** | ⚠️ | Postgres 队列 + `SKIP LOCKED` + 令牌桶限流（`rate_limit.py`）+ 异步 worker | 有基本能力；**无并发上限/背压/优先级队列**（`priority-queue admission` 是已知的"无消费方"缺陷） |
| 123 | 降低 Token Cost | ⚠️ | `MAX_EXCERPT_CHARS` / `MAX_TOTAL_EVIDENCE_CHARS` 截断 + token 计量 + 计费账本 | 无缓存、无去重、无小模型分流 |
| 124 | **首字延迟 / 总响应** | ⚠️ | 总延迟有 P50/P95；**无流式**（`generator` 注释已明确说明并记录原因） | 首字延迟无法优化，需引入流式或明确接受并论述 |
| 125 | 模型超时宕机降级熔断 | ✅ | `resilience.CircuitBreaker` + `retry_delays` + rerank deadline + 模型/检索不可用均转弃权 | 可上线级；唯一小瑕疵是**退避无抖动**（高并发下会同步重试） |
| 126 | 需要监控哪些可观测性指标 | ✅ | 18 个 Prometheus 指标 + OpenTelemetry trace + append-only 审计 + 结构化日志 | 可上线级 |

### 第十二组：电商客服专项（127–129）

| # | 问题 | 状态 | 依据 | 差距与处理动作 |
|---|---|---|---|---|
| 127 | 商品咨询/订单查询/退款售后/投诉安抚不能同方案 | ❌→⚠️ | 审计前无场景区分；本次 `Scene` + `IntentKind` 二维给出拆分依据 | 已补分类，**未接线**；且无订单/商品类工具可落地 |
| 128 | 订单信息不该直接放进 RAG | ⚠️ | 因无订单集成，问题不存在；但也**无文档立场** | 建议补 ADR 明确"实时业务数据走 Tool 不走 RAG" |
| 129 | 退款赔付五段式流程 | ✅ | `docs/agent.md`「propose → authorize → preview → confirm → execute → verify → audit」+ `tool_gateway` 完整实现 | 流程与代码一致；**但无 `billing.refund` 实例**（eval 里写了 `allowed_tools=("billing.refund",)` 却未注册） |

### 第十三组：制造业 / 工业智能客服专项（130–132）

| # | 问题 | 状态 | 依据 | 差距与处理动作 |
|---|---|---|---|---|
| 130 | **故障码：查知识库 / 读实时设备数据 / 建工单** | ❌ | 建工单有（jira/linear）；**读实时设备数据无任何工具**；无故障码识别 | 完全缺失 |
| 131 | **型号/硬件版本/固件版本知识管理** | ❌ | `chunks.metadata` 是 JSONB 但**检索期不参与过滤**（见 #35）；无"必须澄清型号才能回答"的机制 | 完全缺失。这条与 #35 是同一根因 |
| 132 | **MTTR / 一次修复率 / 停机时间** | ❌ | `cases` 有 SLA 时钟（首响/解决），但**无 MTTR、一次修复率、停机时长** | 完全缺失 |

---

## 3. 十个技能维度的深度定级

| 维度 | 定级 | 判断要点 |
|---|---|---|
| **多轮对话与状态管理** | **Demo 级**（审计前不存在，本次补到 Demo+） | 无对话轮次存储；Chatwoot 客户端无拉取历史的能力；`inbox_consumer` 只传单条问题。本次补的 `ConversationMemory` 是内存值对象，**无持久化** |
| **意图识别与拒答边界** | **Demo→接近上线**（本次补） | 拒答边界本就扎实（6 种原因码 + 真发通知）；意图识别本次从二分类升到 7 路由类 × 8 场景，且已在 23 条评测集上验证零误伤 |
| **RAG 检索与召回优化** | **Demo 级** | 主干链路完整且正确，但**全部参数硬编码**（chunk 1200、top_k 8、候选 40/40、RRF k=60），无 overlap、无 query rewrite、无 metadata 过滤、无召回指标 |
| **工具调用与流程编排** | **编排可上线，工具生态 Demo** | 网关本身可上线（5 级 risk、确认绑定、幂等、后置校验、第三方故障分类）；但只注册 4 个工具，且**无工具选择逻辑**——清单 #64/#65 无法演示 |
| **会话记忆与上下文压缩** | **完全缺失→Demo** | 本次补了预算化压缩 + 钉住义务项 + 长期事实提取，但**无持久化、未接线** |
| **幻觉抑制与引用溯源** | **接近上线** | 溯源可上线（chunk/版本/摘录哈希/source_uri）；抑制的最后一环——**引用"支持性"校验**——刻意只做 metric 未做 guard，且精度数据未产出 |
| **超时重试与并发控制** | **接近上线** | 断路器/退避/deadline/降级齐全；缺退避抖动与并发上限 |
| **兜底策略与转人工** | **可上线级** | 弃权必发通知、发送前租约复检、转人工释放租约，是真做完的一块 |
| **日志埋点与可观测性** | **可上线级** | 18 指标 + trace + 审计 + 结构化日志；指标无租户标签（刻意设计，避免泄露） |
| **效果评测与回归用例** | **Demo 级** | 23 用例 + 6 门禁 + 真实链路产出报告，骨架好；但**无归因、无 retrieval 指标、无 LLM-judge、无人工评测** |

---

## 4. 缺口优先级（建议后续实现顺序）

**P0 — 清单高频考点且当前无法演示（必须补）**

1. **多轮对话接线**：给 `ChatwootClient` 增加"拉取会话历史"方法 → `inbox_consumer` 构造 `Turn` 列表 → 传入 `orchestrator.run(history=...)`。**否则本次补的 conversation 模块是死的。**
2. **检索归因评测**：`EvalCase` 增加期望来源键 → `runner` 计算 recall / MRR → 区分 `RETRIEVAL_MISS` / `GENERATION_ERROR` / `ABSTENTION_ERROR`。一次性命中清单 #38/#101/#109/#118/#119。
3. **持久化会话记忆**：新增对话轮次表 + 迁移（注意 `EXPECTED_MIGRATIONS` 门禁）。
4. **Chunk overlap + 切片参数可配置**：命中 #26/#32/#42。

**P1 — 深度补齐**

5. Metadata 过滤接线（`chunks.metadata` JSONB）→ 同时解 #35/#131（型号/版本维度检索）。
6. 大小模型路由 + 降级链 → 命中 #78/#86/#121/#123。
7. 退避抖动 + 并发上限 → 命中 #122/#125。
8. 业务读工具（订单/物流/账单查询）+ 工具选择逻辑 → 命中 #64/#65/#76/#127/#129/#130。
9. 多引用落库（在 `retrieval_config` 记录全部支持 chunk）→ 命中 #36。

**P2 — 文档与论述（口述题，成本极低）**

10. 补 ADR：RAG vs FT、为何确定性编排而非自主 Agent、单/多 Agent 取舍、MCP 取舍、实时数据不走 RAG。
11. 补"上线验收清单"与 incident runbook → 命中 #90/#112。
12. 补成本-延迟-质量取舍策略 → 命中 #121。

---

## 5. 本次已改动的文件（如实披露，均需复核）

> 这些改动发生在切换到"仅调研"之前，**未完成验证**。

| 文件 | 改动 | 状态 |
|---|---|---|
| `apps/api/src/platform_core/agent_runtime/conversation.py` | **新增**。多轮状态、预算化压缩、钉住义务项、长期事实提取、话题切换、指代消解式 query rewrite、澄清判定 | 单测 36 条**全通过** |
| `apps/api/src/platform_core/agent_runtime/intent.py` | **新增**。Scene(8) × Kind(7) → 7 路由类，多意图、置信度、安全类别不可逃逸 | **4 条断言失败，待修** |
| `apps/api/src/platform_core/agent_runtime/orchestrator.py` | 接入意图分类与多轮上下文；新增 `_finish_clarify`；`top_k` 可配置；`model_config` 记录意图与上下文快照 | 单测通过，集成测试未跑 |
| `apps/api/src/platform_core/agent_runtime/qa_path.py` | 新增 4 个弃权原因码与对应文案；`AnswerGenerator` 协议扩展可选参数 | 单测通过 |
| `apps/api/src/platform_core/agent_runtime/prompts.py` | 模板升至 **v3**，新增对话块与规则 7（对话内容亦为不可信数据） | 单测通过 |
| `apps/api/src/platform_core/agent_runtime/generator.py` | 支持渲染对话上下文（红acted、有预算） | 单测通过 |
| `apps/api/tests/unit/agent_runtime/test_conversation.py` | **新增** 36 条 | 通过 |
| `apps/api/tests/unit/agent_runtime/test_intent.py` | **新增** | **4 条失败** |
| 2 个集成测试 double | `generate()` 签名加 `**kwargs` 兼容 | 未跑集成 |

**当前验证状态**：`ruff check` 全绿；`mypy` strict 131 文件 0 错误；
单元测试 719 条通过（含新增）；**新增的 4 条 intent 断言失败**。

**已知遗留**：intent 测试的 4 条失败均为我的断言与实现不符（多意图主次顺序、场景
优先级、强弱写信号的置信度区分），不是实现错误，但必须修正后才能算完成。

---

## 6. 调研过程中发现的、清单之外的问题

这些不在清单里，但影响"能不能讲"和"讲出来是不是真的"：

1. **`AgentOrchestrator.run` 的生产调用方只有 `inbox_consumer`**（`worker/`），
   `agent_runtime/router.py` 的 `POST /v1/conversations/{id}/agent-runs` 是管理端入口。
   两条路径都**不传对话历史**——这是多轮能力缺位的直接证据。
2. **`billing.refund` 在评测集里声明了却从未注册**（`dataset.py` 的
   `allowed_tools=("billing.refund",)`）。声明而不存在的能力，是另一种"无消费方"。
3. **`retry_delays` 无抖动**：高并发下所有调用方会以相同节奏重试，形成同步峰值。
4. **`chunks.metadata` 与 `documents.metadata` 是 JSONB 且检索期完全不用**——
   字段已存在、迁移已做、无消费方。这是本仓库反复出现的缺陷形状的第 16 例。
5. **`uq_citation_claim` 使一条声明只能落一条引用**，与 `docs/agent.md` 的
   "one or more citations" 直接冲突。
