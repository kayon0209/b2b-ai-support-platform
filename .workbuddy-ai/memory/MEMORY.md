# Project Memory — B2B AI Customer Support Platform

Durable rules only. Daily logs (`YYYY-MM-DD.md`) hold narrative + rationale.
Ordered most-critical first — the tail is what gets lost on injection.
Consolidated 2026-09-19 (v2, trimmed).

## Environment

- **Dir**: `D:/360Downloads/360驱动大师目录/b2b-ai-support-plan/b2b-ai-support-plan`
- **Python**: `.venv/Scripts/python.exe` (3.12). Managed 3.13 has **no pytest**.
- **PYTHONPATH: absolute, `;`-joined** — else `No module named 'platform_core'`:
  `$R/apps/api/src;$R/packages/contracts/src;$R/packages/policy/src;$R/packages/observability/src;$R/apps/worker/src`
- **Ports**: ai-postgres 5435, chatwoot-postgres 5434, ai-redis 6380,
  chatwoot-redis 6381, Chatwoot 3000, API 8000, Keycloak 8081, MinIO 9000/9001;
  e2e `b2b-e2e-minio` 19000. Vite 5173, proxies `/api`→8000.
- **DB roles**: `platform` (superuser, bypasses RLS — seeding/cleanup),
  `platform_app` (NOBYPASSRLS — isolation assertions).
- `packages/observability/src/observability.py` is top-level `observability`,
  **not** `platform_core.observability`.

## Verification

```bash
./.venv/Scripts/python.exe -m pytest    # pyproject supplies pythonpath
./.venv/Scripts/python.exe -m ruff check apps packages scripts tests pytest_plugins_release
./.venv/Scripts/python.exe -m ruff format --check apps packages scripts tests pytest_plugins_release
./.venv/Scripts/python.exe -m mypy      # strict, 129 files, 0 errors
./.venv/Scripts/python.exe -m platform_core.evaluation.release_check --evidence-only
./.venv/Scripts/python.exe tests/e2e/e2e_ingestion_minio.py
```

pytest's summary goes to **stderr**; `EXIT=$?` after a pipe reports the last
command — use `${PIPESTATUS[0]}` or no pipe. CI's `release-evidence` job runs the
**full** suite then `release_check --evidence-only`.

**Stop the `ai-*` services before running the suite.** `ai-worker-interactive`
relays the outbox every second, so a test that commits an outbox row then
asserts on its own relay sees `claimed=0`.

**The suite flakes order-dependently here — a red run is not evidence by
itself.** Six consecutive full runs on identical code produced 3 / 0 / 2 / 0 /
10 / 2 failures, each time in different files, and every one passed in
isolation. Two known amplifiers: a failed run leaves fixture rows behind and
the *next* run's setup cascades `IntegrityError` on
`chunks_document_version_id_fkey`; and pytest's tmp-dir GC can trip the
safe-delete guard (54 items > 50) and truncate the summary. **Before touching
code, re-run the failing test alone.** Use `--junitxml` when the summary gets
cut off.

**A truncated grep manufactures false conclusions.** `grep ... | head -40`
once hid an existing registration and produced a report claiming a tool was
missing. Grep by the exact name, separately, before asserting absence.

## Errors must be real 4xx, never 200-with-error-body

FastAPI renders a returned **dict** as HTTP 200. Three routers built
`{"error": ...}` by hand and returned the dict, so every domain refusal
(duplicate flag key, promote blocked by the evaluation gate, rollback with
nothing active) arrived as 200 and the admin UI's `unwrap()` — which only
treats `!res.ok` as failure — reported success. Promote showed a green
"Promoted" for a promotion that never happened.

**Always return `error_response()`/`domain_error_response()` (a
`JSONResponse`), never a bare dict.** `DOMAIN_ERROR_STATUS` in `api.py`
maps codes to statuses; unknown codes fall back to 400, never 200.

Related, now fixed: `main.py` has a `RequestValidationError` handler
(422 → documented envelope, and it does **not** echo `input`, which can be
a token or a prompt) and an `Exception` handler (500 → envelope + trace_id,
details only in the log).

**Symptom to trust:** the UI says "done" but nothing changed. `curl -i` the
endpoint and compare the status with what the UI showed.

## The recurring defect: a capability with no consumer

Every audit round finds the same shape — a value, column or function that
exists and is tested but that no production path reads or writes. Found:
`AgentRun.started_at`, `AgentRun.token_usage`, `sweep_expired_data`,
`credential_ref`, priority-queue admission, `_is_identity_dependent`,
`health_check`, `NEEDS_REAUTH`, `SyncCursor`, `DeadLetterItem`,
`flag_service.evaluate`, the reranker (before 2026-09-18),
`cases.enterprise_account_id`, `release_check`'s CI caller,
`eval_report.json`'s producer, `drain_outbox_once` + `_now`,
`claim_contradiction_candidates` (metric, never a guard).

**For any field a reader depends on, grep its writers/callers.** A test calling
a function directly will not reveal a missing production caller.

## RLS

**Bootstrap pattern (8 instances).** A tenant-owned table read *before* a
binding exists needs a narrow `SECURITY DEFINER` function: pinned
`SET search_path = pg_catalog, public`, `REVOKE ALL FROM PUBLIC` and from
`platform`, `GRANT EXECUTE` to `platform_app` only, `RETURNS TABLE (...)`
(0015, 0016, 0018, 0019, 0026, 0027, 0030 ×2).

Never loosen a policy to `USING (app.tenant_id IS NULL OR ...)` — that grants
table-wide read and `test_cross_tenant_leak_surfaces.py` fails.

**RLS fails silently on writes.** Unbound `SELECT` → zero rows; unbound
`UPDATE`/`DELETE` → `rowcount 0`, **no error**. Assert `rowcount` on any
app-role write. `FOR UPDATE` requires the function be `VOLATILE`.

**Cross-tenant queue scans are global by design** (`claim_pending`,
`claim_ingestion_versions`, `reclaim_stale_ingestions`), so a leftover claimable
row anywhere enters the next suite's batch. **Never leave probe rows.**

## Async and sessions

- **`set_config('app.tenant_id', ..., true)` is transaction-scoped** — it does
  not survive `COMMIT`. **`tenant_session` rebinds on `after_begin`**, so a
  handler that commits mid-request keeps the binding. Every tenant-scoped
  handler must use `tenant_session(ctx)`; 22 sites used to do it by hand.
- **`db.app_role_url()` is the only place the app-role URL is computed.**
- **A failed flush poisons the session.** `await session.rollback()` before
  raising, or the router's `commit()` raises `PendingRollbackError` and a clean
  409 becomes a 500 (`begin_nested()` does **not** help). Prefer `ON CONFLICT
  DO NOTHING` to insert-then-catch: a rollback discards *the whole transaction*.
- **The outbox relay's caller owns the transaction.** `run_once(session)` does
  not commit; pass `commit=True` or a `session_scope()`, or it reports `sent=1`
  while persisting nothing.

## The worker needs the same Windows loop fix as the API

`apps/worker/src/worker/runner.py` used bare `asyncio.run(...)`, which builds
a ProactorEventLoop on Windows — psycopg async refuses it, so the worker
could not start at all (first poll cycle dies with
`RuntimeError: psycopg async cannot run on a ProactorEventLoop`). Fixed by
routing all five call sites through a `run(coro)` helper that passes
`loop_factory=asyncio.SelectorEventLoop` on `win32`.

**Any new `asyncio.run` in a DB-touching path needs the same treatment.**
When a background task "starts" then immediately floods
`worker_cycle_failed` with `RuntimeError`, this is the cause.

Start it with (default queue is `interactive`; override with
`APP_WORKER_QUEUE=outbox|ingestion|retention|sla`):

```bash
PYTHONPATH="<worker/src>;<api/src>;<contracts/src>;<policy/src>;<observability/src>" \
  APP_CHATWOOT_API_TOKEN=<chatwoot user token> \
  ./.venv/Scripts/python.exe -m worker.runner
```

A running worker consumes the global inbox/outbox queues, so it makes
`test_billing_ledger` / `test_inbox_reclaim` / `test_ingestion_worker` flaky
with `claimed=0` **and the failing test differs each run**. Stop it before
running the suite.

## Windows: never launch the API with bare uvicorn

**Always `python -m platform_core.main`.** uvicorn hardcodes
`ProactorEventLoop` on Windows and builds its loop **before** importing the app;
psycopg async refuses it → every DB request dies at connect, which
`TenantContextMiddleware` rendered as a bare `401 AUTH_UNRESOLVED`.

**Any DB-touching script needs**
`asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)`. `uvicorn.run(loop=...)` takes a **string**, not a class.

## Idempotency belongs in a constraint

`UNIQUE` + `ON CONFLICT DO NOTHING` beats read-then-write wherever replicas
race (ingestion claims, `uq_inbox_delivery`, `uq_case_escalation_once`,
`uq_saml_assertion_once`, `uq_billing_entry_event`, `uq_citation_claim`). An
RLS-scoped pre-check is worth having for the *message* only.

## Schema and migrations

- **`alembic_version` is a single-row head table** — use `walk_revisions()`.
- **Non-ASCII repo path**: `script_location = %(here)s` fails under
  `ConfigParser`; call `cfg.set_main_option("script_location", ...)`.
- **`DROP`/`CREATE DATABASE` cannot run in a transaction** — set
  `isolation_level="AUTOCOMMIT"` on the *engine*.
- **`(metadata -> 'k')::text` keeps JSON quotes**; `->>` does not. **A fixture
  that does not reproduce the production row is a coverage gap.**
- **A NOT NULL column filled by a trigger still needs an ORM `server_default`**,
  dialect-neutral (unit tests build on SQLite).
- **Enum columns are `String` holding lowercase *values*.** ORM `Enum(...)`
  needs `values_callable=_enum_values` or reads raise `LookupError`.
- **Tenant tables have no FK on `tenant_id`.** Compose parent FKs as
  `(parent_id, tenant_id) -> (id, tenant_id)`.
- **A test passing against a long-lived DB is not evidence about the
  migrations** — only `downgrade base && upgrade head` is. A missing `GRANT`
  shipped twice that way (`users` 0021, `tenant_domains` 0027);
  `test_schema_privileges.py` covers every table and `EXPECTED_MIGRATIONS` is a
  gate to bump per revision.
- **A guard that cannot be observed failing is not evidence.** `FOUND` is
  **false** after `EXECUTE ... INTO` even when the SELECT returned a row.

## Auth, middleware, types

- **Token-only endpoints must be in `middleware.py::EXEMPT_PATHS`** (or
  `EXEMPT_PREFIXES`, e.g. `/v1/webhooks/`). The middleware 401s non-exempt
  paths *before* the handler. `TestClient(platform_core.main.app)` is the only
  proof of reachability — a middleware-less `TestClient(fresh_app)` still passes.
- **Auth failures are deliberately indistinguishable** — unknown slug,
  suspended tenant, missing/inactive membership all raise `identity not found
  or inactive`; splitting them makes login an enumeration oracle.
- mypy "Duplicate module named `__main__`" = empty `src/__init__.py`; fix with
  `explicit_package_bases = true`.
- New domain code must be mypy-strict clean.

## LLM provider

Gitee AI (模力方舟), OpenAI-compatible, `https://ai.gitee.com/v1`:
`qwen3.8-flash` (chat; thinking channel separated from the answer),
`Qwen3-Embedding-8B` (honors `dimensions: 1536`, so `chunks.embedding
vector(1536)` needs no migration), `bge-reranker-v2-m3`. Credentials in `.env`
(gitignored); an unset key fails the model boundary closed.

## A test double's scale leaks into production logic

`_sources_compete` guarded "comparable ranking" with an **absolute** 0.05
margin, but `hybrid_search` fuses with RRF (scores are `sum(1/(60+rank))`, max
gap ~0.016) so the guard could never fire. The constant was calibrated against
`tests/evals/harness.py`.

Rules: a threshold comparing retrieval scores must be **relative**; a heuristic
exercised only through a harness must be **tested at the production scale**.
Validate a new heuristic against the whole eval dataset before wiring it in
(`qa_path._is_action_request` was deferred three times on "false-positive risk";
measuring over all 23 dataset questions settled it). **Test the caller, not just
the function** — the read-tool gate's tests bound `app.tenant_id` in the helper,
but the only real caller never did.

`scripts/run_eval.py` is the only path that runs the real pipeline end to end.

## Release gates

Zero-tolerance counts are **derived, not typed**: a test declares
`@pytest.mark.zero_tolerance("<invariant>")`, the
`pytest_plugins_release.gate_evidence` plugin writes
`tests/artifacts/release_gate_evidence.json`, and `evaluation/evidence.py` is
the only reader. A partial run (<500 tests) is refused. The writer is a plugin,
not a conftest, because the suite has two test roots.

`release_check` exits **0** all gates pass, **1** a gate failed, **2** inputs
unusable. `--evidence-only` is the CI check. **`scripts/run_eval.py` produces
`tests/artifacts/eval_report.json`** from the real pipeline — never from the
deterministic harness.

## Browser-testing the UI on Windows

`agent-browser` does not support Windows. What works: install
`playwright-core` into the managed node workspace and drive the **system**
Chrome via `executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe"`
(no browser download needed). Scripts live in
`.workbuddy-ai/acceptance/` (`journey.mjs`, `adversarial.mjs`, `vp.mjs`).

- **Restart the API after any Python change** — Vite has HMR, the API does
  not, so a stale server makes a fixed bug look unfixed. Preferred: start a
  second instance on another port (`APP_API_PORT=8010`) and point Vite at it
  with `VITE_API_TARGET`, rather than killing an existing process.
- Long runs get SIGTERM when run in the foreground; redirect to a file.
- Mobile checks must compare `window.innerWidth` against the emulated
  width: if content overflows, Chrome widens the *layout viewport* and
  `innerWidth` grows (was 594 on a 390px device). `scrollWidth` will look
  fine, so it is not a usable signal on its own.

## 起服务给用户试用的最短路径

后端（**必须**用 `-m platform_core.main`，不可用裸 uvicorn —— 见上文 Windows 段）：

```bash
PYTHONPATH="<绝对路径>;拼接" APP_API_PORT=8010 \
  ./.venv/Scripts/python.exe -m platform_core.main
```

前端 —— `VITE_API_TOKEN` 会被 `api.ts` 读（`localStorage` 优先），启动时带上
就**不用在 Token 弹窗里手输**；`VITE_API_TARGET` 默认是 8000，改端口时一定一起改：

```bash
VITE_API_TARGET=http://localhost:8010 VITE_API_TOKEN=pt_admin-demo_<user_id> \
  node node_modules/vite/bin/vite.js --port 5174 --strictPort
```

验证顺序：`/healthz` → 带 token 打一个业务接口 → 打不带 token 的看 401 →
再开浏览器确认不是白屏。四步都过再交给用户。

## Admin UI (`apps/admin-web`)

- **`ApiError` must extend `Error`.** As a plain object it fell through to
  `String(err)` → `[object Object]` in every error banner.
- `ok_response` **spreads** its payload (adding `trace_id`), so responses are
  `{usage: ...}` / `{billing: ...}`, not nested under `data`.
- Use `useAction()` + `<ActionFeedback>` for writes — no `alert()`.
  `useAsync` exposes `errorStatus`, so a page can tell a 403 (permission
  boundary → a note) from a 500 (→ a banner).
- `.env` needs `VITE_API_TOKEN`. Gates are only `tsc` + `vite build` —
  **front-end defects are invisible to pytest.**
- Pages: Cases, GapQueue, PromptRelease, QualityDashboard, Usage, Members,
  Branding, FeatureFlags. Components: Layout, Prompt, TokenDialog, ui.

## Tooling and conventions

- `scripts/seed_admin_demo.py` seeds the `admin-demo` tenant and prints
  `pt_admin-demo_<uuid>`. `scripts/backup_restore_drill.py` restores into a
  scratch DB; its exit **2** means "source was not quiescent", not data loss.
- Conventional-commit subject + a body explaining **why**; state the test count
  delta and ruff/mypy status. Read `AGENTS.md` before changing module
  boundaries.
- SAML login issues **no session**; a first SAML login **never provisions a
  role**; a SCIM **Group maps to a Department, never to a `MembershipRole`**;
  `infra/kubernetes/` has **never been applied to a cluster**.

## Standing conclusions from the 2026-09-18 audit (132 items)

Full mapping: `docs/interview-checklist-audit.md`. Gaps since closed by new
modules: multi-turn context/memory/compression → `agent_runtime/conversation.py`;
intent taxonomy → `agent_runtime/intent.py`; contextual query rewrite →
`conversation.rewrite_query`; retrieval-vs-generation attribution →
`evaluation/runner.py`.

Reuse, do not re-derive:
- `docs/agent.md` documents 7 routing classes and 7 context layers; before the
  audit only 2 routing classes and 1 context layer existed in code. **A doc that
  describes unbuilt behaviour is a spec, not evidence** — grep the doc's nouns
  against `apps/api/src` when auditing coverage.
- `uq_citation_claim` is `(agent_run_id, claim_index)`, so a claim carries
  **one** citation row though `docs/agent.md` says "one or more"; extra
  supporting chunks live in `retrieval_config`.
- Chunking had **no overlap** and `MAX_CHUNK_CHARS = 1200` hardcoded; `top_k=8`
  hardcoded in the orchestrator. Both are now config-driven.
- `retry_delays` had **no jitter** (thundering herd); now `jitter_ratio`.

## Reference customer research

`docs/research/huaqiu-research.md` — 深圳华秋智联 (huaqiu.com) as the benchmark
customer: business lines (PCB/PCBA/商城/方案/DFM/EDA), stakeholder roles, B2B
support scenarios, landing path. Public info only; unverified items tagged
`[假设]`.

## 项目定位（容易误解，务必先看）

**这个 repo 不是终端聊天产品，是"AI 控制平面"**。`docs/architecture.md` 开头即写明
"integrated with Chatwoot as an external bounded context"，上下文图是：
`Customer → Web chat/Email → Chatwoot → 签名 webhook → Support Bridge → AI Control Plane`
，回程 `AI --REST send message--> Chatwoot`。

所以"客户去哪提问"的正确答案是 **Chatwoot**，不是本仓库。本仓库只有：
企业侧管理后台（admin-web）+ `/v1/*` 管理 API + 一个 webhook 入口。

**但客户自服务门户是被规划过的、目前缺失**：`identity/domain_router.py` 有
`/v1/public/branding`，注释写着 "Unauthenticated by design: this is the
tenant's public page"，按 Host 解析租户。前端没做。这是"看起来应该有聊天界面
却没有"的真正原因，不是设计上就该去 Chatwoot。

另注：`docs/handover-summary.md` 记录了 embedding 曾为 `embed_deterministic`
哈希占位（RAG 质量本不可用），M1 才接真实 embedding —— 涉及检索效果时先确认现状。
