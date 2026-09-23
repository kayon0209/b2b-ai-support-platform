# Project Memory — B2B AI Customer Support Platform

Rules only. Narrative → `YYYY-MM-DD.md`; prose + folded originals → `REFERENCE.md` (**not** auto-injected).
v12, 2026-09-22.

## Orientation

**An AI control plane, not a chat product** — owns Tenant/Case/Knowledge/AgentRun/ToolExecution/Citation/
Evaluation/AuditEvent, **not** conversation content. It also **hosts its own customer channels**: Chatwoot was
the kernel and **has been removed** (ADR 0012, all four stages; ADR 0013/0014 Accepted). `AGENTS.md` rules 1/2
were rewritten in the same change that made them true.

**Four chat surfaces, only three of them the product:** `/support` = **the customer surface** (visitor session,
ADR 0011, renders the structured data card); `Workbench` = the operator's; the ops console
(`/quality`+`/gaps`+`/knowledge`) = the supervisor's; `/chat` = internal verification (operator token) and is
**slated for removal** — the user's target is three surfaces, not four. The pilot's value is the **data card**.
**An ADR from another session is not the user's decision: when one vetoes a request the user made, raise it —
don't inherit it** (cost a session twice).

**The product's open gap is the closed loop's last mile** — see `docs/product-gap-analysis.md` (the three
surfaces + the one loop, per-surface gaps, and the ranked list of what to build next).

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

- **`--env-file .env` is mandatory** (compose substitutes `${...}` from the project dir, not `env_file`, and an
  empty explicit value *overrides* the good one). The customer loop needs a *host* API on 8010
  (`python -m platform_core.main`), not compose's 8000. There is **no `chatwoot` profile any more**.
- **The full suite must be the LAST pytest invocation** — the gate plugin writes evidence unconditionally, so a
  targeted run overwrites it → `release_check` exit 2. **Do not "fix" that plugin; it is correct.**
- **The API/worker image bakes the source, so a backend change needs `up -d --build`.** Restarting
  without `--build` leaves the container on old code: new routes 404 while the migrations are already
  applied, which reads exactly like "my change did not take". Rebuild takes ~2.5 min.
- **`-o addopts=""` to see the summary line** — the gate plugin swallows `N passed`. Not a failure, just silence.
- **A red full-suite run is not evidence** — re-run the failing test alone first. **Green tests ≠ working UI** →
  `scripts/{ui_smoke,admin_render_check,support_card_smoke}.cjs`; `concurrency_probe.py` needs a **real service**.
- **Stop every consumer before testing, host processes too** — stopping your shell does not stop your python
  child; kill the tree. Probe: insert a `queued` outbox row and re-read. **Never edit a page while a guard is
  driving it** (Vite HMR → the guard hangs).

## Environment

- `.venv/Scripts/python.exe` (3.12) — managed 3.13 has **no pytest**. Repo
  `D:/360Downloads/360驱动大师目录/b2b-ai-support-plan/b2b-ai-support-plan`.
- **pytest and mypy get their path from `pyproject.toml`'s `pythonpath`, so they need no env var.** For a bare
  `python -m`, set **one quoted** path: `export PYTHONPATH="$R/apps/api/src"`. **A `;`-joined PYTHONPATH in Git
  Bash is silently truncated** (the shell eats it as a command separator) → `ModuleNotFoundError: platform_core`.
- Roles `platform` (bypasses RLS) vs `platform_app` (NOBYPASSRLS).
- **Two sessions share this working tree.** Never `reset --hard`, `checkout --`, `clean -fd`; don't `git add` a
  file carrying the other session's uncommitted changes. **Do not delete `.workbuddy-ai/`.**
- **The Bash tool sometimes executes a command twice.** Write one-off scripts **idempotently** (replace-if-present,
  never assert-on-count) or a second pass duplicates the edit — it produced two `self._x = None` blocks and a
  duplicated dict entry in one session.

## Windows traps (full → REFERENCE)

- **`python -m platform_core.main`, never bare uvicorn**; **`curl --noproxy "*"`**; bodies with `-o`, not a pipe.
- **Port ghosts** (an old `127.0.0.1:PORT` beats a new `0.0.0.0:PORT`) → judge by `netstat -ano` bind + PID, not
  by a 404/405. **`wmic` is blacklisted; the PowerShell tool drops stdout** → `tasklist` + `netstat`.
  **Kill what you started before leaving.**

## Rules that keep being violated

- **The recurring defect is a capability with no consumer** — a value/column/function that is tested but that no
  production path reads. **Grep a reader's writers/callers before trusting it**; a test calling a function
  directly hides a missing caller. **Grep before adding an enum value.** Build the consumer with the capability,
  or don't build it. **Measure the tracked set, not the disk** (`git ls-tree`, not `ls`) — and read a set's
  *source* before comparing, not your own copy of it.
- **Removing a transport removes its consumers.** When you delete a delivery path, every value that only travelled
  on it becomes dead — grep for them *before* the delete, and give each one a surviving consumer. Measured twice:
  the handoff team/business-line/attachments lived only in the Chatwoot private note; the abstention notice fell
  through to the platform branch and reached **nobody** on email/WeChat.
- **`is not None` is not a capability check.** `build_channel_sender` always returns a `ChannelSender`, so
  `has_channel_sender = deps.channel_sender is not None` reported "can send" for a deployment that can send
  nothing. Ask the registry (`systems`), not the object.
- **Errors must be real 4xx.** FastAPI renders a returned **dict** as 200 — return `error_response()` /
  `domain_error_response()`. **Never swallow an exception: catch ⇒ log.**
- **A guard you cannot observe failing is not evidence** → mutation-test every guard; **an over-broad guard is
  worse than none**; give each guard a case **only it** decides; **state its failure mode when you add it**.
- **Never gate behaviour on `Scene.*` without measuring the pattern list.** **A red line is not a feature flag.**
- **Measure before and bidirectionally after changing a heuristic; check the object's actual shape, and decide
  what "pass" means, before asserting.** A change that only makes a test green can be "green that lies".
- **Teardown order is a systemic defect here** — delete children before parents, enumerated from `pg_constraint`,
  never memory; a failed teardown leaves rows that redden *another* file next round. Prefer **derived** tenant
  UUIDs (`uuid.uuid5`): a slug `ON CONFLICT` does **not** absorb a PK collision.
- **When "this is easier" argues against a documented design, the design wins.** Delete a design's dependent
  parameters too. Record deliberate scope boundaries.

## RLS · async · runs · conventions (full → REFERENCE)

- **RLS fails silently on writes**: unbound `SELECT` → 0 rows; `UPDATE`/`DELETE` → rowcount 0 — **assert
  `rowcount` on every app-role write**. **Never leave probe rows**; tear down children-first.
- `set_config('app.tenant_id', …, true)` is **transaction-scoped**; **a failed flush poisons the session** →
  `await session.rollback()` before raising. **The outbox relay's caller owns the transaction.**
- **Worker claiming precedes any tenant and every queue table is FORCE-RLS** → the owner role, the ONE allowed
  exception: `worker.wiring.queue_bookkeeping_session()`.
- **Query an `AgentRun` by its exact `run_id`**, never `conversation_ref` (two creation sites, `router.py:159`).
- `packages/observability`'s **field allowlist *is* the log schema** — an unlisted field is silently dropped (and
  is a redaction boundary). Add to `ALLOWED_LOG_FIELDS` **deliberately**, with a reason.
- **Audit `after=` is hashed and unreadable; `metadata=` is the readable exception.** Put an event's own
  *parameters* in `metadata`, a payload in `after`. Reading a value back out of `after` is impossible.
- **An RLS policy must use `NULLIF(current_setting('app.tenant_id', true), '')::uuid`, never a bare
  `::uuid`.** The setting is *set* far more often than it is valid, and a bare cast on an empty value
  raises a `DataError` instead of matching no rows - which is not fail-closed in the way you want: the
  caller sees an exception and "zero rows" never gets asserted.
- **`set_config(..., false)` leaks on pooled connections.** Two tests use session-level binding; a later
  test reusing that connection inherits it. Any assertion that means "no tenant context" must `RESET
  app.tenant_id` explicitly rather than assuming a fresh session gives you one.
- **Adding a tenant-owned table needs five edits:** migration + `models_registry` import (or
  `create_all` builds a schema missing it) + *both* `TENANT_TABLES` tuples + `EXPECTED_MIGRATIONS` + a
  seed row in `test_cross_tenant_negative` (that test asserts tenant A sees its own row in every table).
- **Two `create_engine`s exist and mixing them breaks.** `platform_core.db.create_engine` is async
  (`await engine.dispose()`); `sqlalchemy.create_engine` is sync. Admin URL → sync, app-role URL → async.
- **Within one router, declare `/queue` before `/{x}`.** FastAPI matches in registration order and
  `queue` is a valid `{x}`, so the other order makes the literal route unreachable.
- **A partial update needs its own `update_*` function, not `upsert` + defaults.** Upserting on a
  status-only patch resets skills to empty and capacity to the default - a silent edit that takes a
  week to notice.
- **`async def helper()` called synchronously returns a coroutine**, so `assert helper() == x`
  compares a coroutine object and passes/fails for the wrong reason. This has bitten twice in test
  helpers - make the wrapper sync and `_run()` inside it.
- **Unique "optional" fields must be NULL, not `""`.** UNIQUE treats NULLs as distinct, so N rows may
  omit a value while a non-empty one stays unique. An empty-string default makes the *second* row fail.
- **`cases.assignee_ref` and `agent_profiles.user_ref` are the same opaque shape on purpose** - no
  translation layer between "who owns this" and "who is this".
- **`Mapped[dict | None]` fails mypy (`type-arg`)** - a bare `dict` is generic; write
  `Mapped[dict[str, float] | None]`.
- **A fallback must *delegate* to the old function, not restate its arithmetic.** `resolve_sla_policy`
  calls `sla_policy_for_tier` when there is no row, so "unconfigured behaves exactly as before" is a
  construction rather than a hope. If you reimplement it, it drifts by a minute and nobody notices.
- **A test whose configured value equals the default cannot prove the wiring.** Pick a value the old
  path cannot produce, or removing the wiring leaves the test green.
- **Pause the worker containers before a full-suite run.** Measured, not advisory: with
  `ai-worker-{outbox,ingestion,interactive,sla,retention}` up, the tests race the live workers for the
  same queue rows and 1-5 tests fail with *a different set every run, each passing alone*. Pausing them
  gives 2178 passed / 0 failed. This is a real resource race, not flakiness - and it is why "a red
  full-suite run is not evidence" needed the extra clause: **check what else is running first.**
- **A whole-dict overwrite destroys fields the writer set.** `queue_agent_run` wrote
  `model_config={"mode": ...}` and `_adopt_or_create_run` replaced `model_config` wholesale with the
  lineage snapshot, so `internal_draft` was **erased before any executor could read it** - an operator
  asking for a draft sent a real message. **When you replace a JSON column, enumerate what else writes
  to it.** "Nothing reads it" and "it was deleted" look identical from the reader.
- **A per-test cleanup must not delete the module's seeded corpus.** Wiping `chunks` between tests
  made every run abstain for "no authorized evidence", so the tests failed for a reason unrelated to
  what they were testing. Split the teardown: per-test rows vs module-scoped fixtures.
- **A read after the run path must use the admin connection, or re-bind.** `orchestrator.run()` commits
  internally (the control lease) and `set_config(..., true)` is transaction-scoped, so an app-role read
  afterwards returns zero rows - which looks exactly like "the run recorded nothing".
- **A timing fixture must be relative to the event under test, not to an earlier `now`.** The category
  before/after test captured `now` before the promote, so in a slow run the "after" row landed *before*
  `automated_at` - green alone, red in a full run.
- **A test double must implement the contract, not the one method the test happens to call.** The
  orchestrator resolves an arm's prompt via `with_template`, so a generator double without it fails the
  run rather than the assertion.
- **The admin web has two gates: `tsc --noEmit` and `vite build`.** `tsc` catches types; the build
  catches what `tsc` alone does not (a page wired into `main.tsx` without its import still typechecks
  as an unused module). Run both after touching `apps/admin-web`.
- **A toggle that re-sends a whole object must re-send it verbatim.** Enabling an experiment posts the
  arms back; sending anything reconstructed would silently rewrite the traffic split on a click.
- **`x.get(k) or default` cannot express "explicitly zero".** It silently turned a `weight: 0` arm into
  weight 1, which also made the "weights must sum above zero" guard unreachable.
- **A test can pass for the wrong reason and still look like a guard.** The first `internal_draft` test
  omitted `channel_system`, so `_dispatch` took the platform-surface branch and sent nothing *whatever
  the mode was*. The fix: assert the opposite case too (`customer_reply` **must** send), or the
  "no send" assertion proves nothing.
- **A capability with no consumer, again: `link_conversation` had no production caller** (only tests).
  Feature 1.5's cross-device continuity therefore did not work. **Before trusting a table, grep for its
  writer in production paths, not just its tests.**
- **The workbench had no reply verb.** `POST /v1/customer/.../messages` writes a *customer* turn. An
  agent could read every context and send nothing - so canned replies, human takeover and AI suggestions
  were all suspended. `POST /v1/conversations/{ref}/replies` is the write path; it transfers the lease
  first, in the same transaction, or the AI can send its own draft over the human's answer.
- **A "free-typed" and an "unknown" provenance must not share a value.** Collapsing them makes
  "nobody reported" indistinguishable from "everybody typed", and the only place that difference lands
  is the denominator of an adoption rate - so the error always flatters the feature it measures.
  `ORIGIN_UNKNOWN = ""` and `ORIGIN_FREE = "free"` are separate for this reason.
- **Attribution must live on the row, not on the lease.** The control lease holds *current* ownership,
  so attributing past replies through it makes every per-agent number change retroactively when a
  conversation is handed over. `conversation_turns.author_ref` is the fix.
- **A reply satisfies the first-response clock only if something records it.** Batch 4 shipped the reply
  path and `first_responded_at` was still only set by an explicit command no production caller issued -
  so an agent could answer and the case would escalate for "no first response". **When you add a path
  that satisfies a state machine, check who moves the state.**
- **An authored message must not be redacted.** `redact_text` masks any 10+ digit run, so it eats the
  order number the agent is answering about, and it makes the stored copy differ from what the customer
  received. `append_authored_turn` is a separate function, not a `redact=False` flag - a defaulted
  boolean gets flipped by someone who has not read the reason.
- Conventional-commit subject + body explaining **why**, with test-count delta + ruff/mypy. Bootstrap tokens are
  **unsigned** → `APP_ENVIRONMENT` must be *explicitly* declared (S-1). **A test outside `testpaths` never runs.**
  **Never issue parallel edits to one file.**

## Built and verified — do NOT "re-fix"

- **`/support`'s card works** (2026-09-22, the page calls `POST /v1/support/verify` + a nav link; smoke 6/6).
  **Chinese reaches the read path. `mypy` is GREEN.**
- **The read-path ownership gate (2.2/2.5) is built and verified.** `verified_account` is **three-state, not a
  boolean**: `None` = operator run (no gate), `""` = anonymous → gate 1 `IDENTITY_REQUIRED` *before any connector
  call*, `"acme"` = verified → gate 2 refuses a mismatched receipt. **Omitting the key means `None` = any message
  may read any order — a hole, not a default.**
- **Chatwoot is gone and the gate is intact** — `release_check` reports `cross_tenant_violations: 15`,
  `unauthorized_writes: 4`, `duplicate_replies: 4` passing. The zero-tolerance tests were re-pointed at the
  channel/platform path **before** the adapter was deleted, exactly as ADR 0012 required.
- **No real send has ever been observed** — no SMTP server, no WeChat credentials here; only the outbound
  decision logic is tested (fake transports).

## Still open

- **`external_resource_refs` has no production reader or writer** (it was the Chatwoot account→tenant mapping).
  Table + model kept; dropping a table is a contract migration. **This is the recurring defect shape — either
  onboarding writes to it again or a migration removes it.**
- **Deliberately NOT changed:** `conversation_ref`'s `chatwoot:conversation:` uuid5 prefix (changing it orphans
  every existing ref) and `0002_bridge_mappings`'s `server_default="chatwoot"` (migrations are immutable; the
  default is inert for every explicit write).
- **Related-cases ranking must stay language-agnostic** (`similarity()` = 0.000 for short Chinese pairs).
- **Receipt timestamps must be ISO 8601** — `redact_text` masks a 10-digit run to `[PHONE]`, breaking the JSON.
- **Cards are read-side only** (`tool_card.py` → `timeline[].card`) and **only `/support` renders them** — the
  workbench shows looked-up data as text, and the legacy `DataCard.tsx` still runs on `/chat`. Unifying them is
  gap-analysis item W1/W2.
- **`pricing/capability.py`'s matrix and the price list default empty** → every quote escalates. The demo's
  "加急费参考 ¥XX（依据：价目表 v3.2）" needs **data configured**, not code.
- Known flake: `test_ingestion_worker.py` (different tests across runs, each green alone) — pre-existing.
- **The closed loop's last mile is `evaluation/categories.py` + `issue_categories` (migration 0046).**
  Categories are derived from `agent_runs.model_config.intent` (`business_line|scene|primary_kind`), so
  stats need no write path. "Automated" is strict: completed **and** no `abstain_reason` **and** no
  same-category re-ask within 900s - `status == COMPLETED` alone scores a re-ask as two automations.
  `routing`/`policy` fix types are never proposable; that exclusion is the guard against automating
  complaints. See `docs/product-shape-and-last-mile.md` §3.

## Where to write memory (do this right)
**`.workbuddy-ai/memory/` is the committed canonical store** (`.gitignore:36` says so explicitly);
`.workbuddy/` is ignored in full. Tooling defaults to `.workbuddy/memory/` — **that is the wrong
place**, a note written there never travels with the repo. Write daily logs and `MEMORY.md` here.

## Conversation ref: one id, two meanings (open, needs authorization)
`/v1/conversations/{conversation_ref}/*` re-derives the path segment as an **external** id
(`conversation_ref_for(tenant, seg)`), while `/v1/conversations` **lists the already-derived**
`conversation_ref_id`. Passing the platform's own id back therefore derives it a second time.

- Live failure: the operator replay page. 4/4 sampled conversations — the first returned
  **another conversation's content** (silent wrong data), the rest `NOT_FOUND`.
  `router.py:217` (`/{ref}/replay`), also `:155`, `agent_reply_router.py:85`,
  `customer_router.py:65`. `/chat` avoids it only because it generates its own external id.
- Next to break: `/replies` (agent reply) — no UI caller yet, but the workbench reply box is the
  roadmap's next step, and it would write every reply into a conversation the customer cannot address.
- Fix (recommended, unauthorised): drop `conversation_ref_for(...)` at those four sites so the
  segment **is** the platform ref; move `/chat` onto `/v1/support/sessions`. All uncommitted code,
  no external consumers.
- `support_router.py`'s module docstring already warned about exactly this
  ("passing the ref back would derive it a second time") — the warning was written, then violated.
