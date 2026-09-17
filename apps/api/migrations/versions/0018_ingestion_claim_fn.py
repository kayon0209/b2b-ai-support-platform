"""A tenant-unbound claim function for the ingestion queue.

Revision ID: 0018_ingestion_claim_fn
Revises: 0017_ingestion_claim
Create Date: 2026-09-17

The problem this solves, measured
---------------------------------
The ingestion worker claims work from `document_versions` before it knows
which tenant the work belongs to - the tenant *is* what the claim returns.
But the table is FORCE-RLS'd on

    USING (tenant_id::text = current_setting('app.tenant_id', true))

and the worker deliberately connects as `platform_app` (NOBYPASSRLS), so that
the data plane is protected by the database rather than by application code.
The combination does not work, and it fails closed in the worst way: the claim
query returns **zero rows** for every tenant, in every cycle, forever.

Measured against the live database (20 rows present):

    platform      (superuser)        visible = 20
    platform_app  (unbound)          visible = 0
    platform_app  (bound to one)     visible = that tenant's only

Zero is indistinguishable from "the queue is empty", so the worker would poll
happily, report nothing to do, and never ingest a single document - the same
silent-no-op shape as `runner.main()` building four `None`s. It cannot be
fixed by application filtering: the rows are not merely unfiltered, they are
invisible.

Options considered
------------------
1. **Connect the worker as `platform` (superuser).** Rejected: that role
   bypasses RLS on *every* table, so one missing `apply_rls_tenant` in the
   worker's write path silently crosses tenants. The escalation is permanent
   and global; the problem is one narrow read.
2. **Add `OR current_setting('app.tenant_id', true) = ''` to the policy.**
   Rejected outright: that exposes all tenants to any unbound connection, on
   every table, for every query. It trades a queue bug for a cross-tenant
   data leak.
3. **Enumerate candidate tenants first, then claim per tenant.** Rejected:
   it needs a query on `tenants` (also RLS'd, also invisible unbound), and
   even with that it makes the claim O(tenants) round-trips per poll and
   reintroduces a race between "which tenants have work" and "claim it".
4. **A narrow SECURITY DEFINER function.** Chosen - the same instrument
   migrations 0015 and 0016 already use for the identity bootstrap, for the
   same structural reason: a query that must run before a tenant can be
   established.

Why the function is safe
------------------------
The privilege escalation is confined to one statement with a fixed shape:

- It returns **only** the columns the claim needs: version id, tenant id,
  object key, current status. No document text, no metadata, no titles.
- It takes a batch size and nothing else. There is no caller-supplied tenant,
  no id, no predicate - so it cannot be used to read a chosen row.
- It is `STABLE` and performs `FOR UPDATE SKIP LOCKED`, so it participates in
  the caller's transaction and preserves the claim semantics exactly.
- `REVOKE ALL ... FROM PUBLIC` then `GRANT EXECUTE` to `platform_app` only:
  the superuser does not need it, and no other role can call it.
- `search_path` is pinned (`pg_catalog, public`) against a shadowing attack;
  every relation is schema-qualified in the body.
- It does not write. The status transition that constitutes the claim stays
  in the caller's transaction, so the function cannot be used to mutate state.

The data plane after this
-------------------------
Only the claim is elevated. The storage read, the chunk writes, every state
transition, and the READY promotion all run with `app.tenant_id` bound (the
worker calls `apply_rls_tenant` per claimed version), so RLS remains a real
boundary around everything that touches content.

Rollback note: the function is dropped in `downgrade`; the worker would then
fail closed again (zero claims) rather than mis-claiming, which is the safe
direction for a downgrade.
"""

from __future__ import annotations

from alembic import op

revision = "0018_ingestion_claim_fn"
down_revision = "0017_ingestion_claim"
branch_labels = None
depends_on = None

FUNCTION = "claim_ingestion_versions"

# Returns the claimable versions and locks them in one statement, so the
# caller's transaction owns the rows before it decides anything.
#
# VOLATILE, not STABLE: PostgreSQL rejects `FOR UPDATE` inside a non-volatile
# function outright ("SELECT FOR UPDATE is not allowed in a non-volatile
# function"). That is correct - taking a row lock is a write to the
# transaction's state, so declaring the function STABLE (as an earlier draft
# did) is both wrong and a startup failure rather than a subtle bug.
#
# The status filter is inlined rather than parameterised: it mirrors
# ingestion_consumer.CLAIMABLE_STATES, and a parameter would let a caller
# widen the predicate to states that are not claimable (e.g. READY), turning
# a queue scan into a general read of the table.
_CREATE = f"""
CREATE OR REPLACE FUNCTION {FUNCTION}(p_batch integer)
RETURNS TABLE (
    version_id uuid,
    tenant_id uuid,
    object_uri text,
    ingestion_status text
)
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT dv.id, dv.tenant_id, dv.object_uri, dv.ingestion_status::text
    FROM public.document_versions dv
    WHERE dv.ingestion_status IN ('uploaded', 'queued_for_retry')
    ORDER BY dv.created_at
    LIMIT p_batch
    FOR UPDATE SKIP LOCKED
$$;
"""  # noqa: S608 - interpolates only the module-owned FUNCTION constant


def upgrade() -> None:
    op.execute(_CREATE)
    # Locked down before it is granted: `CREATE FUNCTION` grants EXECUTE to
    # PUBLIC by default, so the REVOKE is load-bearing, not hygiene.
    op.execute(f"REVOKE ALL ON FUNCTION {FUNCTION}(integer) FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION {FUNCTION}(integer) FROM platform")
    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION}(integer) TO platform_app")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {FUNCTION}(integer)")
