# Project Memory — B2B AI Customer Support Platform

Durable rules only. Narrative → daily logs (`YYYY-MM-DD.md`).
Long-tail detail → `REFERENCE.md` (same dir, **not** auto-injected).
Handover of record → `HANDOVER-2026-09-20.md` (repo root).
Consolidated 2026-09-20 (v7).

## Orientation

An **AI control plane**, not a chat product. Customer → **Chatwoot** (external bounded
context) → signed webhook → Support Bridge → this repo; return leg `AI --REST--> Chatwoot`.
This repo = `apps/admin-web` + `/v1/*` admin APIs + one webhook entry.

## Verification (run all of it; partial runs lie)

```bash
./.venv/Scripts/python.exe -m ruff check apps packages scripts tests pytest_plugins_release
./.venv/Scripts/python.exe -m ruff format --check apps packages scripts tests pytest_plugins_release
./.venv/Scripts/python.exe -m mypy
mv tests/artifacts/release_gate_evidence.json /tmp/evidence.bak 2>/dev/null   # sandbox guard
./.venv/Scripts/python.exe -m pytest --junitxml=tests/artifacts/junit-final.xml
./.venv/Scripts/python.exe -m platform_core.evaluation.release_check --evidence-only
```

- **`ruff check` alone ≠ CI green.** CI runs both check *and* `format --check`.
- pytest summary goes to **stderr**; use `--junitxml` for authoritative counts.
- **The full suite must be the last pytest invocation.** The gate plugin writes evidence
  unconditionally, so any targeted run overwrites it → `release_check` exit 2. It also
  `unlink()`s the old file at `pytest_configure`, which can trip the sandbox batch-delete
  guard → move the file away first. **Do not "fix" that plugin; it is correct.**
- **A red full-suite run is not evidence.** Six runs of identical code gave 3/0/2/0/10/2
  failures in different files, each green solo (fixture leftovers cascading `IntegrityError`;
  tmp-dir GC). **Re-run the failing test alone before believing it.**
- **Stop every consumer before testing — host processes too.** Found 2026-09-20: four orphan
  `worker.runner` host processes (~30 h old) silently claiming the outbox *and* ingestion
  queues, causing every flaky test that day (full suite 1558 pass / `test_outbox_relay` 8
  skipped → 8 passed once killed). **An agent stopping its shell does not stop its python
  child — kill the tree.** Probe don't guess: insert a `queued` outbox row, re-read later;
  climbing `attempts` = consumer live. `docker ps -a` all-Exited proves nothing.
- **"No consumer running" needs a ≥ one-full-run wait (~40 s)** — a worker commits only at
  the end, so an event looks `received` while being processed.

## Environment

- Repo `D:/360Downloads/360驱动大师目录/b2b-ai-support-plan/b2b-ai-support-plan`.
- Python `.venv/Scripts/python.exe` (3.12). Managed 3.13 has **no pytest**.
- **PYTHONPATH absolute and `;`-joined** (else `No module named 'platform_core'`):
  `$R/apps/api/src;$R/packages/contracts/src;$R/packages/policy/src;$R/packages/observability/src;$R/apps/worker/src`.
  Git Bash: `cygpath -w "$(pwd)"` — Python-on-Windows can't resolve POSIX `$(pwd)`.
- Ports: ai-pg 5435, chatwoot-pg 5434, ai-redis 6380, chatwoot-redis 6381, Chatwoot 3000,
  API 8000 (dev 8010), Keycloak 8081, MinIO 9000/9001, Vite 5173/5174.
- Roles: `platform` (superuser, bypasses RLS — seed/cleanup), `platform_app` (NOBYPASSRLS).
- LLM: Gitee AI `https://ai.gitee.com/v1`; creds in `.env`. Account 3, inbox 2; webhook must
  use `host.docker.internal` (`localhost` hits the container itself). Chatwoot admin password
  is set interactively on first boot — deliberately **not** recorded here (a credential in a
  tracked file is readable by anyone with repo access).
- GitHub: `git@github.com:kayon0209/b2b-ai-support-platform`, SSH `~/.ssh/id_ed25519`.
- **Two sessions share this working tree.** Never `reset --hard`, `checkout --` or `clean -fd`.
  Don't `git add` a file carrying the other session's uncommitted changes — that publishes
  their half-finished work. Leave it and say so in the commit message.
- **Do not delete `.workbuddy-ai/`** — it is project data, not cache.
- **Recovery from lost refs/objects** (happened 2026-09-20): last sha from
  `.git/logs/refs/heads/master`, `git fetch origin`, `git update-ref refs/heads/master
  origin/master`, `rm .git/index && git reset --mixed HEAD`, `git fsck`. None touches the
  working tree.

## Windows traps

- **API must be `python -m platform_core.main`, never bare uvicorn** (ProactorEventLoop →
  psycopg async refuses → every DB request dies at connect → bare `401 AUTH_UNRESOLVED`).
- **`curl --noproxy "*"`** — a host proxy (`:55940`) answers `localhost` with 502, which reads
  as a broken route. Write bodies with `-o`, not a pipe (exit 23 / empty body).
- **Port ghosts:** an old process on `127.0.0.1:PORT` beats your new `0.0.0.0:PORT` instance;
  loopback hits old code → 404/405 that look like unregistered routes. Judge by
  `netstat -ano` bind address + PID; `taskkill` may not work. Change port instead.
- **Long runs exhaust sockets** (~20 h): `WinError 10055` → `/healthz` 502 or silent exit.
  Not a code bug. **Kill services you started before ending a session** (a leaked API+Vite
  ran 29 h → `fork: Cannot allocate memory`).
- **`docker` CLI can hang entirely** while containers still run — connect with `psycopg`
  directly rather than debugging the CLI.
- **Process inspection:** PowerShell tool is silently sandboxed; `wmic` blacklisted. Use
  `tasklist` (PID/name only) + `netstat -ano | grep ":5435"` (never `5435|5432` — matches
  54323). Command lines via `psutil` from `~/.workbuddy-ai/binaries/python/envs/default`.
- asyncio scripts need `asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)`;
  `uvicorn.run(loop=...)` takes a **string**.

## The recurring defect: a capability with no consumer

Every audit finds a value/column/function that is tested but that no production path reads
(`AgentRun.started_at`, `token_usage`, `credential_ref`, the reranker, `drain_outbox_once`,
`Route.BUSINESS_WRITE`, `CaseCategory.QUALITY_COMPLAINT`…). **For anything a reader depends
on, grep its writers/callers** — a test calling a function directly hides a missing
production caller. **Grep before adding an enum value.** Build the consumer with the
capability, or don't build it.
**Mirror rule: when "this is easier" argues against a documented design, the design wins**
(`case.eq_confirm` is unreachable *by the agent* on purpose; I once relaxed a required
`HUMAN_APPROVAL` for agent reachability and had to redo the whole thing — *reachability is a
convenience argument, not a safety argument*).
**A tool's risk class lives in `tool_definitions` and `ensure_tool_definitions` only ADDS** —
a class change without a data migration is a comment, not a control. Bump
`EXPECTED_MIGRATIONS` with every migration.

## Release gates

`@pytest.mark.zero_tolerance("<invariant>")` → `pytest_plugins_release/gate_evidence.py`
writes `tests/artifacts/release_gate_evidence.json` → `evaluation/evidence.py` is the only
reader. Partial run (<500 tests) refused; `release_check` exits 0/1/2 (pass / gate failed /
inputs unusable). `scripts/run_eval.py` is the only producer of `eval_report.json`.
A concurrent test run (the parallel session) wipes the evidence → run full suite alone and
read the gate immediately after.

## Correctness rules that keep being violated

- **Errors must be real 4xx.** FastAPI renders a returned **dict** as 200 — always return
  `error_response()` / `domain_error_response()` (a `JSONResponse`). **Never swallow an
  exception: catch ⇒ log.** "UI says done but nothing changed" → `curl -i` and compare.
- **A guard you cannot observe failing is not evidence** → give every guard a **mutation
  test**. But **an over-broad guard is worse than none** (a tenant-id guard with 14 false
  positives means the premise was wrong).
  *A mutation test can pass for the wrong reason*: one guard (procedure veto) stayed green
  under mutation because a *second* guard (trailing `?`) covered the same case, so the veto
  was never actually exercised. Give each guard a case **only it** can decide.
- **Never gate behaviour on `Scene.*` without measuring the pattern list.** `_SCENE_PATTERNS`
  is for retrieval breadth and tool affinity, not routing: its COMPLAINT entry counts "still
  not"/"third time", so `my order has still not arrived` classifies as complaint while the
  report's own complaint example classifies as `technical_support`. (See `complaint.py`.)
- **A red line is not a feature flag.** The EQ handoff adds opt-in behaviour, so it is
  flag-gated; removing an answer the research report classes as a red line is not opt-in.
  Gating a red line behind a default-off flag is how the complaint path came to answer.
- **Measure before and bidirectionally after changing a heuristic**; "nothing moved" on
  patterns absent from the corpus is weak evidence.
- **Don't trust memory or stale docs — measure.** Twice misled in one session (thought
  phase 3 unstarted; picked a task from an outdated cross-review list).
- Delete a design's dependent parameters too — no "might use later" dead code.
- Record deliberate scope boundaries ("record, don't smuggle") instead of silently widening.

## RLS

- **Bootstrap pattern (8 instances):** a tenant table read *before* a binding exists needs a
  narrow `SECURITY DEFINER` function — pinned `SET search_path = pg_catalog, public`,
  `REVOKE ALL FROM PUBLIC` and from `platform`, `GRANT EXECUTE` to `platform_app` only,
  `RETURNS TABLE (...)`. Never loosen to `USING (app.tenant_id IS NULL OR ...)`.
- **RLS fails silently on writes**: unbound `SELECT` → 0 rows; `UPDATE`/`DELETE` → rowcount
  0. Assert `rowcount` on every app-role write.
- Cross-tenant queue scans are global by design — **never leave probe rows**.
- **The isolation policy is `tenant_id IS NULL OR tenant_id = app.tenant_id()`**, so a NULL
  tenant row is *global reference data* by that policy. Isolation tests must assert on rows
  `WHERE tenant_id IS NOT NULL` — counting all rows asserts "no global data exists" and fails
  on fixture leftovers, reporting hygiene as a breach.
- Fixture teardown: delete children before parents, else you poison the next run.
  **Fails solo = dirty DB; passes solo = attribute further.**

## Async and sessions

- `set_config('app.tenant_id', …, true)` is **transaction-scoped**; `tenant_session(ctx)`
  rebinds on `after_begin`. Every tenant-scoped handler uses it.
- **A failed flush poisons the session** → `await session.rollback()` before raising. Prefer
  `ON CONFLICT DO NOTHING`.
- **The outbox relay's caller owns the transaction**: `run_once(session)` does not commit.

## Agent runs

- **Two creation sites.** `router.py:159` writes a *placeholder* (`status=queued`,
  `input_hash=""`); `orchestrator.py:674` writes the *real* run. Nothing advances a `queued`
  row, so "stuck queued" = orphan placeholder; tell them apart by `input_hash=''`.
- **Always query a run by its exact `run_id`** — never by `conversation_ref` (parallel
  sessions leave other people's rows; this produced one completely wrong conclusion). Worker
  log `event_processed.status` is faster and more reliable than table inspection.
- `AgentRun` stores **no answer text** (only `output_hash`); body in `outbox_events.payload`.
- **Do not replace `deps.reader`'s identity** (tried, reverted `e9d8a3e`): it breaks
  `deps.sender is deps.reader`. Put local-first inside `fetch_message` / the consumer.

## Conventions

- Conventional-commit subject + body explaining **why**; state test-count delta + ruff/mypy.
- `alembic.ini` at `apps/api/migrations/`; non-ASCII path ⇒ `cfg.set_main_option(
  "script_location", …)` and **do not chdir** (else `.env` not found). Set
  `APP_ALLOW_BOOTSTRAP_TOKENS=true` locally.
- **A test file outside `testpaths` never runs.** **Never issue parallel edits to one file.**
- SAML login issues **no session**; first SAML login **never provisions a role**; SCIM Group
  → Department, never `MembershipRole`; `infra/kubernetes/` never applied to a cluster.

## See also

`REFERENCE.md` — walkthrough, browser acceptance, migration/fixture gotchas, auth
middleware, Admin UI, heuristic design. `HANDOVER-2026-09-19.md` — the other session's line
(customer chat UI), still valid.
