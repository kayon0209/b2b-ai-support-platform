# Project Memory — B2B AI Customer Support Platform

Durable rules only. Daily logs (`YYYY-MM-DD.md`) hold the narrative and
rationale. **Keep this file tight** — it is truncated on injection when it grows
large, so it is ordered most-critical-first and the tail is the part that gets
lost. Consolidated 2026-09-18 from ~16KB; trim rationale, never a rule.

## Environment

- **Dir**: `D:/360Downloads/360驱动大师目录/b2b-ai-support-plan/b2b-ai-support-plan`
- **Python**: `.venv/Scripts/python.exe` (3.12). The managed 3.13 runtime has
  **no pytest**.
- **PYTHONPATH: absolute, `;`-joined** — relative or `:`-joined gives
  `No module named 'platform_core'` and looks like every test failed to
  collect: `$R/apps/api/src;$R/packages/contracts/src;$R/packages/policy/src;$R/packages/observability/src;$R/apps/worker/src`
  where `R` is the dir above.
- **Ports**: ai-postgres `5435`, chatwoot-postgres `5434`, ai-redis `6380`,
  chatwoot-redis `6381`, Chatwoot `3000`, API `8000`, Keycloak `8081`, MinIO
  `9000/9001`; the e2e uses a pre-existing `b2b-e2e-minio` on `19000`.
- **DB roles**: `platform` (superuser, bypasses RLS — seeding/cleanup),
  `platform_app` (NOBYPASSRLS — anything asserting isolation).
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

pytest's summary goes to **stderr**, and `EXIT=$?` after a pipe reports the
last command — use `${PIPESTATUS[0]}` or no pipe. CI's `release-evidence` job
runs the **full** suite (not `-m integration`: `unauthorized_writes` is backed
by unit tests) then `release_check --evidence-only`.

**Stop the `ai-*` services before running the suite.** `ai-worker-interactive`
runs an outbox relay that claims rows every second, so a test that commits an
outbox event and then asserts on its own relay's batch sees `claimed=0`. The
symptom is a single intermittent failure in `test_billing_ledger` /
`test_outbox_relay` that passes when run alone. Same root cause as the
"outbox is global" rule below: one shared queue, two consumers.

## The recurring defect family: a capability with no consumer

Every audit round finds the same shape — a value, column, or function that
exists and is tested but that no production path reads or writes. Found:
`AgentRun.started_at`, `AgentRun.token_usage`, `sweep_expired_data`,
`credential_ref`, priority-queue admission, `_is_identity_dependent`,
`health_check`, `NEEDS_REAUTH`, `SyncCursor`, `DeadLetterItem`,
`flag_service.evaluate`, the reranker, `cases.enterprise_account_id`,
`release_check`'s CI caller, `eval_report.json`'s producer,
`drain_outbox_once` + `_now`.

**For any field a reader depends on, grep its writers/callers.** A test or doc
calling a function directly will not reveal a missing production caller.

## RLS

**Bootstrap pattern (8 instances).** A tenant-owned table read *before* a
binding exists needs a narrow `SECURITY DEFINER` function: pinned
`SET search_path = pg_catalog, public`, `REVOKE ALL FROM PUBLIC` and from
`platform`, `GRANT EXECUTE` to `platform_app` only, `RETURNS TABLE (...)`
(0015, 0016, 0018, 0019, 0026, 0027, 0030 ×2).

Never loosen a policy to `USING (app.tenant_id IS NULL OR ...)` — that grants
table-wide read across tenants and `test_cross_tenant_leak_surfaces.py` fails.

**RLS fails silently on writes.** Unbound `SELECT` → zero rows; unbound
`UPDATE`/`DELETE` → `rowcount 0` with **no error**. Assert `rowcount` on any
app-role write. `FOR UPDATE` requires the function be `VOLATILE`.

**Cross-tenant queue scans are global by design** (`claim_pending`,
`claim_ingestion_versions`, `reclaim_stale_ingestions` have no tenant filter),
so a leftover claimable row anywhere enters the next suite's batch and looks
like a pipeline bug. **Never leave probe rows in the database.**

## Async and sessions

- **`set_config('app.tenant_id', ..., true)` is transaction-scoped** — it does
  not survive `COMMIT`, and an unbound read returns zero rows with no error.
  **`tenant_session` now rebinds on `after_begin`**, so a handler that commits
  mid-request keeps the binding. Every tenant-scoped handler must use
  `tenant_session(ctx)`; 22 sites used to open `session_scope_with_url(...)` +
  `apply_rls_tenant(...)` by hand and none rebound.
- **`db.app_role_url()` is the only place the app-role URL is computed.** Five
  modules derived it independently (including the auth middleware and the
  worker wiring). Its docstring: *"a second copy is how one of them ends up
  pointing at the owner."*
- **A failed flush poisons the session.** `await session.rollback()` before
  raising, or the router's `commit()` raises `PendingRollbackError` and a clean
  409 becomes a 500 (`begin_nested()` does **not** help). Prefer `ON CONFLICT
  DO NOTHING` to insert-then-catch: a rollback discards *the whole
  transaction*, including earlier rows in the same sweep.
- **The outbox relay's caller owns the transaction.** `run_once(session)` does
  not commit; pass `commit=True` or a `session_scope()`, or it reports `sent=1`
  while persisting nothing. Each row is dispatched under its **own** tenant's
  RLS binding — the claim runs before any tenant is known, and without the
  binding an insert failing `WITH CHECK` becomes zero rows and no error.

## Windows: never launch the API with bare uvicorn

**Always `python -m platform_core.main`.** uvicorn hardcodes
`ProactorEventLoop` on Windows and builds its loop **before** importing the
app, so an import-time `set_event_loop_policy` cannot help. psycopg async
refuses a Proactor loop → every DB request dies at connect, and
`TenantContextMiddleware` rendered that as a bare `401 AUTH_UNRESOLVED`.

**Any DB-touching script needs**
`asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)` (the integration
test convention). It silently disabled `release_check`'s read-tool gate.
`uvicorn.run(loop=...)` takes a **string**, not a class.

## Idempotency belongs in a constraint

`UNIQUE` + `ON CONFLICT DO NOTHING` beats read-then-write wherever replicas
race (ingestion claims, `uq_inbox_delivery`, `uq_case_escalation_once`,
`uq_saml_assertion_once`, `uq_billing_entry_event`). An RLS-scoped pre-check is
worth having for the *message* only; the constraint is the authority.

## Schema and migrations

- **`alembic_version` is a single-row head table** — `count(*)` is 1, not the
  migration count; use `walk_revisions()`.
- **Non-ASCII repo path**: `script_location = %(here)s` silently fails under
  `ConfigParser`; call `cfg.set_main_option("script_location", ...)`.
- **`DROP`/`CREATE DATABASE` cannot run in a transaction** — set
  `isolation_level="AUTOCOMMIT"` on the *engine*.
- **`(metadata -> 'k')::text` keeps JSON quotes**; `->>` does not. A content
  type read with `->` reached the parser as `"text/markdown"` and matched
  nothing, while the fixture (seeding no `content_type`) stayed green — **a
  fixture that does not reproduce the production row is a coverage gap.**
- **A NOT NULL column filled by a trigger still needs an ORM
  `server_default`** (SQLAlchemy emits an explicit NULL, which overrides the
  DEFAULT), and it must be dialect-neutral — unit tests build on SQLite.
- **Enum columns are `String` holding lowercase *values*.** ORM `Enum(...)`
  needs `values_callable=_enum_values` or reads raise `LookupError`.
- **Tenant tables have no FK on `tenant_id`** (RLS enforces isolation, not
  constraints), so orphans are possible. Compose parent FKs as
  `(parent_id, tenant_id) -> (id, tenant_id)`; a single-column FK would accept
  another tenant's row and RLS would hide it.
- **A test passing against a long-lived DB is not evidence about the
  migrations** — only `downgrade base && upgrade head` is. A missing `GRANT`
  shipped twice that way (`users` 0021, `tenant_domains` 0027);
  `test_schema_privileges.py` now covers every table, and
  `EXPECTED_MIGRATIONS` is a gate to bump per revision.
- **A guard that cannot be observed failing is not evidence.** The acyclicity
  trigger was installed, enabled, and inert: `FOUND` is **false** after
  `EXECUTE ... INTO` even when the SELECT returned a row. Terminate a PL/pgSQL
  walk on the **value**.

## Auth, middleware, types

- **Token-only endpoints must be in `middleware.py::EXEMPT_PATHS`** (or
  `EXEMPT_PREFIXES`, e.g. `/v1/webhooks/`). The middleware 401s non-exempt
  paths *before* the handler, so the endpoint is unreachable in the real app
  while a middleware-less `TestClient(fresh_app)` test still passes.
  `TestClient(platform_core.main.app)` is the only proof of reachability.
- **Auth failures are deliberately indistinguishable** — unknown slug,
  suspended tenant, missing/inactive membership all raise `identity not found
  or inactive`; splitting them makes login an enumeration oracle.
  `resolve_identity` and `resolve_by_slug` must agree.
- **mypy "Duplicate module named `__main__`"** = empty `src/__init__.py`
  files; fix with `explicit_package_bases = true` and remove them.
- New domain code must be mypy-strict clean. `opentelemetry.*`,
  `lxml.*`/`signxml.*`, `alembic.*`, `jsonschema.*`/`uuid6.*` are in
  `ignore_missing_imports`.

## LLM provider

Gitee AI (模力方舟), OpenAI-compatible, `https://ai.gitee.com/v1`:
`qwen3.8-flash` (chat; thinking channel separated from the answer),
`Qwen3-Embedding-8B` (natively 1024-dim but honors `dimensions: 1536`, so
`chunks.embedding vector(1536)` needs no migration), `bge-reranker-v2-m3`.
Credentials in `.env` (gitignored); an unset key fails the model boundary
closed.

## A test double's scale leaks into production logic

`_sources_compete` guarded "comparable ranking" with an **absolute** 0.05
margin. `hybrid_search` fuses with RRF, so a score is `sum(1/(60+rank))`: the
top two are 1/61 and 1/62, a gap of **0.000264**, and the largest possible gap
is ~0.016. The guard could never fire, so conflict detection collapsed into "do
both passages contain numbers?" and answerable questions were handed off. The
constant was calibrated against `tests/evals/harness.py` (term-overlap
fractions spanning 0..1), and **no unit test scored a chunk outside 0.2–0.9**.

Rules: a threshold comparing retrieval scores must be **relative**; a heuristic
exercised only through a harness must be **tested at the production scale**.

**Same shape in the read-tool gate**: its tests bound `app.tenant_id` in the
test helper, but the only real caller never did, so it read zero rows forever.
**Test the caller, not just the function** — "the aggregation works" and
"anything reaches it" are different claims.

`scripts/run_eval.py` found the first, and is the only path that can: it is the
only one that runs the real pipeline end to end.

## Release gates

Zero-tolerance counts are **derived, not typed**: a test declares
`@pytest.mark.zero_tolerance("<invariant>")`, the
`pytest_plugins_release.gate_evidence` plugin writes
`tests/artifacts/release_gate_evidence.json`, and `evaluation/evidence.py` is
the only reader. A skipped, deselected or dropped backing test makes the count
non-zero or the evidence unusable; a partial run (<500 tests) is refused. The
writer is a plugin, not a conftest, because the suite has two test roots.

`release_check` exits **0** all gates pass, **1** a gate failed, **2** inputs
unusable. `--evidence-only` is the CI check; `--skip-db` still fails the
read-tool gate by design. **`scripts/run_eval.py` produces
`tests/artifacts/eval_report.json`** from the real pipeline (throwaway tenant,
real worker + embeddings, real ACL/status filters, live model) and cleans up
after itself. Never generate it from the deterministic harness: that feeds the
gate an oracle's numbers. `docs/testing-and-evaluation.md` documents the
sequence. Read-tool telemetry needs a live tenant, not a fixture.

## Admin UI (`apps/admin-web`)

- **`ApiError` must extend `Error`.** Every call site reads
  `err instanceof Error ? err.message : String(err)`; as a plain object it fell
  through to `String(err)`, so every error banner and `alert()` rendered
  `[object Object]`.
- `ok_response` **spreads** its payload (adding `trace_id`), so responses are
  `{usage: ...}` / `{billing: ...}`, not nested under `data`.
- Use `useAction()` + `<ActionFeedback>` for writes — no `alert()` (blocks the
  tab, unstylable, not a live region). `useAsync` exposes `errorStatus`, so a
  page can tell a 403 (permission boundary → a note) from a 500 (→ a banner).
- Vite proxies `/api` → `localhost:8000`; `.env` needs `VITE_API_TOKEN`. Gates
  are only `tsc` + `vite build` — front-end defects are invisible to pytest.

## Tooling and conventions

- `scripts/seed_admin_demo.py` seeds the `admin-demo` tenant and prints
  `pt_admin-demo_<uuid>`; `scripts/backup_restore_drill.py` restores into a
  scratch DB and asserts counts, per-tenant distribution, audit min/max, outbox
  status and resume guards. Its exit **2** means "source was not quiescent",
  not data loss.
- Conventional-commit subject + a body explaining **why**; state the test count
  delta and ruff/mypy status. Read `AGENTS.md` before changing module
  boundaries. SAML login issues **no session**; a first SAML login **never
  provisions a role**; a SCIM **Group maps to a Department, never to a
  `MembershipRole`**; `infra/kubernetes/` has **never been applied to a
  cluster**.
