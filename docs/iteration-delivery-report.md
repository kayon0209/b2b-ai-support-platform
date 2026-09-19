# 迭代交付报告 — RAG 与对话系统能力补齐

日期：2026-09-19
依据：`docs/iteration-plan-rag-dialogue.md`（P0 全部 + P1 全部 + P2 主体）
验证基线：`tests/artifacts/eval_report.json`（23/23 PASS，recall@k = 1.0 / measured 7）

---

## 交付清单（按 Phase）

### Phase 0 — 收口与接线 ✅

| 项 | 结果 |
|---|---|
| 0.1 intent 测试红灯 | 修复实现（非弱化断言）：场景加权打分（PRE_SALES 疑问框架 2.0、安全强信号词干 2.0）；`can I <写动词><宾语>` 请求式框架写入信号；置信度分层 0.9/0.75/0.7；程序性问题折扣限定 "how" 开头 |
| 0.2 conversation 接线 | `inbox_consumer.load_history` 接缝 + `orchestrator.run(history=...)` 全链打通 |
| 0.3 上下文指标 | `context_turns_kept/summarized/pinned` 直方图 + `context_pins_evicted` 计数器；挤掉钉子 WARN |
| 0.4 基线固化 | `run_eval.py` 实测 23/23，报告含归因聚合 |

### Phase 1 — 检索链路与数据处理 ✅

- **1.1** `ChunkingConfig`（max/min/overlap，`overlap>=max` 报错）；段落级 overlap；配置随 `PIPELINE_VERSION=v2` 写入版本 metadata，改参数即自动重切。
- **1.2** `scripts/tune_chunking.py` 网格实验（recall@k / MRR / chunk 数 / 摄取耗时），报告落 `tests/artifacts/chunking_tuning.json`。
- **1.3** 第三路 **pg_trgm**（GIN `gin_trgm_ops` 索引）+ 第四路 **knowledge_aliases**（租户表 + 权重 + 管理 API）；每路独立开关可测贡献。
- **1.4** `normalize_colloquial`（与别名单一数据源，整词边界含连字符防误伤 EC-504）；`rewrite_query` **实体保护**（型号/故障码逐字符保留）。
- **1.5** `MetadataFilter`（allowlist 键 + JSONB `@>` 双表谓词 chunk+version，GIN 索引）；orchestrator 从 "model X"/"firmware Y" 实体构建过滤，**空结果自动降级宽召回并记录**。
- **1.6** DOCX 表格 → markdown 原子 chunk（`content_kind=table`，不切分不重叠）；图片仅评估结论（见 runbook 附录）。
- **1.7** Scene 档位 top-k（政策 12 / 技术 6 / 默认 8）；**相对**阈值裁剪（floor_ratio × top_score，flag 默认关）；rerank 候选上限。
- **1.8** typo/口语/指代由 1.3+1.4+Phase 2 承担；**超文档增量：CJK 大切分**（`_content_terms` 中文二元语法，生产 tokenizer 与 trigram 路同粒度）。

### Phase 2 — 记忆与上下文 ✅

- **2.1** `conversation_turns`（迁移 0034，FORCE RLS，存 `pii.redact_text` 脱敏文本 + 原文哈希；RetentionPolicy 登记 90 天）。
- **2.2** `ChatwootClient.list_messages`（复用断路器；失败返回空数组退单轮，trace 标记）。
- **2.3** `load_history` 合并策略：本地窗口优先，Chatwoot 补更早窗口；`ConversationMemory.add` 修剪（挤掉钉子 → 指标+WARN）。
- **2.4/2.7** 预算参数进 config；**证据优先**：证据占满时上下文缩至下限并记 `context_budget_reduced_for_evidence`。
- **2.5** `contact_facts`（迁移 0035，UNIQUE(tenant,contact,key) + ON CONFLICT DO UPDATE = 最新陈述为准）；仅 CUSTOMER 轮提取（防模型自反馈）；跨会话 facts 注入 `CompactedContext.durable_facts`。
- **2.6** 摘要保持确定性可 diff + 钉住项独立通道（不进摘要）。
- **2.8** 追问上限：连续 `clarification_max_streak` 次澄清后转人工（`CLARIFICATION_LIMIT`），澄清不计入转人工率。

### Phase 3 — Agent 与工具 ✅

- **3.1** `tool_gateway/selector.py`：确定性选择（scene 亲权 × 显式名词 × 路由门控 `BUSINESS_READ` only）；无候选 → `TOOL_NO_CANDIDATE` 转人工；授权仍在 policy。
- **3.2** 读工具 ×4：`order.get_status` / `shipment.track` / `billing.get_invoice`（`business_api` 通用连接器适配器）+ `case.read`（内部执行器，RLS 会话内读，跨租户负向测试）；`ensure_tool_definitions` 幂等种子 8 个工具定义。
- **3.3** ADR 0006：实时数据走工具、RAG 只承载稳定知识。
- **3.4** 工具回执 = 第二类证据（伪 chunk，`source_uri=tool://<tool>/<ref>`，`excerpt_hash`=回执哈希）；迁移 **0036** `citations.document_version_id` 可空；引用来源审计可区分；工具失败/UNKNOWN → 弃权转人工（`TOOL_EXECUTION_FAILED/UNVERIFIED`）。
- **3.5** ADR 0007：不做通用 multi-agent；Scene 子流程分派 + 重新评估触发条件。
- **3.6** 死信 `POST /{id}/retry`：重置 pending + 回卷同步游标（诚实语义：不伪造原调用重放），审计留痕。

### Phase 4 — 质量、可信与评测 ✅（4.4 复核页复用缺口工作流）

- **4.1** `EvalCase.expected_version_keys` + `CaseResult.attribution`（PASS / RETRIEVAL_MISS / GENERATION_ERROR / ABSTENTION_ERROR）+ recall@k / MRR；**新门禁 `min_retrieval_recall_at_k`**；归因结果聚合进报告。
- **4.2** 引用支持性 guard：`claim_contradiction_candidates` 从纯指标升级为 **flag 门控 guard（默认关）**，开启后矛盾声明 → `UNSUPPORTED_CLAIM` 弃权。
- **4.3** `evaluation/judge.py`：三维 rubric（correctness/relevance/faithfulness）+ `cohens_kappa`（≥0.6 才可靠）；judge 不可用时降级而非失败；**只作 metric 不作门禁**。
- **4.4** 人工标注/一致性统计：judge kappa 函数 + 缺口草稿 review/publish 工作流即人工评审面（含审计）；独立"评测复核"页待 judge 上线后按需补。
- **4.5** 权威度进检索排序（`retrieval_authority_boost`，flag 默认关）；冲突弃权自动入缺口队列（`record_gap`）供裁定；`Document.owner_ref` 已有落点。
- **4.6** 清洗（NFC 规范化、页码噪声、块级去重，逐项开关+前后报告）+ front-matter 标签（与 1.5 同键）已并入摄取状态机。
- **4.7** `docs/launch-checklist-and-runbook.md`（每条阈值对应门禁；演练记录如实标注模型宕机演练未执行）。

### Phase 5 — 工程治理 ✅

- **5.1** 成本指标 `platform_run_cost_cents`（token × 配置单价，histogram 看 p95）。
- **5.2** 并行化评估：别名扩展改 Python 单查询（省一次往返）；流式需改 provider 面，**维持单独评估**（如实标注）。
- **5.3** 退避抖动 `jitter_ratio`（默认 0.3，可注入 rng）；队列背压（深度 ≥ `APP_QUEUE_MAX_DEPTH` → 429 `QUEUE_SATURATED`）；**优先级认领接线**（SLA 升级过的会话先认领，部署级开关）。
- **5.4** 转人工携带证据：私有便签（reason_code + question_hash + run_id，`APP_HANDOFF_EVIDENCE_ENABLED`）。
- **5.5** 模型降级链：primary → fallback（`APP_MODEL_FALLBACK_*`）→ 弃权，`model_fallback_total` 可观测。

---

## 验证

| 检查 | 结果 |
|---|---|
| pytest 全量（apps + evals + packages + tests） | **1372 passed, 0 failed** |
| 基线评测（真实管线 + 活模型） | **23/23 PASS**，citation_violations=0，abstention_false=0，forbidden=0 |
| 归因 | PASS×23，recall@k=1.0（measured 7） |
| ruff + ruff format --check | 全绿 |
| mypy --strict（136 文件） | 全绿 |
| 迁移 | 0033–0036 `downgrade/upgrade` 循环通过；`EXPECTED_MIGRATIONS=36` |
| 前端 tsc + vite build | 通过 |

## 如实标注的未竟项

1. **数据集规模**：23 用例（计划目标 ≥100）。已具备 expected_keys/归因/门禁机制，扩容是纯内容工作。
2. **模型宕机演练**：需要配置真实备用模型端点后执行一次并记录。
3. **流式输出**：需改 provider 表面，维持独立评估（计划 7 节已列此风险）。
4. **PDF 表格**：pypdf 无法可靠抽表格（加 pdfplumber 违反"不引新依赖"约束），DOCX 已完整支持；结论已并入 1.6 图片评估记录。
5. **judge 人工标注子集**：kappa 函数与可靠性判定已就绪，需要人工标注数据才能出一致性报告。
