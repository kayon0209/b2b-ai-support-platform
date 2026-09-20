# A live outbox/ingestion consumer shares the local database

Status: **resolved 2026-09-20.** Cause identified and stopped.
Found: 2026-09-19 by a race in the relay suite; identified: 2026-09-20.

## Resolution

The consumer was **four orphan `worker.runner` host processes**, left behind by
earlier agent sessions and running for roughly 30 hours. They were claiming the
outbox *and* ingestion queues the whole time.

They were orphans because of how agent shells behave: **an agent that stops its
shell does not stop the python child it started.** The shell exits, the worker
keeps running, and nothing in `docker ps` shows it — which is why the search
below looked in the wrong places for a day.

Stopping them resolved every flaky test seen that day:

- the full suite went `PYTEST_EXIT=0` (1558 tests, `release_check` exit 0);
- `test_outbox_relay.py` went from **8 skipped to 8 passed**.

To find them (and to find them again, if this recurs):

```bash
netstat -ano | grep ":5435"      # never ":5435|5432" - that also matches 54323
```

That lists the PIDs holding connections to the platform database. A python
process that holds DB connections but listens on no port is worker-shaped; read
its command line with `psutil` to confirm, then **kill the process tree** — not
just the shell that spawned it.

The diagnosis below is kept as written on 2026-09-19, because the evidence chain
is what made the eventual identification quick, and because the same symptoms
from a different cause would need the same checks.

## What is happening

The `platform` database has a consumer that claims rows from `outbox_events`
(and competes for the ingestion queue) while tests are running. It is **not**
the local docker compose stack — `docker ps` shows no `ai-worker-outbox` /
`ai-worker-interactive` — and it is not the test process.

The effect is that any test asserting *"the relay processed the row I just
seeded"* or *"the claim returned the row I asked for"* races it. The visible
symptom is always the same shape:

```
RelayStats(claimed=0, sent=0, failed=0, parked=0, unhandled=0)
IngestStats(claimed=0, ...)
```

which reads like a bug in the claim and is not.

## Evidence (each item reproduces independently)

1. **The relay suite skips itself.** `pytest apps/api/tests/integration/
   test_outbox_relay.py -rs` reports all 8 tests skipped, each with:

   > a live worker/relay is consuming outbox rows; `docker compose stop
   > ai-worker-interactive ai-worker-outbox` before running this suite

   That is the suite's own `_assert_no_live_relay`, which seeds a sentinel row
   and checks whether it is still `queued` two seconds later. It is correct.

2. **`docker ps` has no such worker.** Only postgres, redis, keycloak,
   minio and the Chatwoot stack. So the guard is not pointing at a container
   it can see — but its verdict still holds.

3. **A plain-SQL insert is consumed in under a second.** With no relay or
   worker code imported at all, a row inserted with an explicit
   `status='queued', attempts=0` reads back as `('sent', 1)` at t+0.5s:

   ```
   inserted 01a0ba7d-0245-75de-8015-51d965e45cd1
     t+0.5s: ('sent', 1)
     t+1.0s: ('sent', 1)   ... stable
   ```

4. **The database is not doing it.** On `outbox_events`:
   `pg_trigger` (non-internal) is empty; `pg_rules` is 0; the `status` column
   default is `'queued'::character varying` and `attempts` defaults to `0`.
   So an external process is issuing the `UPDATE`.

5. **Neutralising the guard makes the suite fail.** Temporarily rewriting the
   skip to `if False:` (in a throwaway copy, since deleted) lets all 8 tests
   run, and one fails with:

   ```
   E   AssertionError: assert 'sent' == 'queued'
   ```

   A test that seeds a row expecting `queued` finds it already `sent`. The
   guard is not a false positive; it is prevention.

6. **The same file, six runs, two pass and four fail.** Back to back with an
   identical command:

   ```
   run 1: FAIL -> test_usage_recorded_is_aggregated_through_the_relay
                  test_run_once_without_commit_does_not_persist
   run 2: FAIL -> test_run_once_without_commit_does_not_persist
   run 3: pass (12 passed)
   run 4: FAIL -> test_usage_recorded_is_aggregated_through_the_relay
                  test_run_once_without_commit_does_not_persist
   run 5: FAIL -> test_run_once_without_commit_does_not_persist
   run 6: pass (12 passed)
   ```

   Random outcome and a drifting failure set is the signature of a race. A
   real defect in the change under test would fail deterministically and fail
   the same tests.

7. **`pg_stat_activity` never catches it.** Sampling 40 times at 250ms
   intervals shows only idle pooled connections from hours earlier. The
   consumer's connection is brief, intermittent, or does not surface on this
   host — which is why the actor has not been named.

## What this also explains

The surviving flake in `apps/worker/src/worker/ingestion_consumer.py`'s
`drain_versions` docstring — one failure per run, a *different* test each run,
always `IngestStats(claimed=0)` from a narrowed claim over an id that is
claimable and not locked by that loop. `claim_ingestion_versions` is a global
FIFO and needs no tenant binding, so a concurrent consumer competes for the
same row and the winner is timing.

The `drain_versions` rework (per-round commit plus `handled_ids`) is still
correct: it fixes a real self-locking defect, measured as a 44-second
`idle in transaction` holding a row lock. The remaining failure is **not** that
work being incomplete — it is the measurement environment being dirty.

## First actions for whoever picks this up

> **Superseded by the resolution above** — this section is what was tried before
> the cause was known, and it did not find it. Kept because the third option
> (probe the queues directly) is the step that would have worked fastest, and
> the first two ate most of a day.

1. Find and stop the consumer. Candidates, in order of likelihood:

   ```powershell
   # another IDE/terminal session running a worker against this same DB
   Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
     Select-Object ProcessId, CommandLine | Format-List

   # another compose project (not just b2b-ai-support-*)
   docker ps -a --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
   ```

   Note every checkout on this machine points at `localhost:5435`, so a
   worker started from a different working copy looks like a ghost here.

   **What was actually true**: not a container and not a shell — orphan python
   children of finished agent sessions. `docker ps -a` showing everything
   Exited proves nothing, and a process listing is only useful if it survives
   the shell that started it. The reliable probe is the queue itself: insert a
   `queued` row and re-read it seconds later; a climbing `attempts` counter is
   a live consumer regardless of what any process or container list says.

2. Re-run the two suites that were being blocked:

   ```bash
   pytest apps/api/tests/integration/test_outbox_relay.py -q   # expect 8 run, not 8 skipped
   pytest apps/api/tests/integration/test_ingestion_worker.py -q
   ```

   If they go green, the diagnosis is confirmed. If they still fail, the cause
   is one of the other candidates and the search continues.

3. Only then treat any `claimed=0` failure as a code defect.

## Making the tests robust either way

The tests should not depend on *"the table contains only my row"*. Two options,
both already used elsewhere in this repository:

- **tenant isolation** — give each test its own tenant and let the relay claim
  per tenant;
- **row filtering** — let the claim take an id/version allowlist, the way
  `claim_ingestion_versions` already accepts `p_version_ids`.

`_assert_no_live_relay` is the right pattern to generalise: state the
precondition, and skip with the reason rather than failing misleadingly.
