"""Fix the content type returned by claim_ingestion_versions.

Revision ID: 0025_fix_claim_content_type
Revises: 0024_claim_content_type
Create Date: 2026-09-18

Migration 0024 added `content_type` to the ingestion claim function with

    (dv.metadata -> 'content_type')::text

`->` returns the value as `json`, and casting a JSON *string* to `text` keeps
its surrounding double quotes. Measured directly against this database:

    '{"content_type": "text/markdown"}'::jsonb -> 'content_type'  ::text
        => "text/markdown"      -- with literal quote characters
    '{"content_type": "text/markdown"}'::jsonb ->> 'content_type'
        => text/markdown

The worker passes that value to `parse_document`, which matches on exact
content types, so every document uploaded through the API - the only path that
records `content_type` in metadata - failed ingestion with

    IngestionError: unsupported content type for parsing: "text/markdown"

A document whose metadata has no `content_type` returns SQL NULL from either
operator and the worker defaults to `text/markdown`, which is why the test
suite stayed green: the tests seed versions without the metadata field, and
only the real upload path sets it. Caught by `tests/e2e/e2e_ingestion_minio.py`,
which uploads through the service.

The signature is unchanged, so this only needs `CREATE OR REPLACE`; every
attribute is restated rather than relying on OR REPLACE to preserve them.
The downgrade restores 0024's exact definition so that a downgrade to 0024
reproduces the schema that revision describes.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0025_fix_claim_content_type"
down_revision: str | None = "0024_claim_content_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FUNC = "claim_ingestion_versions"

# _FUNC is a module-level constant, not a runtime parameter, so string
# interpolation here is safe and matches migrations 0018 and 0024.
_UPGRADE = """
CREATE OR REPLACE FUNCTION CLAIM(p_batch integer)
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
""".replace("CLAIM", _FUNC)

# Restores 0024 exactly, including its `->` extraction, so that
# `alembic downgrade 0024` yields the schema 0024 declares.
_DOWNGRADE = """
CREATE OR REPLACE FUNCTION CLAIM(p_batch integer)
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


def upgrade() -> None:
    op.execute(_UPGRADE)


def downgrade() -> None:
    op.execute(_DOWNGRADE)
