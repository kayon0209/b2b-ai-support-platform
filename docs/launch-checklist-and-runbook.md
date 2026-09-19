# 上线验收清单与 Incident Runbook（迭代计划 4.7）

日期：2026-09-19
对应门禁：`platform_core.evaluation.gates` + `scripts/release_check.py`。
**清单里的每一条阈值都必须有一个对应的门禁或指标**——写在这里却没有门禁的项，
视为未实现，不许在验收表上打勾。

---

## 1. 上线验收清单

### 1.1 安全（全部零容忍，一票否决）

| 项 | 阈值 | 门禁/证据 |
|---|---|---|
| 跨租户泄漏 | = 0 | `zero_cross_tenant` 门禁 + `test_cross_tenant_leak_surfaces` |
| 越权写 | = 0 | `zero_unauthorized_write` 门禁 |
| 重复回复客户 | = 0 | `zero_duplicate_reply` 门禁 + `test_self_reply_guard` |
| RLS FORCE | 全部租户表 | `test_tenant_owned_tables_force_row_level_security` |
| append-only 表不可改 | audit/billing | `test_schema_privileges` |
| bootstrap token | 生产环境禁用 | `config._assert_auth_is_configured` 启动即拒 |

### 1.2 质量（固定语料，`scripts/run_eval.py`）

| 项 | 阈值 | 门禁 |
|---|---|---|
| 引用覆盖率 | ≥ 95% | `max_citation_violation_rate = 0.05` |
| 弃权正确率 | ≥ 90% | `min_abstention_correct_rate = 0.90` |
| 禁止声明率 | ≤ 2% | `max_forbidden_claim_rate = 0.02` |
| 检索召回 recall@k | ≥ 0.90 | `min_retrieval_recall_at_k`（新增） |
| 归因 | 每用例带归因标签 | 报告 `attribution_counts` |
| judge 与人工一致性 | kappa ≥ 0.6 才启用 | `judge.cohens_kappa` + 人工标注子集 |

### 1.3 成本与延迟

| 项 | 阈值 | 测量 |
|---|---|---|
| 单会话成本 p50 / p95 | p50 ≤ 1 美分、p95 ≤ 5 美分 | `platform_run_cost_cents`（估算：token × 配置单价） |
| 每 run LLM 调用数 | ≤ 10（`APP_RUN_MAX_LLM_CALLS`） | 超限转人工并记事件 |
| 总延迟 p95 | < 8 s，backstop 20 s | `platform_agent_run_latency_seconds`（按 outcome 分桶） |
| 队列背压 | 深度 ≥ `APP_QUEUE_MAX_DEPTH` → 429 | agent-runtime 入队口 |
| 重试抖动 | `APP_RETRY_JITTER_RATIO = 0.3` | `resilience.retry_delays` |

### 1.4 回滚

| 项 | 机制 |
|---|---|
| Prompt 回滚 | prompt-release API（回滚到历史版本，立即生效） |
| 新行为 | 全部 flag 关闭默认（guard/读工具/归一化/过滤/优先认领），租户级可单独关 |
| 模型故障 | 降级链 primary → fallback（`APP_MODEL_FALLBACK_*`）→ 弃权转人工 |
| 语料事故 | kill switch：租户级停用 agent（租约释放 + 停止入队） |

---

## 2. Incident Runbook — 错误回答激增

触发信号（按优先级）：
- `platform_agent_abstentions_total` 下降 + `wrong_resolution` 案例上升；
- `platform_agent_citation_validation_total{status="unsupported"}` 突增；
- 客诉通道出现"机器人答错了"关键词。

### 第一步：止血（< 5 分钟）

1. **关新行为**：检查最近翻转的 feature flag（`/v1/flags`），全部回退到 0。
   所有行为变更默认关闭就是为这一步服务的。
2. **回滚 prompt**：`POST /v1/prompts/rollback` 到上一个 serving 版本。
   "Every customer-visible answer changes from this moment"——这一步立即生效。
3. 若仍错误：**租户级停用**（kill switch），并把该租户未处理事件留在队列。

### 第二步：定位（< 30 分钟）

1. 用错误会话的 `trace_id` 从 audit 取该 run 的 `retrieval_config` +
   `model_config`（两条快照都为复现而设计）；
2. 看 `evidence_source`：是 `tool` 还是文档检索？归因标签
   （RETRIEVAL_MISS / GENERATION_ERROR / ABSTENTION_ERROR）直接给出管线在哪一环；
3. `RETRIEVAL_MISS`：检查语料最近变更（清洗/重切的 `pipeline_version` 是否
   变过、chunk metadata 是否漂移）；
4. `GENERATION_ERROR`：对照 `prompt_version_id`，确认是否新 prompt 版本引入。

### 第三步：恢复与复盘

1. 修复后先跑 `run_eval.py` 与门禁，全绿再重新放量（flag 从 0 起步）；
2. 复盘必须回答：门禁为什么没拦住？（新失败模式→加用例；旧用例失效→查语料）；
3. 桌面演练记录追加到本文件的"演练记录"一节。

### 演练记录

| 日期 | 场景 | 结果 |
|---|---|---|
| 2026-09-18 | 备份恢复（backup_restore_drill.py） | 通过（见交付报告） |
| （待办） | 模型宕机 → 降级链 → 弃权转人工 | 未执行：需要 fallback 模型配置 |

> 如实标注：模型宕机演练需要配置一个真实备用模型端点，当前未执行。
