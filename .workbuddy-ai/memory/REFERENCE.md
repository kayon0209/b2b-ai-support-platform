# Reference — long-tail detail split out of MEMORY.md

Not auto-injected. Read on demand. MEMORY.md holds the rules that change
behaviour often; this file holds detail that is expensive to rediscover but
rarely needed. Split out on 2026-09-19 (MEMORY.md v3).

## Serving the stack for a manual walkthrough

Backend **must** be `python -m platform_core.main` (see MEMORY.md → Windows):

```bash
PYTHONPATH="<absolute ;-joined>" APP_API_PORT=8010 \
  ./.venv/Scripts/python.exe -m platform_core.main
```

Front-end: `VITE_API_TOKEN` is read by `api.ts` (localStorage wins) so the Token
dialog can be skipped; `VITE_API_TARGET` defaults to 8000 and must be changed
with the port:

```bash
VITE_API_TARGET=http://localhost:8010 VITE_API_TOKEN=pt_admin-demo_<user_id> \
  node node_modules/vite/bin/vite.js --port 5174 --strictPort
```

Verify in order: `/healthz` → an authenticated business call → the same call
without a token (expect 401) → open the browser and confirm it is not blank.
**Restart the API after any Python change** — Vite has HMR, the API does not, so
a stale server makes a fixed bug look unfixed. Preferred: start a second instance
on `APP_API_PORT=8010` and point Vite at it with `VITE_API_TARGET` rather than
killing an existing process.

**Check the port is actually free before reusing 8010/5174.** A previous
session's instance can still be listening, and it does not fail loudly:

- An old API bound to `127.0.0.1:8010` and a new one bound to `0.0.0.0:8010`
  **both report "Uvicorn running on ...:8010"**. Loopback traffic goes to the
  more specific binding, so requests reach the *old* process and a
  newly-added endpoint answers 405 — which looks exactly like a route that was
  never registered. `netstat -ano | grep :8010` shows the bound address and the
  PID; the fix is a port nobody holds (8020 worked).
- A previous Vite can hold `[::1]:5174` (IPv6 only), so `curl 127.0.0.1:5174`
  times out while the port is demonstrably "in use". Bind a fresh port.
- `curl` picks up the environment proxy and returns **502 for localhost**. Use
  `--noproxy '*'` for every local call.
- Write curl output to a **file** (`-o`), not through a pipe: `head -c` closing
  the pipe makes curl exit 23 and the body never lands, which reads as an empty
  response.

Starting the worker (default queue is `interactive`; override with
`APP_WORKER_QUEUE=outbox|ingestion|retention|sla`):

```bash
PYTHONPATH="<worker/src>;<api/src>;<contracts/src>;<policy/src>;<observability/src>" \
  APP_CHATWOOT_API_TOKEN=<chatwoot user token> \
  ./.venv/Scripts/python.exe -m worker.runner
```

## Browser-testing the UI on Windows

`agent-browser` does not support Windows. What works: install `playwright-core`
into the managed node workspace and drive the **system** Chrome via
`executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe"` (no
browser download needed). Scripts are authored in `.workbuddy-ai/acceptance/`
and **run from `~/.workbuddy-ai/binaries/node/workspace/`** — copy the `.mjs`
into that tree's `acceptance/` first, because `playwright-core` is installed
there and **ESM ignores `NODE_PATH`** (`ERR_MODULE_NOT_FOUND` otherwise).

- Run long scripts **in the background with output redirected to a file**;
  a foreground run gets SIGTERM partway through.
- **Chrome's `innerText` returns *rendered* text.** `.section-title` is
  `text-transform: uppercase`, so an assertion on `"Frozen arguments"` fails
  against `"FROZEN ARGUMENTS"`. Match case-insensitively.
- A deliberately-provoked 4xx/5xx is logged as a console error; filter it out of
  any "page errors" count or the harness is permanently red.
- An acceptance harness that needs pending data must **create it**: tool
  proposals expire 15 minutes after they are raised, so a run against
  yesterday's rows tests the empty state instead.
- Wait for a *derived* state, not the banner: an action's success banner can
  appear before the follow-up refetch lands, so asserting on a button's
  enabled-ness immediately after is a race in the harness, not a page defect.

- Long runs get SIGTERM when run in the foreground; redirect to a file.
- Mobile checks must compare `window.innerWidth` against the emulated width: if
  content overflows, Chrome widens the *layout viewport* and `innerWidth` grows
  (was 594 on a 390px device). `scrollWidth` will look fine, so it is not a
  usable signal on its own.

## Migration and fixture gotchas

- **`(metadata -> 'k')::text` keeps JSON quotes**; `->>` does not. **A fixture
  that does not reproduce the production row is a coverage gap.**
- **A NOT NULL column filled by a trigger still needs an ORM `server_default`**,
  dialect-neutral (unit tests build on SQLite).
- **Non-ASCII repo path**: `script_location = %(here)s` fails under
  `ConfigParser`; call `cfg.set_main_option("script_location", ...)`.

## Idempotency and schema (full form)

- **Idempotency belongs in a constraint.** `UNIQUE` + `ON CONFLICT DO NOTHING`
  beats read-then-write wherever replicas race (`uq_inbox_delivery`,
  `uq_case_escalation_once`, `uq_saml_assertion_once`, `uq_billing_entry_event`,
  `uq_citation_claim`). An RLS-scoped pre-check is worth having for the *message* only.
- `alembic_version` is a single-row head table — use `walk_revisions()`.
  `DROP`/`CREATE DATABASE` cannot run in a transaction — set engine
  `isolation_level="AUTOCOMMIT"`.
- **Enum columns are `String` holding lowercase *values*.** ORM `Enum(...)` needs
  `values_callable=_enum_values` or reads raise `LookupError`.
- Tenant tables have **no FK on `tenant_id`**; compose parent FKs as
  `(parent_id, tenant_id) -> (id, tenant_id)`.
- **A test passing against a long-lived DB is not evidence about the migrations** —
  only `downgrade base && upgrade head` is. A missing `GRANT` shipped twice that
  way (`users` 0021, `tenant_domains` 0027); `test_schema_privileges.py` covers
  every table and `EXPECTED_MIGRATIONS` is a gate to bump per revision.
- **A guard that cannot be observed failing is not evidence.** `FOUND` is
  **false** after `EXECUTE ... INTO` even when the SELECT returned a row.

## Auth, middleware, types

- **Token-only endpoints must be in `middleware.py::EXEMPT_PATHS`** (or
  `EXEMPT_PREFIXES`, e.g. `/v1/webhooks/`). The middleware 401s non-exempt paths
  *before* the handler. `TestClient(platform_core.main.app)` is the only proof of
  reachability — a middleware-less `TestClient(fresh_app)` still passes.
- Auth failures are deliberately indistinguishable (unknown slug, suspended
  tenant, missing/inactive membership all raise `identity not found or inactive`);
  splitting them makes login an enumeration oracle.
- mypy "Duplicate module named `__main__`" = empty `src/__init__.py`; fix with
  `explicit_package_bases = true`. New domain code must be mypy-strict clean.

## Admin UI (`apps/admin-web`)

- `ApiError` must extend `Error` — as a plain object it fell through to
  `String(err)` → `[object Object]` in every error banner.
- `ok_response` **spreads** its payload (adding `trace_id`), so responses are
  `{usage: ...}` / `{billing: ...}`, not nested under `data`.
- Use `useAction()` + `<ActionFeedback>` for writes — no `alert()`. `useAsync`
  exposes `errorStatus`, so a page can tell a 403 (permission boundary → a note)
  from a 500 (→ a banner).
- `.env` needs `VITE_API_TOKEN`. Gates are only `tsc` + `vite build` —
  **front-end defects are invisible to pytest.**
- Pages: Cases, GapQueue, PromptRelease, QualityDashboard, Usage, Members,
  Branding, FeatureFlags. Components: Layout, Prompt, TokenDialog, ui.

## Heuristic and test design

- A threshold comparing retrieval scores must be **relative**. `_sources_compete`
  guarded "comparable ranking" with an **absolute** 0.05 margin, but
  `hybrid_search` fuses with RRF (scores are `sum(1/(60+rank))`, max gap ~0.016)
  so the guard could never fire — the constant was calibrated against
  `tests/evals/harness.py`.
- A heuristic exercised only through a harness must be **tested at the production
  scale**. Validate a new heuristic against the whole eval dataset before wiring
  it in (`qa_path._is_action_request` was deferred three times on
  "false-positive risk"; measuring over all 23 dataset questions settled it).
- **Test the caller, not just the function** — the read-tool gate's tests bound
  `app.tenant_id` in the helper, but the only real caller never did.

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

## Historical note

`docs/handover-summary.md` records that embedding was once the
`embed_deterministic` hash placeholder (RAG quality was unusable by
construction) and real embedding only landed at M1 — confirm the current state
before judging retrieval quality.

---

## Folded from MEMORY.md on 2026-09-19 (v6 trim)

Verbose originals of the sections now compressed in MEMORY.md.

### Recurring defect — the full list found so far

`AgentRun.started_at`, `token_usage`, `sweep_expired_data`, `credential_ref`,
`_is_identity_dependent`, `health_check`, `NEEDS_REAUTH`, `SyncCursor`, `DeadLetterItem`,
`flag_service.evaluate`, the reranker, `cases.enterprise_account_id`, `drain_outbox_once`,
`claim_contradiction_candidates`, `Route.BUSINESS_WRITE` (produced by `intent.py`, no
orchestrator branch). Also: `CaseConversation` had a reader and **no writer**, so
`worker.priority_claim_enabled` was a silent no-op; `GET /v1/tools` offered tools the caller
could never propose.

### Errors-as-4xx — the original three

Three routers hand-built `{"error": ...}`, so domain refusals arrived as 200 and the admin
UI's `unwrap()` (only `!res.ok` counts) reported success — "Promoted" showed green for a
promotion that never happened. `DOMAIN_ERROR_STATUS` in `api.py` maps codes, unknown → 400.
`main.py` handles `RequestValidationError` (422 → envelope, never echoes `input`) and
`Exception` (500 → envelope + trace_id).

### RLS bootstrap migrations

0015, 0016, 0018, 0019, 0026, 0027, 0030 ×2 — plus 0037 (`resolve_turn_text`).
Never `USING (app.tenant_id IS NULL OR ...)`: table-wide read, fails
`test_cross_tenant_leak_surfaces.py`.

### Customer portal extras

`support_bridge/minimize.py` deliberately **excludes customer content**; the worker must
re-fetch from Chatwoot by `message_id`, so a run queued without a Chatwoot source goes
`queued → completed` with no agent turn — closing that loop is a product/compliance
decision, not a bug. 方案 A (`63407de`): `POST /v1/customer/conversations/{ref}/messages`
writes `conversation_turns` (redacted on write, dedup on `(conversation, text_hash)`).
`scripts/seed_admin_demo.py` prints `pt_admin-demo_<uuid>`; `scripts/backup_restore_drill.py`
exit **2** = "source was not quiescent".

---

## Folded from MEMORY.md on 2026-09-21 (v8 trim)

MEMORY.md was exceeding the injection limit and being silently truncated, so these four
situational sections moved here and MEMORY.md now carries one-line rules plus a pointer.
**Nothing was deleted** — this is the full text as of v8.

### Windows traps (full)

- **API must be `python -m platform_core.main`, never bare uvicorn** (ProactorEventLoop → psycopg async
  refuses → every DB request dies at connect → bare `401 AUTH_UNRESOLVED`).
- **`curl --noproxy "*"`** (a host proxy `:55940` 502s `localhost`, reading as a broken route); write bodies
  with `-o`, not a pipe (exit 23 / empty body).
- **Port ghosts:** an old `127.0.0.1:PORT` beats your new `0.0.0.0:PORT`; loopback reaches old code →
  404/405 that look like unregistered routes. Judge by `netstat -ano` bind address + PID; `taskkill` may not
  work — change port instead.
- **Long runs exhaust sockets** (~20 h: `WinError 10055` → `/healthz` 502 or silent exit — not a code bug).
  **Kill services you started before ending a session** (a leaked API+Vite ran 29 h → `fork: Cannot allocate
  memory`).
- **`docker` CLI can hang entirely** while containers still run — use `psycopg` directly.
- **Process inspection:** the PowerShell tool is silently sandboxed and `wmic` is blacklisted. Use `tasklist`
  (PID/name) + `netstat -ano | grep ":5435"` (never `5435|5432` — matches 54323); command lines via `psutil`
  from `~/.workbuddy-ai/binaries/python/envs/default`.
- asyncio: `asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)`; `uvicorn.run(loop=...)` takes a
  **string**. Long jobs need `run_in_background: true` (`nohup … &` is reaped when the call returns); a script
  piped into `head`/`tail` dies of SIGPIPE — write to a file.

### RLS (full)

- **Bootstrap pattern (8 instances):** a tenant table read *before* a binding exists needs a narrow
  `SECURITY DEFINER` function — pinned `SET search_path = pg_catalog, public`, `REVOKE ALL FROM PUBLIC` and
  from `platform`, `GRANT EXECUTE` to `platform_app` only. Never `USING (app.tenant_id IS NULL OR ...)`.
- **RLS fails silently on writes**: unbound `SELECT` → 0 rows; `UPDATE`/`DELETE` → rowcount 0. Assert
  `rowcount` on every app-role write. Cross-tenant queue scans are global by design — **never leave probe
  rows**.
- **Policy is `tenant_id IS NULL OR tenant_id = app.tenant_id()`** → a NULL-tenant row is *global reference
  data*. Isolation tests must assert `WHERE tenant_id IS NOT NULL`; counting all rows fails on fixture
  leftovers and reports hygiene as a breach.
- Teardown: children before parents, else you poison the next run. **Fails solo = dirty DB; passes solo =
  attribute further.**

### Async, sessions, agent runs (full)

- `set_config('app.tenant_id', …, true)` is **transaction-scoped**; `tenant_session(ctx)` rebinds on
  `after_begin`. **A failed flush poisons the session** → `await session.rollback()` before raising; prefer
  `ON CONFLICT DO NOTHING`. **The outbox relay's caller owns the transaction** — `run_once(session)` does not
  commit.
- **Two `AgentRun` creation sites:** `router.py:159` writes a *placeholder* (`status=queued`, `input_hash=""`),
  `orchestrator.py:674` the *real* run. Nothing advances a `queued` row → "stuck queued" = orphan placeholder;
  tell them apart by `input_hash=''`.
- **Query a run by its exact `run_id`** — never by `conversation_ref` (parallel sessions leave other people's
  rows; this produced one completely wrong conclusion). Worker log `event_processed.status` beats table
  inspection. `AgentRun` stores **no answer text** (only `output_hash`); body in `outbox_events.payload`.
- **Do not replace `deps.reader`'s identity** (tried, reverted `e9d8a3e`) — it breaks
  `deps.sender is deps.reader`. Put local-first inside `fetch_message` / the consumer.

### Conventions (full)

- Conventional-commit subject + body explaining **why**; state test-count delta + ruff/mypy.
- `alembic.ini` at `apps/api/migrations/`; non-ASCII path ⇒ `cfg.set_main_option("script_location", …)` and
  **do not chdir** (else `.env` not found). `APP_ALLOW_BOOTSTRAP_TOKENS=true` locally. Bootstrap tokens are
  **unsigned**, so `APP_ENVIRONMENT` must be *explicitly* declared (S-1: `model_fields_set`, not a `"local"`
  default) — the guard is only as good as the flag.
- **A test file outside `testpaths` never runs.** **Never issue parallel edits to one file.**
- SAML login issues **no session**; first SAML login **never provisions a role**; SCIM Group → Department,
  never `MembershipRole`; `infra/kubernetes/` is never applied to a cluster.

### Rationale behind the `Scene.*` rule

`_SCENE_PATTERNS` is for retrieval breadth and tool affinity, not routing: its COMPLAINT entry counts
"still not"/"third time", so `my order has still not arrived` classifies as complaint while the report's own
complaint example classifies as `technical_support` (see `complaint.py`).

### Environment details moved out of MEMORY.md

- **GitHub** `git@github.com:kayon0209/b2b-ai-support-platform`, SSH key `~/.ssh/id_ed25519`.
- **Chatwoot admin password is set interactively on first boot** and is deliberately **not** recorded in any
  tracked file — a credential in a tracked file is readable by anyone with repo access.
- **Lost refs/objects recovery** (happened 2026-09-20): last sha from `.git/logs/refs/heads/master` →
  `git fetch origin` → `git update-ref refs/heads/master origin/master` → `rm .git/index && git reset --mixed
  HEAD` → `git fsck`. **None of these touches the working tree.**
- **Process inspection:** the PowerShell tool is silently sandboxed and `wmic` is blacklisted. Use `tasklist`
  (PID/name only) + `netstat -ano | grep ":5435"` (never `5435|5432` — that matches 54323); command lines via
  `psutil` from `~/.workbuddy-ai/binaries/python/envs/default`.
- asyncio: `asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)`; `uvicorn.run(loop=...)` takes a
  **string**. Long jobs need `run_in_background: true` (`nohup … &` is reaped when the call returns); a script
  piped into `head`/`tail` dies of SIGPIPE — write to a file and read it.

### The two runtime guards — verified invocation (2026-09-21)

Both need live services; neither is a pytest test, and both are mutation-tested.

```bash
# 1. API on a port nobody holds (8010), then Vite pointed at it
PYTHONPATH="<absolute ;-joined>" APP_API_PORT=8010 ./.venv/Scripts/python.exe -m platform_core.main
cd apps/admin-web && VITE_API_TARGET=http://localhost:8010 VITE_API_TOKEN=pt_admin-demo_<user_id> \
  node node_modules/vite/bin/vite.js --port 5174 --strictPort

# 2. concurrency (needs API + admin DB)
APP_BASE_URL=http://127.0.0.1:8010 APP_TOKEN=pt_admin-demo_<user_id> \
APP_ADMIN_DATABASE_URL=postgresql://platform:platform@localhost:5435/platform \
./.venv/Scripts/python.exe scripts/concurrency_probe.py

# 3. UI wiring (needs Vite)
NODE_PATH="C:/Users/Rose/.workbuddy-ai/binaries/node/workspace/node_modules" \
APP_BASE_URL=http://localhost:5174 APP_TOKEN=pt_admin-demo_<user_id> node scripts/ui_smoke.cjs
```

- **`ui_smoke.cjs` fails with `Cannot find module 'playwright-core'` unless `NODE_PATH` points at the managed
  node workspace** — `playwright-core` is not installed in the repo. `NODE_PATH` works here because the script
  is CJS; ESM ignores it (see the browser-testing section above).
- Use `localhost:5174`, not `127.0.0.1` — Vite binds IPv6 `[::1]` only.
- Pass `postgresql://` (not `postgresql+psycopg://`) to `APP_ADMIN_DATABASE_URL`; the probe calls
  `psycopg.connect` directly.
- Token: `SELECT t.slug, m.user_id FROM memberships m JOIN tenants t ON t.id=m.tenant_id WHERE m.status='active'`.
- Last measured: probe **5/5**, ui_smoke **10/10** (`/workbench served=23 rendered=23` — that is the F-5 fix).
- **A full-suite run that makes no progress is usually a dead container, not a slow suite.** Check CPU time
  first (a hung run showed 5.6 s of CPU after 20 min), then probe 5435/6380. After restarting only the data
  services the same suite finished in 2 min 13 s. Start **only the data services** — never the workers.
- MinIO cannot be started while Docker has no HTTPS proxy (registry-1.docker.io unreachable), but **no test
  needs it** — its absence caused zero failures.
