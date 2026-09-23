# Project Memory — B2B AI Customer Support Platform

**Index of rules that change behaviour often.** Narrative → `YYYY-MM-DD.md`;
incidents, prose, folded originals → `REFERENCE.md` (**not** auto-injected).
v13, 2026-09-23.

## Orientation

**An AI control plane, not a chat product** — owns Tenant/Case/Knowledge/AgentRun/ToolExecution/Citation/
Evaluation/AuditEvent, **not** conversation content. It **hosts its own customer channels**: Chatwoot was
the kernel and **has been removed** (ADR 0012, all four stages; ADR 0013/0014 Accepted). `AGENTS.md`
rules 1/2 were rewritten in the same change that made them true.

**Four chat surfaces, only three of them the product:** `/support` = **the customer surface** (visitor
session, ADR 0011, renders the structured data card); `Workbench` = the operator's; the ops console
(`/quality`+`/gaps`+`/knowledge`) = the supervisor's; `/chat` = internal verification (operator token),
**slated for removal**. The pilot's value is the **data card**. **An ADR from another session is not the
user's decision: when one vetoes a request the user made, raise it — don't inherit it** (cost 2 sessions).

**The product's open gap is the closed loop's last mile** — `docs/product-gap-analysis.md` (three surfaces
+ one loop, per-surface gaps, ranked next-builds); `docs/product-shape-and-last-mile.md` (§2.1 = the
customer-surface UX spec, §3 = categories).

## Run & verify

```bash
docker compose --env-file .env -f infra/compose/docker-compose.yml up -d
./.venv/Scripts/python.exe -m ruff check apps packages scripts tests pytest_plugins_release
./.venv/Scripts/python.exe -m ruff format --check apps packages scripts tests pytest_plugins_release
./.venv/Scripts/python.exe -m mypy
mv tests/artifacts/release_gate_evidence.json /tmp/evidence.bak 2>/dev/null   # sandbox guard
./.venv/Scripts/python.exe -m pytest --junitxml=tests/artifacts/junit-final.xml
./.venv/Scripts/python.exe -m platform_core.evaluation.release_check --evidence-only
```

- **`--env-file .env` is mandatory** (compose substitutes `${...}` from the project dir, not `env_file`;
  an empty explicit value *overrides* the good one). The customer loop needs a *host* API on 8010
  (`python -m platform_core.main`), not compose's 8000. There is **no `chatwoot` profile any more**.
- **The full suite must be the LAST pytest invocation** — the gate plugin writes evidence unconditionally,
  so a targeted run overwrites it → `release_check` exit 2. **Do not "fix" that plugin; it is correct.**
  **Pause the worker containers first** — a live worker races the tests for the same queue rows.
- **The API/worker image bakes the source → a backend change needs `up -d --build`.** Restarting without
  `--build` leaves old code: new routes 404 while migrations are already applied, which reads exactly like
  "my change did not take". Rebuild ~2.5 min. **`-o addopts=""` to see the summary line.**
- **A red full-suite run is not evidence** — re-run the failing test alone first. **Green tests ≠ working
  UI** → `scripts/{ui_smoke,admin_render_check,support_card_smoke}.cjs`; `concurrency_probe.py` needs a
  **real service**. **Stop every consumer before testing, host processes too** (killing your shell does not
  kill your python child). **Never edit a page while a guard is driving it** (Vite HMR → hang).

## Environment & Windows traps

- `.venv/Scripts/python.exe` (3.12) — managed 3.13 has **no pytest**. Repo
  `D:/360Downloads/360驱动大师目录/b2b-ai-support-plan/b2b-ai-support-plan`.
- **pytest/mypy read `pyproject.toml`'s `pythonpath` → no env var.** For a bare `python -m`, set **one
  quoted** path: `export PYTHONPATH="$R/apps/api/src"`. **A `;`-joined PYTHONPATH in Git Bash is silently
  truncated** → `ModuleNotFoundError: platform_core`. Script source root:
  `apps/api/src;apps/worker/src;packages/*/src;.` (`observability` = `packages/observability/src`).
- Roles `platform` (bypasses RLS) vs `platform_app` (NOBYPASSRLS).
- **`python -m platform_core.main`, never bare uvicorn**; **`curl --noproxy "*"`**; bodies with `-o`, not a
  pipe. Vite needs `--host 127.0.0.1` or it only listens on IPv6.
- **Port ghosts** (an old `127.0.0.1:PORT` beats a new `0.0.0.0:PORT`) → judge by `netstat -ano` bind + PID,
  not by a 404/405. **`wmic` is blacklisted; the PowerShell tool drops stdout** → `tasklist` + `netstat`.
  **Kill what you started before leaving.**
- **`agent-browser` does not support Windows.** Use playwright-core in the managed node workspace driving the
  **system** Chrome (`executablePath`); scripts `.cjs`/`.mjs` **run from that workspace tree** (ESM ignores
  `NODE_PATH`); long runs in the background with output to a file.
- **Two sessions share this working tree.** Never `reset --hard`, `checkout --`, `clean -fd`; don't `git add`
  a file carrying the other session's uncommitted changes. **Do not delete `.workbuddy-ai/`.**
- **The Bash tool sometimes executes a command twice** → write one-off scripts **idempotently**
  (replace-if-present, never assert-on-count) or a second pass duplicates the edit.
- **Memory lives in `.workbuddy-ai/memory/`** (`.gitignore:36` = the committed canonical store). Tooling
  defaults to `.workbuddy/memory/` — **that is the wrong place**; a note there never travels with the repo.

## Rules that keep being violated (each with a case in REFERENCE)

- **The recurring defect is a capability with no consumer** — a value/column/function that is tested but
  that no production path reads. **Grep a reader's writers/callers before trusting it**; a test calling a
  function directly hides a missing caller. **Grep before adding an enum value.** Build the consumer with
  the capability, or don't build it. **Measure the tracked set, not the disk** (`git ls-tree`, not `ls`).
- **Removing a transport removes its consumers** — every value that only travelled on it becomes dead.
  Grep for them *before* the delete and give each a surviving consumer.
- **`is not None` is not a capability check** — ask the registry (`systems`), not the object.
- **Errors must be real 4xx.** FastAPI renders a returned **dict** as 200 — return `error_response()` /
  `domain_error_response()`. **Never swallow an exception: catch ⇒ log.**
- **A guard you cannot observe failing is not evidence** → mutation-test every guard; **an over-broad guard
  is worse than none**; give each guard a case **only it** decides; **state its failure mode when you add it**.
- **Never gate behaviour on `Scene.*` without measuring the pattern list.** **A red line is not a feature flag.**
- **Measure before and bidirectionally after changing a heuristic; check the object's actual shape, and
  decide what "pass" means, before asserting.** A change that only makes a test green can be "green that lies".
- **A test can pass for the wrong reason and still look like a guard** — assert the opposite case too.
- **Teardown order is a systemic defect here** — delete children before parents, enumerated from
  `pg_constraint`, never memory. Prefer **derived** tenant UUIDs (`uuid.uuid5`): a slug `ON CONFLICT` does
  **not** absorb a PK collision.
- **When "this is easier" argues against a documented design, the design wins.** Delete a design's dependent
  parameters too. Record deliberate scope boundaries.

## RLS · async · runs · conventions (full → REFERENCE)

- **RLS fails silently on writes**: unbound `SELECT` → 0 rows; `UPDATE`/`DELETE` → rowcount 0 — **assert
  `rowcount` on every app-role write**. **Never leave probe rows**; tear down children-first.
- `set_config('app.tenant_id', …, true)` is **transaction-scoped**; **a failed flush poisons the session** →
  `await session.rollback()` before raising. **The outbox relay's caller owns the transaction.**
- **Worker claiming precedes any tenant and every queue table is FORCE-RLS** → the owner role, the ONE
  allowed exception: `worker.wiring.queue_bookkeeping_session()`.
- **Query an `AgentRun` by its exact `run_id`**, never `conversation_ref` (two creation sites, `router.py:159`).
- `packages/observability`'s **field allowlist *is* the log schema** — an unlisted field is silently dropped.
- **Audit `after=` is hashed and unreadable; `metadata=` is the readable exception.**
- **An RLS policy must use `NULLIF(current_setting('app.tenant_id', true), '')::uuid`, never a bare `::uuid`.**
- **`set_config(..., false)` leaks on pooled connections** — a "no tenant context" assertion must `RESET`.
- **Adding a tenant-owned table needs five edits:** migration + `models_registry` import + *both*
  `TENANT_TABLES` tuples + `EXPECTED_MIGRATIONS` + a seed row in `test_cross_tenant_negative`.
- **Within one router, declare `/queue` before `/{x}`** (registration order; `queue` is a valid `{x}`).
- **A partial update needs its own `update_*` function, not `upsert` + defaults.**
- **`async def helper()` called synchronously returns a coroutine** → make the wrapper sync, `_run()` inside.
- **Unique "optional" fields must be NULL, not `""`.** **`Mapped[dict | None]` fails mypy** — add type args.
- **A fallback must *delegate* to the old function, not restate its arithmetic.**
- **A test whose configured value equals the default cannot prove the wiring.**
- **Two `create_engine`s exist and mixing them breaks** — admin URL sync, app-role URL async.
- **The admin web has two gates: `tsc --noEmit` and `vite build`.** Run both after touching `admin-web`.
- **A toggle that re-sends a whole object must re-send it verbatim.** **`x.get(k) or default` cannot express
  "explicitly zero".**
- Conventional-commit subject + body explaining **why**, with test-count delta + ruff/mypy. Bootstrap tokens
  are **unsigned** → `APP_ENVIRONMENT` must be *explicitly* declared (S-1). **A test outside `testpaths`
  never runs.** **Never issue parallel edits to one file.**

## Built and verified — do NOT "re-fix"

- **`/support`'s card works** (2026-09-22: `POST /v1/support/verify` + nav link; smoke 6/6).
  **Chinese reaches the read path. `mypy` is GREEN.**
- **The read-path ownership gate (2.2/2.5) is built and verified.** `verified_account` is **three-state, not
  a boolean**: `None` = operator run (no gate), `""` = anonymous → gate 1 `IDENTITY_REQUIRED` *before any
  connector call*, `"acme"` = verified → gate 2 refuses a mismatched receipt. **Omitting the key means
  `None` = any message may read any order — a hole, not a default.**
- **Chatwoot is gone and the gate is intact** — `release_check`: `cross_tenant_violations: 15`,
  `unauthorized_writes: 4`, `duplicate_replies: 4` passing.
- **No real send has ever been observed** — no SMTP server, no WeChat credentials here; only the outbound
  decision logic is tested (fake transports).
- **2026-09-23 customer-side walkthrough fixes** (Chinese tool selection + `_READ_PRIORITY`; handoff no
  longer swallows the next question — **the notice must be written by the API layer**; polling baseline;
  `npm run build` i18n/type errors). Detail → REFERENCE, `2026-09-23.md`.

## Still open

- **`external_resource_refs` has no production reader or writer** (was the Chatwoot account→tenant mapping).
  Table + model kept; dropping a table is a contract migration. **The recurring defect shape — either
  onboarding writes to it again or a migration removes it.**
- **Deliberately NOT changed:** `conversation_ref`'s `chatwoot:conversation:` uuid5 prefix (changing it
  orphans every existing ref) and `0002_bridge_mappings`'s `server_default="chatwoot"` (immutable; inert).
- **Related-cases ranking must stay language-agnostic** (`similarity()` = 0.000 for short Chinese pairs).
- **Receipt timestamps must be ISO 8601** — `redact_text` masks a 10-digit run to `[PHONE]`, breaking JSON.
- **Cards are read-side only** (`tool_card.py` → `timeline[].card`) and **only `/support` renders them** —
  the workbench shows looked-up data as text, and the legacy `DataCard.tsx` still runs on `/chat`. W1/W2.
- **`pricing/capability.py`'s matrix and the price list default empty** → every quote escalates. The demo's
  "加急费参考 ¥XX（依据：价目表 v3.2）" needs **data configured**, not code.
- Known flake: `test_ingestion_worker.py` (different tests across runs, each green alone) — pre-existing.
- **The closed loop's last mile is `evaluation/categories.py` + `issue_categories` (migration 0046).**
  Categories derive from `agent_runs.model_config.intent` (`business_line|scene|primary_kind`), so stats
  need no write path. "Automated" is strict: completed **and** no `abstain_reason` **and** no
  same-category re-ask within 900s. `routing`/`policy` fix types are never proposable.

## Conversation ref: one id, two meanings (open, needs authorization)

`/v1/conversations/{conversation_ref}/*` re-derives the path segment as an **external** id
(`conversation_ref_for(tenant, seg)`), while `/v1/conversations` **lists the already-derived**
`conversation_ref_id`. Passing the platform's own id back therefore derives it a second time.

- Live failure: the operator replay page. 4/4 sampled — the first returned **another conversation's
  content** (silent wrong data), the rest `NOT_FOUND`. `router.py:217`, also `:155`,
  `agent_reply_router.py:85`, `customer_router.py:65`. `/chat` avoids it only because it generates its own
  external id. Next to break: `/replies` (the workbench reply box is the roadmap's next step).
- Fix (recommended, **unauthorised**): drop `conversation_ref_for(...)` at those four sites so the segment
  **is** the platform ref; move `/chat` onto `/v1/support/sessions`. All uncommitted, no external consumers.
  `support_router.py`'s docstring already warned about exactly this.
