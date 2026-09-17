"""A tenant-unbound reclaim sweep for abandoned ingestion claims.

Revision ID: 0019_ingestion_reclaim_fn
Revises: 0018_ingestion_claim_fn
Create Date: 2026-09-17

Why a second function
---------------------
Migration 0018 added `claim_ingestion_versions` so the worker (which connects
as `platform_app`, NOBYPASSRLS) could find work without knowing a tenant in
advance. Recovery has the same structural problem from the opposite direction:
`reclaim_stale_ingestion` looks for rows that *nobody is tracking* - a worker
claimed them and died - so by definition it cannot know which tenant to bind.

Measured on `document_versions` with a known row id:

    platform_app, unbound  UPDATE ... WHERE id = <known id>   rowcount = 0
    platform_app, bound    UPDATE ... WHERE id = <known id>   rowcount = 1

Zero rows, no error, no warning. An RLS'd UPDATE that matches nothing returns
success, so a reclaim sweep written as a plain UPDATE would report "0
reclaimed" forever and the abandoned version would sit in PARSING
indefinitely - indistinguishable, from the outside, from a queue with nothing
to do. The document would be permanently unsearchable and no alert would ever
fire, because PARSING is a legitimate state to be in.

Why the function is safe
------------------------
Same instrument as 0015/0016/0018, and the same confinement:

- It takes only a timeout. No ids, no predicates from the caller, so it
  cannot be aimed at a chosen row.
- It touches only rows whose state is one of the four in-progress values
  *and* whose `updated_at` is older than the cutoff, and it moves them to
  exactly one state (`uploaded`). There is no field it can be used to set.
- It returns a count, not row data - so it cannot be used to read anything,
  not even which tenants have stuck work. (The caller does not need that: it
  re-claims through 0018, which returns the tenant.)
- The state list is inlined, mirroring `ingestion_consumer.IN_PROGRESS_STATES`.
  Parameterising it would let a caller widen the predicate to any state,
  including READY, turning a recovery sweep into a bulk status rewrite.
- `REVOKE ALL ... FROM PUBLIC` then `GRANT EXECUTE` to `platform_app` only.

VOLATILE because it writes. `search_path` pinned and every relation
schema-qualified, as in 0018.

Rollback note: dropping this function makes recovery a no-op (the worker
falls back to whatever the caller had), which fails safe - an abandoned claim
stays stranded rather than being stolen from a live worker.
"""

from __future__ import annotations

from alembic import op

revision = "0019_ingestion_reclaim_fn"
down_revision = "0018_ingestion_claim_fn"
branch_labels = None
depends_on = None

FUNCTION = "reclaim_stale_ingestions"

# `<=` rather than `<` in the cutoff comparison, and the difference is not
# cosmetic. Both clocks are whole seconds, so a row touched in the current
# second has `updated_at == now`; with `<` a timeout of 0 reclaims nothing.
# Measured: `reclaim_stale_ingestions(0)` returned 0 against a row in PARSING
# whose `updated_at` equalled `now`. Every caller reads the parameter as "at
# least this stale", so the comparison must match that reading.
_CREATE = f"""
CREATE OR REPLACE FUNCTION {FUNCTION}(p_timeout_seconds integer)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    affected integer;
BEGIN
    UPDATE public.document_versions dv
    SET ingestion_status = 'uploaded'
    WHERE dv.ingestion_status IN ('parsing', 'chunking', 'embedding', 'indexing')
      AND dv.updated_at <= CAST(EXTRACT(EPOCH FROM now()) AS bigint) - p_timeout_seconds;

    GET DIAGNOSTICS affected = ROW_COUNT;
    RETURN affected;
END;
$$;
"""  # noqa: S608 - interpolates only the module-owned FUNCTION constant

# `<=` rather than `<`, and the difference is not cosmetic. Both clocks are
# whole seconds, so a row touched in the current second has
# `updated_at == now`; with `<` a timeout of 0 reclaims nothing, and the
# strict inequality makes the threshold mean "strictly older than now" while
# every caller reads it as "at least this stale". Measured: with `<`,
# `reclaim_stale_ingestions(0)` returned 0 against a row in PARSING whose
# `updated_at` equalled `now`.
def upgrade() -> None:
    op.execute(_CREATE)
    # `CREATE FUNCTION` grants EXECUTE to PUBLIC by default, so this REVOKE
    # is load-bearing rather than hygiene.
    op.execute(f"REVOKE ALL ON FUNCTION {FUNCTION}(integer) FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION {FUNCTION}(integer) FROM platform")
    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION}(integer) TO platform_app")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {FUNCTION}(integer)")
