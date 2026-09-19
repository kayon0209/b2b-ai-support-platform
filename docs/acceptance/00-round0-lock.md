# Round 0 lock — 2026-09-19 04:06:41
HEAD: d44420eab511af5a36364c9d74b32d909ef33990

## git status --short
 M .workbuddy-ai/memory/2026-09-18.md
 M .workbuddy-ai/memory/MEMORY.md
 M apps/admin-web/src/components/Layout.tsx
 M apps/admin-web/src/components/ui.tsx
 M apps/admin-web/src/lib/api.ts
 M apps/admin-web/src/pages/Branding.tsx
 M apps/admin-web/src/pages/Cases.tsx
 M apps/admin-web/src/pages/FeatureFlags.tsx
 M apps/admin-web/src/pages/GapQueue.tsx
 M apps/admin-web/src/pages/Members.tsx
 M apps/admin-web/src/pages/PromptRelease.tsx
 M apps/admin-web/src/pages/Usage.tsx
 M apps/admin-web/src/styles.css
 M apps/api/src/platform_core/agent_runtime/generator.py
 M apps/api/src/platform_core/agent_runtime/models.py
 M apps/api/src/platform_core/agent_runtime/orchestrator.py
 M apps/api/src/platform_core/agent_runtime/prompts.py
 M apps/api/src/platform_core/agent_runtime/qa_path.py
 M apps/api/src/platform_core/agent_runtime/router.py
 M apps/api/src/platform_core/audit/router.py
 M apps/api/src/platform_core/config.py
 M apps/api/src/platform_core/db.py
 M apps/api/src/platform_core/evaluation/gates.py
 M apps/api/src/platform_core/evaluation/pii.py
 M apps/api/src/platform_core/evaluation/runner.py
 M apps/api/src/platform_core/integrations/resilience.py
 M apps/api/src/platform_core/integrations/router.py
 M apps/api/src/platform_core/integrations/sdk.py
 M apps/api/src/platform_core/integrations/webhook_router.py
 M apps/api/src/platform_core/knowledge/ingest.py
 M apps/api/src/platform_core/knowledge/models.py
 M apps/api/src/platform_core/knowledge/router.py
 M apps/api/src/platform_core/retrieval/hybrid.py
 M apps/api/src/platform_core/support_bridge/chatwoot_client.py
 M apps/api/src/platform_core/support_bridge/minimize.py
 M apps/api/src/platform_core/support_bridge/router.py
 M apps/api/src/platform_core/tool_gateway/gateway.py
 M apps/api/src/platform_core/tool_gateway/registry.py
 M apps/api/src/platform_core/tool_gateway/router.py
 M apps/api/tests/integration/test_e2e_acceptance.py
 M apps/api/tests/integration/test_migration_and_performance.py
 M apps/api/tests/integration/test_orchestrator_lease_race.py
 M apps/api/tests/integration/test_retention_sweep.py
 M apps/api/tests/unit/knowledge/test_ingest.py
 M apps/api/tests/unit/test_metrics.py
 M apps/worker/src/worker/inbox_consumer.py
 M apps/worker/src/worker/ingestion_consumer.py
 M apps/worker/src/worker/wiring.py
 M packages/observability/src/observability_metrics.py
 M scripts/run_eval.py
 M tests/evals/dataset.py
 M tests/evals/harness.py
 M tests/evals/test_dataset.py
 M tests/evals/test_release_gates.py
?? apps/admin-web/src/components/TokenDialog.tsx
?? apps/api/migrations/versions/0032_chatwoot_tenant_resolver.py
?? apps/api/migrations/versions/0033_retrieval_multipath.py
?? apps/api/migrations/versions/0034_conversation_turns.py
?? apps/api/migrations/versions/0035_contact_facts.py
?? apps/api/migrations/versions/0036_tool_citations.py
?? apps/api/src/platform_core/agent_runtime/conversation.py
?? apps/api/src/platform_core/agent_runtime/conversation_store.py
?? apps/api/src/platform_core/agent_runtime/intent.py
?? apps/api/src/platform_core/evaluation/judge.py
?? apps/api/src/platform_core/integrations/business_read.py
?? apps/api/src/platform_core/tool_gateway/case_read.py
?? apps/api/src/platform_core/tool_gateway/selector.py
?? apps/api/tests/integration/test_case_read_tool.py
?? apps/api/tests/integration/test_multipath_retrieval_and_memory.py
?? apps/api/tests/unit/agent_runtime/test_conversation.py
?? apps/api/tests/unit/agent_runtime/test_intent.py
?? apps/api/tests/unit/evaluation/test_judge.py
?? apps/api/tests/unit/knowledge/test_chunking_and_memory.py
?? apps/api/tests/unit/tool_gateway/test_read_tools.py
?? docs/acceptance/
?? docs/adr/0006-realtime-data-via-tools.md
?? docs/adr/0007-no-multi-agent-scene-dispatch.md
?? docs/interview-checklist-audit.md
?? docs/iteration-delivery-report.md
?? docs/iteration-plan-rag-dialogue.md
?? docs/launch-checklist-and-runbook.md
?? scripts/audit_unconsumed.py
?? scripts/tune_chunking.py

## git diff --stat
 apps/admin-web/src/pages/Branding.tsx              |   63 +-
 apps/admin-web/src/pages/Cases.tsx                 |    2 +
 apps/admin-web/src/pages/FeatureFlags.tsx          |    2 +
 apps/admin-web/src/pages/GapQueue.tsx              |   22 +-
 apps/admin-web/src/pages/Members.tsx               |   13 +
 apps/admin-web/src/pages/PromptRelease.tsx         |   40 +-
 apps/admin-web/src/pages/Usage.tsx                 |   10 +
 apps/admin-web/src/styles.css                      |    3 +
 .../src/platform_core/agent_runtime/generator.py   |   90 +-
 apps/api/src/platform_core/agent_runtime/models.py |   57 +-
 .../platform_core/agent_runtime/orchestrator.py    | 1049 ++++++++++++++++++--
 .../api/src/platform_core/agent_runtime/prompts.py |   33 +-
 .../api/src/platform_core/agent_runtime/qa_path.py |  111 ++-
 apps/api/src/platform_core/agent_runtime/router.py |   29 +
 apps/api/src/platform_core/audit/router.py         |    2 -
 apps/api/src/platform_core/config.py               |   99 ++
 apps/api/src/platform_core/db.py                   |   57 +-
 apps/api/src/platform_core/evaluation/gates.py     |   19 +
 apps/api/src/platform_core/evaluation/pii.py       |   16 +
 apps/api/src/platform_core/evaluation/runner.py    |   66 +-
 .../src/platform_core/integrations/resilience.py   |   31 +-
 apps/api/src/platform_core/integrations/router.py  |   67 ++
 apps/api/src/platform_core/integrations/sdk.py     |  107 +-
 .../platform_core/integrations/webhook_router.py   |    6 +-
 apps/api/src/platform_core/knowledge/ingest.py     |  455 ++++++++-
 apps/api/src/platform_core/knowledge/models.py     |   21 +
 apps/api/src/platform_core/knowledge/router.py     |  109 ++
 apps/api/src/platform_core/retrieval/hybrid.py     |  333 +++++--
 .../support_bridge/chatwoot_client.py              |   46 +
 .../src/platform_core/support_bridge/minimize.py   |    6 +
 .../api/src/platform_core/support_bridge/router.py |   21 +-
 apps/api/src/platform_core/tool_gateway/gateway.py |   39 +-
 .../api/src/platform_core/tool_gateway/registry.py |  134 +++
 apps/api/src/platform_core/tool_gateway/router.py  |    5 +-
 apps/api/tests/integration/test_e2e_acceptance.py  |    2 +-
 .../integration/test_migration_and_performance.py  |    2 +-
 .../integration/test_orchestrator_lease_race.py    |    9 +-
 apps/api/tests/integration/test_retention_sweep.py |    1 +
 apps/api/tests/unit/knowledge/test_ingest.py       |   55 +
 apps/api/tests/unit/test_metrics.py                |   31 +
 apps/worker/src/worker/inbox_consumer.py           |  246 ++++-
 apps/worker/src/worker/ingestion_consumer.py       |   70 +-
 apps/worker/src/worker/wiring.py                   |    6 +-
 .../observability/src/observability_metrics.py     |   55 +-
 scripts/run_eval.py                                |   23 +-
 tests/evals/dataset.py                             |   31 +-
 tests/evals/harness.py                             |   13 +
 tests/evals/test_dataset.py                        |    4 +-
 tests/evals/test_release_gates.py                  |    4 +-
 54 files changed, 3759 insertions(+), 449 deletions(-)

## Round 0 结论（补充，2026-09-19 04:1x）

### 服务状态
- 基础设施容器（chatwoot-web/sidekiq、双 postgres、双 redis、minio、keycloak）：复用已在运行的 compose 栈
- API: `python -m platform_core.main` @ 127.0.0.1:8000（Windows 必须 selector loop；裸 uvicorn 会 ProactorEventLoop 崩溃）
- worker: interactive（含 outbox relay）+ ingestion，各一进程
- admin-web: `npm run dev` @ localhost:5173，/api 代理 → 8000
- 数据库: alembic current = 0036_tool_citations (head)，与 EXPECTED_MIGRATIONS=36 一致

### 构建新鲜度探针
- API 端：OpenAPI 含本次迭代新增端点 `/v1/dead-letters/{item_id}/retry|resolve`、`/v1/knowledge/aliases` → **确认新代码在跑**
- 前端 dist: `assets/index-D7XS2NNY.js`（02:34 构建，与当前源码同代）；验收走 dev server（直接服务当前源码）

### 鉴权
- 种子令牌（tenant_owner@admin-demo）：`pt_admin-demo_f4b78ee8-5e89-5648-a309-3c5117838c60`
- 验证：未认证 GET /v1/tenant/usage → 401；带令牌 → 200（usage + quality/metrics）

### 自检交接件
- 迭代会话**未产出 handoff.md**。其自检证据以 `docs/iteration-delivery-report.md` 代替（声明：pytest 1372 passed / eval 23/23 / 迁移 0033–0036 循环通过 / ruff+mypy 全绿 / tsc+build 通过）。
- 未验证清单与不知道清单由本验收流程后续各 Round 实测补齐。

### 环境备注
- .env 齐备（bootstrap tokens 开启、LLM key 已配、Chatwoot token 已配）；密钥值不记录于本报告。
