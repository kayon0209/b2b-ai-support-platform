"""Add content_type to the ingestion claim function (Phase 1: document parser).

Revision ID: 0024_claim_content_type
Revises: 0023_tenant_run_quota
Create Date: 2026-09-18

The ingestion worker needs the document's content type to dispatch to the correct
text extractor (markdown vs PDF vs DOCX). ``claim_ingestion_versions`` already
returns ``object_uri`` for the same reason the worker needs metadata to do its
job - the content type is not sensitive, and exposing it through the same
SECURITY DEFINER function keeps the claim a single round-trip.

PostgreSQL does not allow CREATE OR REPLACE when the RETURN TABLE signature
changes, so the function is dropped and recreated. The upgrade/downgrade are
symmetric: the function is the only state.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0024_claim_content_type"
down_revision: str | None = "0023_tenant_run_quota"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FUNC = "claim_ingestion_versions"

# _FUNC is a module-level constant, not a runtime parameter, so string
# interpolation here is safe and is the same pattern used by migration 0018.
_UPGRADE = """
DROP FUNCTION IF EXISTS CLAIM(integer);
CREATE FUNCTION CLAIM(p_batch integer)
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
           (dv.metadata -> 'content_type')::text
    FROM public.document_versions dv
    WHERE dv.ingestion_status IN ('uploaded', 'queued_for_retry')
    ORDER BY dv.created_at
    LIMIT p_batch
    FOR UPDATE SKIP LOCKED
$$;
""".replace("CLAIM", _FUNC)

_DOWNGRADE = """
DROP FUNCTION IF EXISTS CLAIM(integer);
CREATE FUNCTION CLAIM(p_batch integer)
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
""".replace("CLAIM", _FUNC)


def upgrade() -> None:
    op.execute(_UPGRADE)
    op.execute(f"REVOKE ALL ON FUNCTION {_FUNC}(integer) FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION {_FUNC}(integer) FROM platform")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_FUNC}(integer) TO platform_app")


def downgrade() -> None:
    op.execute(_DOWNGRADE)
    op.execute(f"REVOKE ALL ON FUNCTION {_FUNC}(integer) FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION {_FUNC}(integer) FROM platform")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_FUNC}(integer) TO platform_app")
