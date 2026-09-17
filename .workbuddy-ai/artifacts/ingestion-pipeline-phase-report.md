# Ingestion pipeline — built, verified, committed

**Commit:** `d497383` — `feat(worker): index uploaded documents end to end`

## Why this phase existed

Uploaded documents reached `ingestion_status = 'uploaded'` and stopped. Nothing
parsed, chunked, embedded or indexed them, so the ingestion state machine
(`UPLOADED → PARSING → CHUNKING → EMBEDDING → INDEXING → READY`) existed with no
worker to advance it, and `hybrid_search` could never recall a real upload. The
retrieval half of the product was only testable against rows inserted by hand.

## What was built

| Item | Path |
| --- | --- |
| Ingestion worker | `apps/worker/src/worker/ingestion_consumer.py` |
| Queue role dispatch | `apps/worker/src/worker/runner.py` (`APP_WORKER_QUEUE`) |
| Deps + app-role URL | `apps/worker/src/worker/wiring.py` |
| Object read | `apps/api/src/platform_core/knowledge/service.py` (`get_object`) |
| Migrations | `0017`, `0018`, `0019` |
| Integration tests | `apps/api/tests/integration/test_ingestion_worker.py` (18) |
| Unit tests | `tests/unit/test_worker_dispatch.py` (9) |
| End-to-end check | `tests/e2e/e2e_ingestion_minio.py` |

One image now serves three worker roles — `interactive`, `ingestion`, `outbox` —
selected by `APP_WORKER_QUEUE`. `build_ingestion_deps` **fails closed** when no
embedder is configured, so a misconfigured deployment refuses to start rather
than claiming work it can never finish and leaving documents stuck in `parsing`.

## Four real defects found by running it

None of these were visible by reading code; each was found by executing the path.

1. **`document_versions` had neither timestamp column.** The claim orders by
   `created_at`; stale recovery compares `updated_at`. Both were imaginary —
   `DocumentVersion` does not use `TimestampMixin`. Reading the model file
   agreed with the wrong schema, which is why introspection alone missed it.

2. **RLS makes an unbound `UPDATE` match zero rows *without erroring*.**
   ```
   unbound UPDATE ... WHERE id = <known id>   rowcount 0   (no exception)
   bound   UPDATE ... WHERE id = <known id>   rowcount 1
   ```
   The claim reported `claimed=1` and persisted nothing — which, in production,
   means several workers ingesting the same document concurrently. Fixed by
   binding the tenant per row (taken from the row itself, not an escalation) and
   moving the cross-tenant scan into a `SECURITY DEFINER` function.

3. **`reclaim_stale_ingestions(0)` reclaimed nothing.** `updated_at` and `now()`
   are both whole seconds, so a row touched this second satisfies
   `updated_at == now`; `<` never fires. The semantics are "at least this
   stale", so the cutoff is `<=`.

4. **An explicit NULL from the ORM overrides a column `DEFAULT`.** After the new
   NOT NULL timestamps landed, every upload returned HTTP 503
   `IntegrityError: null value in column "updated_at"`. The trigger cannot help:
   it runs after the row reaches the table. The fix must use a
   **dialect-neutral** `server_default` — a Postgres `EXTRACT(EPOCH FROM now())`
   broke all 13 `tool_gateway` unit tests, which build the schema on SQLite.

## Verification

| Gate | Result |
| --- | --- |
| `pytest` | **612 passed** (was 561) |
| `ruff check` | clean |
| `mypy` | clean (24 files in the touched packages) |
| Migration reversibility | fresh DB, down to base and back up |
| Real-MinIO end to end | upload → MinIO → worker → chunks → `hybrid_search` |

End-to-end output against the real `b2b-e2e-minio` container:

```
minio round-trip OK (258 bytes)
worker stats: IngestStats(claimed=1, ready=1, failed=0, deferred=0, reclaimed=0)
ingestion_status=ready status=active
chunks=2 with_embedding=2
hybrid_search hits=2
    sections=['Refund policy', 'Eligibility window'] score=0.0164
    sections=['Refund policy', 'Processing time']    score=0.0161
```

## Test-isolation defect fixed along the way

The claim function is tenant-agnostic **by design**. A throwaway probe of mine
left one `uploaded` row in a *seeded* tenant, and the strict-count test then
failed with `IngestStats(claimed=2, ready=2)` — a message that reads exactly
like a pipeline bug. The autouse fixture now moves claimable rows belonging to
**other** tenants to `expired` rather than deleting them: it is someone else's
data, and altering data to make a test pass is the wrong trade. All probe
scripts were deleted before committing, since ad-hoc scripts writing to the real
database are the leak source.

## Not done in this phase

- Durable cross-process rate limiting on the inbound API.
- Backup/restore drill.
- Dependency scanning (pip-audit / npm audit).
- Three of four ADRs.
- Phase 5 items (SAML/SCIM, custom domains, quotas/billing events).
