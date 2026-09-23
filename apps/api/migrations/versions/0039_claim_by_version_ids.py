"""Let the ingestion claim be narrowed to specific versions.

Revision ID: 0039_claim_by_version_ids
Revises: 0038_eq_confirm_human_approval
Create Date: 2026-09-19

The problem this solves, measured
---------------------------------
`claim_ingestion_versions(p_batch)` answers one question: "the oldest N
claimable rows anywhere". That is exactly right for the worker, which serves
every tenant and does not care which document comes next. It is wrong for a
caller that already holds a document and needs *that* one ingested.

The failure is reproducible and was reproduced. `scripts/run_eval.py` uploads
13 corpus entries, then loops "one drain per entry" with `batch=10`:

    seeded 13 claimable rows, claim(10) -> the 10 oldest
    seeded 5 stale + 13 fresh,  claim(10) -> 5 stale + 5 fresh (indices 5-12 excluded)
    live, 13 claimable rows,    claim(10) -> 9 rows, and the OLDEST was absent

The last line is the signature of `FOR UPDATE SKIP LOCKED`: a row held by
another transaction is skipped rather than waited on, so a claim can return
fewer rows than the batch, and the ones it does return are the oldest. Every
round re-claims from the same head, so the tail of the queue never gets a
turn. The caller saw

    RuntimeError: ingesting refund-policy-v3 left ingestion_status='uploaded'
                   (stats=IngestStats(claimed=8, ready=8, ...))

for a document that was uploaded, stored, and perfectly ingestable. It is
worth noting the shape of that error: `ready=8` is the *batch* summary and
says nothing about the row the caller asked about - which is why the caller
asserts on its own version's row and not on the statistics.

Options considered
------------------
1. **Raise `batch` above the queue watermark in the caller.** Rejected: the
   watermark is a property of the whole database (the claim is
   tenant-agnostic) and grows with every other tenant's backlog. A caller
   cannot know it, and "drain to empty" turns one document's ingest into an
   unbounded amount of unrelated work.
2. **Claim and release unrelated rows until the wanted one appears.**
   Rejected: `release_claim` returns a row to `uploaded`, so the next claim
   picks it up again, and the loop oscillates over the same head. It also
   burns the pipeline's state machine for a scheduling problem.
3. **Drain the queue to empty from the caller.** Rejected: correct but
   hostile - a single caller would ingest every tenant's backlog to obtain
   one document, and two such callers would starve each other.
4. **Narrow the claim to a given id set.** Chosen. The candidate set is the
   only thing that changes; the locking discipline, the state filter, and the
   escalation are untouched.

Why this is not a widening of the escalation
--------------------------------------------
The privilege escalation (SECURITY DEFINER, `REVOKE ALL FROM PUBLIC`) exists
because `document_versions` is FORCE-RLS'd and the worker claims *before* it
knows a tenant. Migration 0018 justified it as "cannot be pointed at a chosen
row", and this revision introduces exactly that pointer - so the justification
is restated rather than quietly dropped:

- The parameter is an **id set plus a batch limit**, not a predicate. There is
  no tenant, no status, no free-form SQL, no ordering key. A caller cannot
  widen the status filter to `ready` or reach another tenant's rows *as a
  category*.
- It returns the same five columns. The result of narrowing is a subset of
  what the un-narrowed call already returned to the same role, so it discloses
  nothing that `claim_ingestion_versions(integer)` did not.
- **Targeting is a scheduling capability, not an access-control one.** The
  grant is unchanged and still `platform_app` only. A role that could not
  read a row before cannot read it now: the `WHERE` clause matches on id, but
  RLS is not what the function bypasses in the caller's favour - the function
  returns the row's own `tenant_id`, and the caller's subsequent writes
  (`_advance`, chunk inserts) run *with* `app.tenant_id` bound to that tenant.
  A caller that targets a tenant it has no business touching gets a claim and
  then fails its own RLS-checked writes, rather than being handed content.
- Rows that are not claimable are still filtered out (`uploaded`,
  `queued_for_retry` only). A targeted claim of a `ready` row returns nothing.

An id set is also the honest interface for what the callers actually do: the
eval harness does not want "some document", it wants the thirteen it just
uploaded.

The `id` tiebreaker, and why it belongs in the same revision
------------------------------------------------------------
`ORDER BY dv.created_at` alone is a **partial** order. `created_at` is stamped
by migration 0017's INSERT trigger as `EXTRACT(EPOCH FROM now())::bigint` -
whole seconds - so every row written in the same second is one ordering tie,
and `LIMIT n` over a tie group returns an arbitrary n of them. Measured: 13
rows inserted in a loop, all 13 at `created_at = 1789827361`.

That is not a cosmetic imperfection. It is the mechanism behind a
nondeterministic shortfall observed in a full test-suite run: a narrowly
targeted `drain_versions` call saw `claimed=0` on one round even though all
of its ids were in `uploaded` and matched the predicate, and a later round
picked them up. Two rows in the same tie group are interchangeable to the
sort, and `FOR UPDATE SKIP LOCKED` adds a second source of arbitrary choice -
a skipped row is simply not in the result. A caller cannot distinguish "my row
was not selected this time" from "my row is unclaimable".

Appending `dv.id` makes the order **total**. `id` is UUIDv7 (see
`orm_base.default_uuid`), which is time-ordered by construction, so the
tiebreaker preserves FIFO intent rather than overriding it: within one
second, the row created first still sorts first. The change is confined to
the `ORDER BY`; the candidate set, the status filter, the locking discipline,
and the ACL are all untouched.

Folding it into this revision rather than adding a 0040 is deliberate: 0039
already drops and recreates the one-argument function to change its
signature, so the tiebreaker costs nothing extra here. A separate revision
would exist only to rewrite a body this one is already rewriting, and the
downgrade path would have to unwind two ORDER BY changes instead of one.

Signature change, so DROP then CREATE
-------------------------------------
PostgreSQL cannot `CREATE OR REPLACE` when the argument list changes. The drop
is safe in the same transaction: nothing holds a prepared reference to the
one-argument form, and `downgrade` restores it exactly as 0025 left it, so a
downgrade reproduces the schema revision 0038 describes.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0039_claim_by_version_ids"
down_revision: str | None = "0038_eq_confirm_human_approval"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FUNC = "claim_ingestion_versions"

# _FUNC is a module-level constant, not a runtime parameter, so the
# interpolation below is safe and matches migrations 0018/0024/0025.
#
# `CAST(:ids AS uuid[])` is what lets the same function body serve both call
# shapes: psycopg sends NULL for the absent case, `= ANY(NULL)` evaluates to
# NULL (not true) per row, and the `IS NULL` branch is taken instead. Both
# branches stay in one SQL statement, so `FOR UPDATE SKIP LOCKED` still runs
# once, over one candidate set, with no procedural branch to reason about.
_CREATE = """
CREATE OR REPLACE FUNCTION placeholder_name(p_batch integer, p_version_ids uuid[] DEFAULT NULL)
RETURNS TABLE (
    version_id uuid,
    tenant_id uuid,
    object_uri text,
    ingestion_status text,
    content_type text
)
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT dv.id, dv.tenant_id, dv.object_uri, dv.ingestion_status::text,
           (dv.metadata ->> 'content_type')
    FROM public.document_versions dv
    WHERE dv.ingestion_status IN ('uploaded', 'queued_for_retry')
      AND (p_version_ids IS NULL OR dv.id = ANY (p_version_ids))
    ORDER BY dv.created_at, dv.id
    LIMIT p_batch
    FOR UPDATE SKIP LOCKED
$$;
"""

# Restores 0025's exact definition (one argument, no narrowing) so that
# `alembic downgrade 0038` yields the schema 0038 declares.
_DROP_RESTORE = """
CREATE OR REPLACE FUNCTION placeholder_name(p_batch integer)
RETURNS TABLE (
    version_id uuid,
    tenant_id uuid,
    object_uri text,
    ingestion_status text,
    content_type text
)
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT dv.id, dv.tenant_id, dv.object_uri, dv.ingestion_status::text,
           (dv.metadata ->> 'content_type')
    FROM public.document_versions dv
    WHERE dv.ingestion_status IN ('uploaded', 'queued_for_retry')
    ORDER BY dv.created_at
    LIMIT p_batch
    FOR UPDATE SKIP LOCKED
$$;
"""


def _grant() -> None:
    # Restated rather than assumed: `CREATE OR REPLACE` preserves the ACL of
    # an existing function, but the `DROP` in `upgrade` discards it, and
    # `CREATE FUNCTION` grants EXECUTE to PUBLIC by default. A missed REVOKE
    # is a silent widening, so every path re-applies all three.
    op.execute(f"REVOKE ALL ON FUNCTION {_FUNC}(integer, uuid[]) FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION {_FUNC}(integer, uuid[]) FROM platform")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_FUNC}(integer, uuid[]) TO platform_app")


def upgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {_FUNC}(integer)")
    op.execute(_CREATE.replace("placeholder_name", _FUNC))
    _grant()


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {_FUNC}(integer, uuid[])")
    op.execute(_DROP_RESTORE.replace("placeholder_name", _FUNC))
    op.execute(f"REVOKE ALL ON FUNCTION {_FUNC}(integer) FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION {_FUNC}(integer) FROM platform")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_FUNC}(integer) TO platform_app")
