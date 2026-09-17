# Project Memory — B2B AI Customer Support Platform

Long-term conventions and environment facts. Daily logs live in `YYYY-MM-DD.md`.

## Environment

- **Working dir**: `D:/360Downloads/360驱动大师目录/b2b-ai-support-plan/b2b-ai-support-plan`
- **Python**: `.venv/Scripts/python.exe` (3.12.0, system-based). Managed runtime
  `C:/Users/Rose/.workbuddy-ai/binaries/python/versions/3.13.12/python.exe` has
  **no pytest** — always use the venv.
- **PYTHONPATH must use absolute paths joined by `;` on Windows**:
  ```bash
  R="D:/360Downloads/360驱动大师目录/b2b-ai-support-plan/b2b-ai-support-plan"
  export PYTHONPATH="$R/apps/api/src;$R/packages/contracts/src;$R/packages/policy/src;$R/packages/observability/src;$R/apps/worker/src"
  ./.venv/Scripts/python.exe -m pytest apps packages -q
  ```
  Using `:` or relative paths yields "No module named 'platform_core'" and makes
  it look like every test file failed to collect.
- **Docker ports**: ai-postgres `5435`, chatwoot-postgres `5434`, ai-redis `6380`,
  chatwoot-redis `6381`, Chatwoot `3000`, API `8000`, Keycloak `8081`, MinIO `9000/9001`.
- **Two DB roles**: `platform` (superuser, bypasses RLS — use for seeding/cleanup)
  and `platform_app` (NOBYPASSRLS — use for anything asserting isolation).
- `packages/observability/src/observability.py` is a top-level `observability`
  module, **not** `platform_core.observability`. Import as
  `from observability import JsonLogger, TraceContext, new_trace_context`.

## Non-obvious correctness rules

- **`set_config('app.tenant_id', ..., true)` is transaction-scoped.** It does not
  survive `COMMIT`. Any read after a commit must re-apply it, or RLS silently
  returns zero rows and you get `NoResultFound` — which looks exactly like
  "the data was never written". This has cost real debugging time.
- **ProviderRole is a StrEnum.** Serialize chat roles with `str(m.role)`, not
  `m.role.value`; the latter breaks when a plain equivalent string is passed.
- **pytest's summary line goes to stderr.** `| grep passed` in a pipeline can
  come back empty even on a fully green run. Trust the **exit code** (0 = all pass).
- **mypy "Duplicate module named `__main__`"** means there are empty
  `src/__init__.py` files making each `src/` look like a package root. The fix is
  `explicit_package_bases = true` plus removing the empty inits — not changing code.
- Celery/Redis for the custom platform must stay **separate** from Chatwoot's Redis.

## LLM provider

- Gitee AI (模力方舟), OpenAI-compatible, `https://ai.gitee.com/v1`.
- Models: `qwen3.8-flash` (chat, reasoning — thinking channel is separated from
  the answer), `Qwen3-Embedding-8B`, `bge-reranker-v2-m3`.
- **`Qwen3-Embedding-8B` is natively 1024-dim but honors `dimensions: 1536`**, so
  the existing `chunks.embedding vector(1536)` column needs no migration.
- Credentials live in `.env` (gitignored). `.env.example` is the tracked template.
  Never commit the real key; unset key means the model boundary fails closed.

## Verification commands

```bash
./.venv/Scripts/python.exe -m pytest apps packages -q -W ignore   # expect exit 0
./.venv/Scripts/python.exe -m ruff check apps packages scripts
./.venv/Scripts/python.exe -m ruff format --check apps packages scripts
./.venv/Scripts/python.exe scripts/smoke_gitee_ai.py             # live provider check
docker compose -f infra/compose/docker-compose.yml config --quiet
```

## Delivery conventions

- Commits: conventional-commit subject, then a body explaining **why**. State the
  test count delta and the ruff/mypy status.
- New domain code must be mypy-strict clean. There are ~49 pre-existing errors in
  older modules; do not add to them and do not fix them incidentally.
- Architecture rules and prohibited shortcuts are in `AGENTS.md` — read it before
  changing module boundaries.

## Integration test gotchas

- **alembic `Config` + non-ASCII repo path**: `script_location = %(here)s` in
  `alembic.ini` is read by `ConfigParser`, which **silently fails** when the
  path contains Chinese chars (e.g. `360驱动大师目录`); `command.upgrade` then
  dies with "No 'script_location' key found". Fix: call
  `cfg.set_main_option("script_location", str(ALEMBIC_INI.parent))` after
  `Config(...)`. Also, for a test under `apps/api/tests/integration/`,
  `ALEMBIC_INI` is `Path(__file__).resolve().parents[4]` (the workspace is
  `.../b2b-ai-support-plan/b2b-ai-support-plan/apps/...`, so `parents[3]` is the
  inner `apps/` dir, not the repo root).
- **psycopg async needs `SelectorEventLoop` on Windows**: a plain `asyncio.run`
  uses `ProactorEventLoop`, which psycopg rejects. Run async DB benchmarks with
  `asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)`.
- **`alembic_version` is a single-row "current head" table** — `SELECT count(*)`
  returns 1, NOT the migration count. Count migrations with
  `len(list(ScriptDirectory.from_config(cfg).walk_revisions()))`; verify at-head
  via `version_num IN ScriptDirectory(...).get_heads()`.
- **`DROP`/`CREATE DATABASE` cannot run inside a transaction**: set
  `create_engine(url).execution_options(isolation_level="AUTOCOMMIT")` on the
  *engine* before connecting (not on a connection already inside `begin()`).
- **local Postgres `max_connections=100` is tight**: size async pools well below
  it (pool_size=30) for concurrency benchmarks, or connections get refused and
  latency explodes.

## Windows startup — the API must not be launched with bare uvicorn

**Always start the API with `python -m platform_core.main`.** Never
`uvicorn platform_core.main:app` on Windows.

Reason: uvicorn's `uvicorn/loops/asyncio.py::asyncio_loop_factory` hardcodes
`ProactorEventLoop` when `sys.platform == "win32" and not use_subprocess`, and
uvicorn builds its loop **before** importing the app — so an import-time
`set_event_loop_policy` in `main.py` cannot influence it. psycopg async refuses
to run on a Proactor loop, so every DB-backed request dies at connect time.

The failure mode was nasty: `TenantContextMiddleware.dispatch` caught the
exception and returned a bare `401 AUTH_UNRESOLVED`, which is indistinguishable
from a bad token. It looked like an auth bug for a long time.

`platform_core.db.ensure_async_db_loop()` now raises a loud, actionable
`RuntimeError` instead, so the diagnosis is immediate. `main.py::run()` passes
`loop=asyncio.SelectorEventLoop` explicitly.

## Migrations that read FORCE-RLS tables

`memberships` is FORCE RLS, so its rows are invisible unless `app.tenant_id` is
bound — but auth resolution is what *discovers* the tenant, so it logically runs
before any binding exists. Migration `0015_membership_bootstrap` solves this with
`resolve_active_membership(slug, user_id)`: a read-only `SECURITY DEFINER`
function granting EXECUTE (never SELECT) back to `platform_app`.

Do **not** "fix" a future variant of this by loosening the policy to
`USING (app.tenant_id IS NULL OR ...)`. That grants table-wide read across every
tenant and `test_cross_tenant_leak_surfaces.py` will (correctly) fail.

`resolve_identity` (single probe, function-based) and `resolve_by_slug`
(two-step: tenants → bind → memberships) are both live and must agree. Which to
use: single probe when you only need identity, two-step when you also need tenant
settings.

**Enum columns are `String` in the migrations and hold lowercase enum *values*.**
Any ORM `Enum(...)` must pass `values_callable=_enum_values`, or SQLAlchemy looks
for member *names* (`ACTIVE`) and every read raises `LookupError`.

**Auth failures are deliberately indistinguishable.** Unknown slug, suspended
tenant, missing membership and inactive membership all raise the same
`identity not found or inactive`. Splitting them would make login a tenant/user
enumeration oracle. If a test wants to tell them apart, it must use the two-step
path, not the single probe.

## Changing the migration count

`test_migration_and_performance.py::EXPECTED_MIGRATIONS` is a deliberate gate —
it exercises the whole chain down to base and back up on a fresh database. Bump
it when adding a revision; a non-reversible migration then fails in CI rather
than during a production rollback.

## The outbox is global — test suites that touch it must drain it

`claim_pending` scans `outbox_events` with **no tenant filter**, which is correct
in production (one relay drains everything). The consequence for tests: any
queued row left by another suite is claimed by the relay suite's batch, and
`stats.claimed == 1` style assertions then measure the wrong thing.

This produces an **intermittent, ordering-dependent** failure that looks exactly
like a bug in the relay. `test_outbox_relay.py::_cleanup` therefore also deletes
rows with `status = 'queued'`. Rows in `sent` are inert.

If you add a test that commits an outbox event, either clean up after yourself or
accept that the relay suite depends on you doing so.

## RLS fails silently on writes, not just reads

The read path is the well-known one: an unbound `SELECT` on a FORCE-RLS table
returns **zero rows**, which is indistinguishable from "no data". Measured on
`document_versions`:

```
SELECT count(*) FROM document_versions   -- platform → N rows, platform_app → 0
```

The write path is worse and easy to miss — **the same is true of `UPDATE`**:

```
unbound UPDATE document_versions ... WHERE id = <known id>   -- rowcount 0, NO error
bound   UPDATE document_versions ... WHERE id = <known id>   -- rowcount 1
```

No exception is raised. A claim that reported success (`claimed=1`) persisted
nothing, and the only reason it was caught is that a later assertion read the
row back. **Any `UPDATE`/`DELETE` issued from an unbound app-role session must
assert its `rowcount`**, or the failure surfaces far from its cause — or not at
all. When the tenant is only discoverable from the row itself, bind per row
(the tenant comes from the row, so this is not an escalation).

Same root cause, same remedy as the auth bootstrap: a narrow `SECURITY DEFINER`
function with a pinned `search_path`, `REVOKE ALL FROM PUBLIC`, and EXECUTE
granted only to `platform_app`. Now used by `0015`, `0016`, `0018` and `0019` —
four instances, so treat this as the standing pattern, not a one-off.

Corollary: `FOR UPDATE` requires the function to be declared **`VOLATILE`**.
PostgreSQL refuses it in a non-volatile function at creation time.

## Cross-tenant queue scans are global — by design, and tests must account for it

`claim_ingestion_versions` (like the outbox relay) has **no tenant filter**: one
bulk worker serves every tenant, which is correct. The test consequence is that
**any** leftover claimable row anywhere enters the next suite's batch.

A manual probe that left one `uploaded` row in a *seeded* tenant made
`test_ingestion_produces_chunks_and_marks_ready` fail with
`IngestStats(claimed=2, ready=2)` — a message that reads exactly like a pipeline
bug. `test_ingestion_worker.py`'s autouse fixture therefore moves claimable rows
belonging to *other* tenants to `expired` (terminal, not claimable). It does not
delete them: that is someone else's data, and altering data to make a test pass
is the wrong fix.

**Consequence for working in this repo: never leave probe rows in the database.**
Ad-hoc `scripts/_probe_*.py` that write to the real DB are the leak source. Use
a throwaway tenant and clean it up, or do not write at all.

## Timestamps the database owns need `server_default` in the ORM too

Adding a NOT NULL column whose value comes from a trigger is not enough on its
own. SQLAlchemy emits the column in the `INSERT` with an **explicit NULL** when
the ORM metadata declares no default, and **an explicit NULL overrides the
column's `DEFAULT`** — so the constraint fires before the trigger can help.
Measured: every upload returned HTTP 503
`IntegrityError: null value in column "updated_at"`.

The `server_default` must be a **dialect-neutral literal** (e.g. `0`), not a
Postgres expression like `EXTRACT(EPOCH FROM now())`. Unit tests build the
schema via `Base.metadata.create_all` on SQLite, which cannot parse it — that
single mistake broke all 13 `tool_gateway` tests with a DDL error. On Postgres
the trigger overwrites the placeholder, so the true value still wins.

## Two embedder interfaces, deliberately

- Worker / ingestion needs the **batch** shape:
  `embed(texts) -> EmbeddingResult`.
- `hybrid_search` needs the **query** shape: `Embedder.embed_query(query)`.

A stub that implements only one will let a test pass while the other path is
never exercised. When faking embeddings, implement both, and note which caller
uses which.

## Local tooling

- `scripts/seed_admin_demo.py` creates the `admin-demo` tenant + `tenant_owner`
  user and prints a bootstrap token (`pt_admin-demo_<uuid>`). Use it to exercise
  the API and admin-web by hand; it is throwaway local tooling, not product code.
- Start the API with `python -m platform_core.main` (see Windows startup above),
  Vite with `npx vite`. The Vite dev server proxies `/api` → `localhost:8000`.
- `admin-web` `.env` needs `VITE_API_TOKEN`; `.env.example` is the template.
- Real end-to-end ingestion check (needs the `b2b-e2e-minio` container on
  `19000`, and MinIO credentials in `APP_OBJECT_STORAGE_*`):
  `./.venv/Scripts/python.exe tests/e2e/e2e_ingestion_minio.py`
  — uploads, runs the real worker, and asserts `hybrid_search` recall.
