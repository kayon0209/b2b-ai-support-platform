# Project Memory — B2B AI Customer Support Platform

Durable rules only. Narrative → daily logs (`YYYY-MM-DD.md`).
Long-tail detail → `REFERENCE.md` (same dir, **not** auto-injected).
Trimmed 2026-09-19 (v6).

## Orientation

An **AI control plane**, not a chat product. Customer → **Chatwoot** (external bounded
context) → signed webhook → Support Bridge → this repo; return leg `AI --REST--> Chatwoot`.
This repo = `apps/admin-web` + `/v1/*` admin APIs + one webhook entry.

## Environment

- Repo `D:/360Downloads/360驱动大师目录/b2b-ai-support-plan/b2b-ai-support-plan`.
- Python `.venv/Scripts/python.exe` (3.12). Managed 3.13 has **no pytest**.
- **PYTHONPATH absolute and `;`-joined** (else `No module named 'platform_core'`):
  `$R/apps/api/src;$R/packages/contracts/src;$R/packages/policy/src;$R/packages/observability/src;$R/apps/worker/src`.
  Git Bash: `cygpath -w "$(pwd)"`; Python-on-Windows can't resolve POSIX `$(pwd)`.
- Ports: ai-pg 5435, chatwoot-pg 5434, ai-redis 6380, chatwoot-redis 6381, Chatwoot 3000,
  API 8000 (dev 8010), Keycloak 8081, MinIO 9000/9001, Vite 5173/5174.
- Roles: `platform` (superuser, bypasses RLS — seed/cleanup), `platform_app` (NOBYPASSRLS).
  `app_role_url()` = database_url with `platform:platform@` → `platform_app:platform_app@`.
- LLM: Gitee AI `https://ai.gitee.com/v1`; creds in `.env`. `packages/observability/src/observability.py`
  is top-level `observability`. Chatwoot `admin@example.com`/`Admin@123456`, account 3, inbox 2;
  webhook must use `host.docker.internal` — `localhost` hits the container itself.
- GitHub: `git@github.com:kayon0209/b2b-ai-support-platform`, SSH key `~/.ssh/id_ed25519`.
- **Two sessions share this working tree.** Never `reset --hard`, `checkout --` or
  `clean -fd`: they destroy the other session's uncommitted work. When a file carries
  their uncommitted changes, do not `git add` it either — that publishes their
  half-finished work. Leave the change in the tree and say so in the commit message.
- **Recovery from lost refs/objects** (happened 2026-09-20: `.git/refs/heads/` deleted
  and post-pack loose objects gone, surfacing as `invalid object ...` then "your current
  branch does not have any commits yet"): get the last sha from
  `.git/logs/refs/heads/master`, `git fetch origin` (the remote has everything),
  `git update-ref refs/heads/master origin/master`, `rm .git/index && git reset --mixed
  HEAD`, then `git fsck`. None of that touches the working tree.

## Verification

```bash
./.venv/Scripts/python.exe -m pytest                    # full suite
./.venv/Scripts/python.exe -m ruff check apps packages scripts tests pytest_plugins_release
./.venv/Scripts/python.exe -m ruff format --check <same>
./.venv/Scripts/python.exe -m mypy
./.venv/Scripts/python.exe -m platform_core.evaluation.release_check --evidence-only
```

- pytest summary goes to **stderr**; after a pipe use `${PIPESTATUS[0]}`.
- **Stop every consumer before tests — host processes too.** A relay claims the global
  queues. `docker ps -a` all-Exited proves nothing. **Probe, don't guess**: insert one
  `queued` outbox row, re-read seconds later; climbing `attempts` = a consumer is live.
  **Found 2026-09-20: four orphan `worker.runner` host processes** left by earlier agent
  sessions, running ~30 h and silently claiming the outbox *and* ingestion queues. They
  caused *every* flaky test seen that day: with them stopped the full suite went
  `PYTEST_EXIT=0` (1558 tests, `release_check` exit 0) and `test_outbox_relay.py` went from
  8 skipped to 8 passed. **An agent that stops its shell does not stop the python child** —
  kill the process tree. To find them: `netstat -ano | grep ":5435"` (never `5435|5432`,
  which matches 54323) to list DB-connected PIDs — a python process holding DB connections
  and listening on no port is worker-shaped — then read command lines with `psutil` from the
  isolated venv `~/.workbuddy-ai/binaries/python/envs/default` (PowerShell is sandboxed
  here, `wmic` blacklisted, `tasklist /V` carries no command lines).
- **A red full-suite run is not evidence** — six runs of identical code gave 3/0/2/0/10/2
  failures in different files, each green solo. Causes: leftover fixture rows cascading
  `IntegrityError` into the *next* run; pytest tmp-dir GC tripping the sandbox safe-delete
  guard (>50 items). **Re-run the failing test alone first.**
- **"No consumer running" needs a ≥ one-full-run wait (~40 s).** A worker holds one
  transaction and commits only at the end, so an event looks `received` for tens of
  seconds *while being processed*. A 12 s check gave a wrong conclusion here.
- **Inspecting host processes:** PowerShell tool is silently sandboxed (exit 0, no output);
  `wmic` blacklisted. Use `tasklist` (PID/name only) + `netstat -ano`, which does **not**
  show docker-bridge connections, so DB `client_port` → PID mapping is unavailable.
- **A truncated grep manufactures false conclusions.** Grep the exact name before
  asserting absence.

## Release gates

`@pytest.mark.zero_tolerance("<invariant>")` → `pytest_plugins_release/gate_evidence.py`
writes `tests/artifacts/release_gate_evidence.json` → `evaluation/evidence.py` is the only
reader. Partial run (<500 tests) refused; `release_check` exits 0/1/2 (pass/gate failed/
inputs unusable). `scripts/run_eval.py` is the only producer of `eval_report.json`.
**The plugin writes unconditionally**, so any targeted run leaves partial evidence — the
**full suite must be the last pytest invocation** before trusting `release_check`.
It also `unlink()`s the old file in `pytest_configure` and only writes in
`pytest_sessionfinish`, so **a concurrent test run (the parallel session) wipes the
evidence** and `release_check` exits 2 with "evidence unusable". Run the full suite when
nobody else is testing and read the gate immediately after.

## The recurring defect: a capability with no consumer

Every audit finds a value/column/function that is tested but that no production path reads
(`AgentRun.started_at`, `token_usage`, `credential_ref`, the reranker, `drain_outbox_once`,
`Route.BUSINESS_WRITE`, …). **For any field a reader depends on, grep its writers/callers** —
a test calling a function directly hides a missing production caller.
**Mirror: when "this is easier" argues against a documented design, the design wins**
(`case.eq_confirm` is unreachable *by the agent* on purpose).
**A tool's risk class lives in `tool_definitions`, and `ensure_tool_definitions` only ADDS**
— a class change without a data migration is a comment, not a control. Bump
`EXPECTED_MIGRATIONS` with every migration.

## Errors must be real 4xx, never 200-with-error-body

FastAPI renders a returned **dict** as 200. Always return `error_response()` /
`domain_error_response()` (a `JSONResponse`). **Never swallow an exception — catch ⇒ log.**
Symptom to trust: UI says "done" but nothing changed → `curl -i` and compare.

## RLS

- **Bootstrap pattern (8 instances):** a tenant table read *before* a binding exists needs a
  narrow `SECURITY DEFINER` function — pinned `SET search_path = pg_catalog, public`,
  `REVOKE ALL FROM PUBLIC` and from `platform`, `GRANT EXECUTE` to `platform_app` only,
  `RETURNS TABLE (...)`. Never loosen to `USING (app.tenant_id IS NULL OR ...)`.
- **RLS fails silently on writes**: unbound `SELECT` → 0 rows; `UPDATE`/`DELETE` → rowcount
  0. Assert `rowcount` on every app-role write. `FOR UPDATE` ⇒ `VOLATILE`.
- Cross-tenant queue scans are global by design — **never leave probe rows**.
- **The isolation policy is `tenant_id IS NULL OR tenant_id = app.tenant_id()`**, so a
  NULL-tenant row is *global reference data* by that policy's own definition. An isolation
  test must therefore assert on rows `WHERE tenant_id IS NOT NULL` — counting all rows
  asserts "no global data exists", and then fails on fixture leftovers, reporting a hygiene
  problem as a security breach. Verify a production path cannot create the NULL rows before
  calling them junk (e.g. `ensure_tool_definitions` is the only insert into
  `tool_definitions` and always sets `tenant_id`).

## Async and sessions

- `set_config('app.tenant_id', ..., true)` is **transaction-scoped**. `tenant_session(ctx)`
  rebinds on `after_begin`; every tenant-scoped handler must use it.
- **A failed flush poisons the session** — `await session.rollback()` before raising, or the
  router's `commit()` raises `PendingRollbackError`. Prefer `ON CONFLICT DO NOTHING`.
- **The outbox relay's caller owns the transaction**: `run_once(session)` does not commit.

## Windows: asyncio loops

**Never launch the API with bare uvicorn** — always `python -m platform_core.main`. uvicorn
hardcodes `ProactorEventLoop` before importing the app; psycopg async refuses it → every DB
request dies at connect, surfacing as a bare `401 AUTH_UNRESOLVED`. Scripts need
`asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)`; `uvicorn.run(loop=...)` takes a
**string**. Same fix in `apps/worker/src/worker/runner.py`.

## Conventions

- Conventional-commit subject + body explaining **why**; state test-count delta + ruff/mypy.
  Read `AGENTS.md` before touching module boundaries.
- alembic.ini at `apps/api/migrations/`; non-ASCII path ⇒ `cfg.set_main_option("script_location", …)`
  and **do not chdir** (else `.env` not found).
- **A test file outside `testpaths` never runs.** **Never issue parallel edits to one file.**
- SAML login issues **no session**; a first SAML login **never provisions a role**; SCIM Group
  → Department, never `MembershipRole`; `infra/kubernetes/` never applied to a cluster.

## Agent runs: two rows, and why platform answers vanished

- **Two creation sites.** `router.py:159` (trigger endpoint) writes a *placeholder*:
  `status=queued`, `input_hash=""`, `code_version="0.1.0"`, `conversation_ref_id` = raw path
  UUID. `orchestrator.py:674` writes the *real* run: `status=RUNNING`, populated
  `input_hash`, uuid5-derived ref. Only `RUNNING`/`COMPLETED`/`FAILED` are ever assigned —
  **nothing advances a `queued` row**, so "run stuck queued" is an orphan placeholder.
  Tell them apart by `input_hash=''`. The chat UI polls the *timeline*, not the run.
- **Platform-native question → no answer (2026-09-19, fixed and verified end-to-end).** No
  Chatwoot target ⇒ `_dispatch` returns `OUTBOUND_TARGET_MISSING` ⇒ run FAILED, while
  `_persist_memory` only writes an agent turn for `completed`. Answer generated, never
  persisted, timeline empty. Fixed in `inbox_consumer._persist_memory` for
  `failed + OUTBOUND_TARGET_MISSING + answer_text`; **never** for `OUTBOUND_FAILED`/
  `OUTBOUND_AMBIGUOUS`. Root fix belongs in `_dispatch` (no external channel ≠ failure).
- **One conversation, one identity:** `conversation_ref_for()` in
  `platform_core/support_bridge/conversation_ref.py` is the *only* definition of the
  uuid5 derivation. The worker, the customer endpoint and the run trigger/list all call it.
  The formula is frozen (pinned by a test against a live value) — changing it orphans every
  stored turn/run/lease, so it is a data migration. Asserting call-site agreement by `is`
  identity, not by equal output: a copy that agrees today can still drift.
- `AgentRun` stores **no answer text** (only `output_hash`); body in `outbox_events.payload`,
  citations in `Citation.source_uri`.
- **Do not replace `deps.reader`'s identity** (tried, reverted `e9d8a3e`): a wrapper broke
  `deps.sender is deps.reader` and `isinstance(deps.sender, ChatwootClient)`. Put local-first
  inside `fetch_message` or in the consumer.

## Parallel session (2026-09-19)

58 uncommitted files touching `tool_gateway/*`, `orchestrator.py`, `policy/engine.py`,
`agent_runtime/{intent,qa_path}.py`; its migration 0038 chains to my 0037. Its tree had 4
mypy errors. **Don't touch its files; don't break the migration chain.** An unknown
consumer was also competing for the queue (see daily log).

## See also

`REFERENCE.md` — walkthrough, Windows browser testing, migration/fixture gotchas, auth
middleware, Admin UI, heuristic design, 2026-09-18 audit, folded verbose notes.
`HANDOVER-2026-09-19.md` (repo root) — start here if asked where the last session stopped.

## 【更正】闭环不通的真实原因：run 是 failed，不是 queued

上一轮我记录"event completed 但 run 卡 queued"——**错了**。收工后读 worker 日志
才看清：`event_processed` 里 `status="failed"`，按 run_id 精确查 `agent_runs` 三个
都是 `failed`（abstain_reason 空 = 不是弃权；latency 9.2s / 50s / 6.6s）。
之前看到的 `queued` 是**别的会话的 run**——查状态时我用了 conversation_ref 匹配，
混进了别人的行。

**教训（很值钱）**：排查"为什么没结果"时，
**永远用返回的 run_id 精确查**，不要用 conversation_ref 去捞——多会话并行时
库里同时有别人的 run，会得出完全错误的结论。
**先看 worker 日志里的 event_processed.status**，比查表快且准。

方向：LLM/embedding 调用超时或异常（50s 时延典型）。已更正进 HANDOVER 文档。
