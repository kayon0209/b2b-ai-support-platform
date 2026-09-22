# Project Memory — B2B AI Customer Support Platform

Rules only. Narrative → `YYYY-MM-DD.md`. Full detail + rationale → `REFERENCE.md` (**not** auto-injected).
Handover of record → `HANDOVER-CONTINUE-2026-09-21.md`. Consolidated 2026-09-21 (v8).

## Orientation

**An AI control plane, not a chat product.** Customer → **Chatwoot** → signed webhook → Support Bridge →
this repo; return leg `AI --REST--> Chatwoot`. Repo = `apps/admin-web` + `/v1/*` + one webhook entry; it owns
Tenant/Case/Knowledge/AgentRun/ToolExecution/Citation/Evaluation/AuditEvent, **not** conversation content.
**`/chat` is an internal verification surface, not a customer channel** (ADR 0010): `conversation_ref` from the
signed webhook is the only customer-identity handle, and an unauthenticated send path = "anyone can spend
model money".

## Verification — run all of it; partial runs lie

```bash
./.venv/Scripts/python.exe -m ruff check apps packages scripts tests pytest_plugins_release
./.venv/Scripts/python.exe -m ruff format --check apps packages scripts tests pytest_plugins_release
./.venv/Scripts/python.exe -m mypy
mv tests/artifacts/release_gate_evidence.json /tmp/evidence.bak 2>/dev/null   # sandbox guard
./.venv/Scripts/python.exe -m pytest --junitxml=tests/artifacts/junit-final.xml
./.venv/Scripts/python.exe -m platform_core.evaluation.release_check --evidence-only
```

- `ruff check` alone ≠ CI green (CI also runs `format --check`). pytest summary → **stderr**; trust `--junitxml`.
- **The full suite must be the LAST pytest invocation** — the gate plugin writes evidence unconditionally, so a
  targeted run overwrites it → `release_check` exit 2; it `unlink()`s the old file at `pytest_configure`,
  tripping the sandbox batch-delete guard. **Do not "fix" that plugin; it is correct.**
- **A red full-suite run is not evidence** (6 identical runs → 3/0/2/0/10/2 failures in different files, each
  green solo). **Re-run the failing test alone before believing it.**
- **Green tests ≠ working UI** (1741 green while 6 wiring/failure-path bugs lived). UI → `scripts/ui_smoke.cjs`;
  concurrency → `scripts/concurrency_probe.py` against a **real service** (`TestClient` shares one portal and
  can pass on unfixed code). Both are mutation-tested; keep them so.
- **Stop every consumer before testing — host processes too.** Orphan `worker.runner` processes silently claimed
  the outbox *and* ingestion queues for a day of flaky tests. **Stopping your shell does not stop your python
  child — kill the tree.** Probe, don't guess: insert a `queued` outbox row and re-read; climbing `attempts` =
  consumer live. `docker ps -a` all-Exited proves nothing; wait ≥ one full run (~40 s) before concluding "no
  consumer".
- Gates: `zero_tolerance` marks → `release_gate_evidence.json`, whose only reader is `evaluation/evidence.py`;
  partial runs (<500 tests) refused; `release_check` exits 0/1/2; `run_eval.py` is the only `eval_report.json`
  producer. A concurrent run wipes the evidence.

## Environment

- Repo `D:/360Downloads/360驱动大师目录/b2b-ai-support-plan/b2b-ai-support-plan`; `.venv/Scripts/python.exe`
  (3.12) — managed 3.13 has **no pytest**.
- **PYTHONPATH absolute, `;`-joined** (else `No module named 'platform_core'`):
  `$R/apps/api/src;$R/packages/contracts/src;$R/packages/policy/src;$R/packages/observability/src;$R/apps/worker/src`.
  Git Bash: `cygpath -w "$(pwd)"`.
- Ports: ai-pg 5435 · chatwoot-pg 5434 · ai-redis 6380 · chatwoot-redis 6381 · Chatwoot 3000 · API 8000 (dev
  8010) · Keycloak 8081 · MinIO 9000/9001 · Vite 5173/5174 (**IPv6 `[::1]` — use `localhost`**).
- Roles: `platform` (superuser, bypasses RLS — seed/cleanup), `platform_app` (NOBYPASSRLS). LLM Gitee AI
  `https://ai.gitee.com/v1` (creds in `.env`); webhook must use `host.docker.internal`. Bootstrap tokens
  `pt_<tenant-slug>_<user-id>` via `memberships JOIN tenants`.
- **Two sessions share this working tree.** Never `reset --hard`, `checkout --`, `clean -fd`. Don't `git add` a
  file carrying the other session's uncommitted changes — that publishes their half-finished work; leave it and
  say so in the commit message. **Read the whole diff for sections that aren't yours** (that is how another
  session's doc section landed in `1924f4b`). **Do not delete `.workbuddy-ai/`** — project data, not cache.
  Lost-refs recovery, GitHub/SSH, Chatwoot creds → REFERENCE.

## Windows traps (full list → REFERENCE)

- **API must be `python -m platform_core.main`, never bare uvicorn** (ProactorEventLoop → psycopg async
  refuses → every DB request dies at connect → bare `401 AUTH_UNRESOLVED`).
- **`curl --noproxy "*"`** (a host proxy 502s `localhost`); write bodies with `-o`, not a pipe (exit 23).
- **Port ghosts:** an old `127.0.0.1:PORT` beats your new `0.0.0.0:PORT` → loopback reaches old code → 404/405
  that look like unregistered routes. Judge by `netstat -ano` bind address + PID; change port.
- **Long runs exhaust sockets** (~20 h: `WinError 10055`); **`docker` CLI can hang while containers run** (use
  `psycopg`). **Kill services you started before ending a session.**

## The recurring defect: a capability with no consumer

Every audit finds a value/column/function that is tested but that no production path reads (full list →
REFERENCE). **For anything a reader depends on, grep its writers/callers** — a test calling a function directly
hides a missing production caller. **Grep before adding an enum value.** Build the consumer with the
capability, or don't build it.
**When "this is easier" argues against a documented design, the design wins** (`case.eq_confirm` is unreachable
*by the agent* on purpose — *reachability is a convenience argument, not a safety argument*).
**A tool's risk class lives in `tool_definitions`; `ensure_tool_definitions` only ADDS** — a class change
without a data migration is a comment, not a control.
**Measure the tracked set, not the disk.** `EXPECTED_MIGRATIONS = 43` has been wrong since `a04d1ee` — counted
from the working tree, which held another session's *untracked* `0040_case_attachments.py`, while HEAD tracks
**42** → every clean checkout fails `test_migration_and_performance`. **Bump it per migration, counting
`git ls-tree`, not `ls`.**

## Correctness rules that keep being violated

- **Errors must be real 4xx.** FastAPI renders a returned **dict** as 200 — always return `error_response()` /
  `domain_error_response()`. **Never swallow an exception: catch ⇒ log.** "UI says done but nothing changed" →
  `curl -i` and compare.
- **A guard you cannot observe failing is not evidence** → mutation-test every guard; **an over-broad guard is
  worse than none**; *a mutation test can pass for the wrong reason* — give each guard a case **only it** decides.
- **Never gate behaviour on `Scene.*` without measuring the pattern list.** **A red line is not a feature flag.**
- **Measure before and bidirectionally after changing a heuristic. Don't trust memory or stale docs — measure.
  Check the object's actual shape, and decide what "pass" means, before asserting** — 5 self-inflicted check
  errors in one audit came from skipping that.
- Delete a design's dependent parameters too. Record deliberate scope boundaries ("record, don't smuggle").

## RLS · async · agent runs · conventions (full text → REFERENCE)

- **RLS fails silently on writes**: unbound `SELECT` → 0 rows; `UPDATE`/`DELETE` → rowcount 0 — **assert
  `rowcount` on every app-role write**. Isolation tests assert `WHERE tenant_id IS NOT NULL` (NULL-tenant rows
  are *global reference data* by policy). **Never leave probe rows**; tear fixtures down children-first.
- `set_config('app.tenant_id', …, true)` is **transaction-scoped**; **a failed flush poisons the session** →
  `await session.rollback()` before raising. **The outbox relay's caller owns the transaction.**
- **Two `AgentRun` creation sites** (`router.py:159` placeholder `input_hash=''` vs `orchestrator.py:674` real).
  **Query a run by its exact `run_id`**, never `conversation_ref`. Don't replace `deps.reader`'s identity.
- Conventional-commit subject + body explaining **why**, with test-count delta + ruff/mypy. Bootstrap tokens are
  **unsigned** → `APP_ENVIRONMENT` must be *explicitly* declared (S-1). **A test file outside `testpaths` never
  runs.** **Never issue parallel edits to one file.**

## Worker / RLS conventions (2026-09-21, after fixing a P0)

- **Two roles, two names.** Claiming runs *before any tenant is known* and every queue table is
  FORCE-RLS, so the claim must be on the owner role — that is now the ONE allowed exception and it
  has a name: `worker.wiring.queue_bookkeeping_session()`. **Everything that touches tenant data
  goes through `identity.tenant_context.tenant_session(ctx)`** (app role + re-bind at `after_begin`).
  `InboxWorker` claims on the first and processes each event on the second; `drain_once` commits the
  claim before processing so the `FOR UPDATE` locks are released and a crash is reclaimable.
- **The P0 that motivated it:** the worker ran its whole unit of work on `session_scope()` (bootstrap
  owner, `rolbypassrls`), so RLS was off for the agent run. `flag_service` looks a flag up by key and
  relies on RLS to scope it, so a run for tenant A read **tenant B's** `agent.business_read_enabled=false`
  → the read-tool branch was skipped → no receipt → no card. Invisible in 1773 green tests: every
  fixture seeded one tenant.
- **`tenant_session` lives in `identity/tenant_context`, not `platform_core.api`** (that module is
  "shared HTTP helpers"; the worker needed a DB helper and hand-rolled a broken binding instead of
  importing the request layer). `api.py` re-exports it with `from x import y as y` — the PEP 484
  explicit form, which is what satisfies both ruff and mypy.
- **Quote the guard's failure mode when adding one:** the isolation guard only fails when *two*
  tenants define the same flag key. A one-tenant fixture cannot see it (measured).
- **Stop the worker containers before running pytest** — a live consumer claims seeded inbox events
  within a second and the symptom is "0 processed / the run stays `queued`".
- **§5-D is not a bug.** `test_usage_counts_queued_runs` asserts that queuing a run consumes quota;
  placeholders are counted on purpose. Those 32 leftover rows are historical duplicate accounting.
- **outbox relay still claims and dispatches on one owner session** (per-row `apply_rls_tenant` is
  therefore decorative). Documented in `OutboxWorker.run_once`; the fix changes its documented
  batch-atomicity contract and needs its own verification.

## Still open / easy to re-break

- **Related-cases ranking must stay language-agnostic.** `pg_trgm`'s `similarity()` returns **0.000**
  for every short-Chinese pair tried ("能不能加急" vs "加急打样多久" → 0, because the shared 加急 is
  a 2-char term that lands in different 3-char windows). So `cases.find_related_cases` scores
  **term overlap** (Latin words ≥3 chars + CJK bigrams, Dice), with a document-frequency cut for
  tenant-ubiquitous terms (`RELATED_UBIQUITY` 0.5, only above `RELATED_DF_MIN_SAMPLE` 30 subjects)
  instead of a hand-written stopword list. Do not "simplify" it back to `similarity()` — that ships a
  permanently empty panel in the product's real language.
- **A bucket match is capped** (`RELATED_CATEGORY_ONLY_MAX` 2) and sorted last, labelled
  `match: "category"`. `category` defaults to `general`, so same-category alone is a list of arbitrary
  recent tickets — the original defect of the related-cases panel.
- **Chinese never selects a read tool (P1, other session's file).** 5/5 Chinese phrasings →
  `knowledge_qa`/`answer_from_knowledge` with zero candidates; 2/2 English → `business_read` +
  `order.get_status`. So the cards, and the whole "where is my order" path, are English-only today.
  Root cause is the kind decision in `intent.py`; the reproduction table is in
  `FINDINGS-2026-09-21-CARD-AND-RLS.md` §2.
- **Receipt timestamps must be ISO 8601, not epoch.** `redact_text` masks a 10-digit run to `[PHONE]`,
  which breaks the receipt's JSON and makes `_survives_redaction` refuse to publish it — so the card
  silently disappears on the real adapter while the demo (ISO) keeps working. `test_business_read_receipt`
  had a fixture returning ISO while the adapter returned an int, which is why the suite stayed green.
- **Cards are read-side only.** Receipts already live on `role=tool` turns; `agent_runtime/tool_card.py`
  normalises them into `timeline[].card`. Nothing new is stored, so no migration — and no card for a
  receipt whose shape it does not recognise (`card: null`, never raw JSON on the customer surface).
- **Diagnostic ladder for a config/flag that is "silently off":** connection role (`current_user`,
  `rolbypassrls`) → GUC (`current_setting('app.tenant_id', true)`, before *and* after any COMMIT) →
  business logic. `_load_flag` is `WHERE key = ?` + `.first()` with no ORDER BY **by design**, so it is
  only correct while RLS is enforced.

## See also

`REFERENCE.md` — walkthrough, browser acceptance, migration/auth/UI/heuristic detail, folded originals.
`HANDOVER-2026-09-19.md` — the other session's line (customer chat UI), still valid.
`FINDINGS-2026-09-21-CARD-AND-RLS.md` — the read-path/RLS findings, with raw outputs.
