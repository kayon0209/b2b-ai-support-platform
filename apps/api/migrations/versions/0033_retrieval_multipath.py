"""Multi-path retrieval: trigram index, aliases, metadata filter support.

Revision ID: 0033_retrieval_multipath
Revises: 0032_chatwoot_tenant_resolver
Create Date: 2026-09-19

Three things, because the retrieval plan needs all three to be measurable:

1. `pg_trgm` + a GIN index on `chunks.text`
-------------------------------------------
The FTS path matches whole tokens, so a typo ("refnud"), a fault code
("EC-5O4" with a letter O) and every CJK query retrieve nothing: `'simple'
tsquery` has no stemming for CJK at all, and a Chinese sentence is one
token. Trigram similarity works at the character level for any script,
which is why it is the third retrieval path rather than a repair for the
first.

2. `knowledge_aliases`
----------------------
Product vocabulary is tenant-specific ("GC-500" is the same product as
"GateWay 500" only this tenant says so). Rows are tenant-owned and RLS'd
like everything else; the alias is unique per tenant so one surface form
maps to exactly one canonical term — two meanings for one alias would make
query expansion a coin flip.

3. GIN index on `chunks.metadata`
---------------------------------
The metadata filter (plan 1.5) predicates with JSONB containment
(`metadata @> {...}`). Without the index every filtered query is a scan of
the tenant's chunks; with `jsonb_path_ops` it is an index lookup. The
column predates this migration - it had no consumer, which is exactly the
defect shape the audit keeps finding.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0033_retrieval_multipath"
down_revision: str | None = "0032_chatwoot_tenant_resolver"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"

_ALIASES_TABLE = """
CREATE TABLE knowledge_aliases (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    -- The canonical term the corpus uses; the alias is the surface form a
    -- customer might type instead.
    term varchar(127) NOT NULL,
    alias varchar(127) NOT NULL,
    -- Expansion weight, > 0 and <= 2: an alias is at best as authoritative
    -- as the term itself, so it can rank a candidate up but never double it.
    weight numeric(4, 2) NOT NULL DEFAULT 1.00 CHECK (weight > 0 AND weight <= 2),
    created_at bigint NOT NULL DEFAULT 0
)
"""


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chunks_text_trgm ON chunks USING gin (text gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chunks_metadata_gin "
        "ON chunks USING gin (metadata jsonb_path_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_document_versions_metadata_gin "
        "ON document_versions USING gin (metadata jsonb_path_ops)"
    )
    op.execute(_ALIASES_TABLE)
    op.execute(
        "CREATE UNIQUE INDEX uq_aliases_tenant_alias ON knowledge_aliases (tenant_id, alias)"
    )
    op.execute("ALTER TABLE knowledge_aliases ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE knowledge_aliases FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON knowledge_aliases "
        "USING (tenant_id::text = current_setting('app.tenant_id', true)) "
        "WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true))"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON knowledge_aliases TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS knowledge_aliases")
    op.execute("DROP INDEX IF EXISTS ix_document_versions_metadata_gin")
    op.execute("DROP INDEX IF EXISTS ix_chunks_metadata_gin")
    op.execute("DROP INDEX IF EXISTS ix_chunks_text_trgm")
    # The extension itself is left installed: dropping it would break any
    # table still carrying a trigram index elsewhere.
