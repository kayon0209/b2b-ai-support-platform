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
