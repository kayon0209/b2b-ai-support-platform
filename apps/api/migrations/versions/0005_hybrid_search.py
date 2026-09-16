"""pgvector extension + hybrid retrieval columns (ticket 14).

- Adds embedding vector(1536) and search_vector tsvector to chunks.
- GIN index on search_vector for FTS; ivfflat on embedding for ANN.
- Deterministic column renames avoided; pure additive (expand phase).

Revision ID: 0005_hybrid_search
Revises: 0004_lease_and_knowledge
Create Date: 2026-09-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_hybrid_search"
down_revision: str | None = "0004_lease_and_knowledge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS embedding vector(1536)")
    op.execute(
        """ALTER TABLE chunks ADD COLUMN IF NOT EXISTS search_vector tsvector
           GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED"""
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_chunks_fts ON chunks USING GIN (search_vector)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chunks_embedding ON chunks "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )
    # test vectors for RLS: match style of earlier migrations
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON chunks TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding")
    op.execute("DROP INDEX IF EXISTS ix_chunks_fts")
    op.execute("ALTER TABLE chunks DROP COLUMN IF EXISTS search_vector")
    op.execute("ALTER TABLE chunks DROP COLUMN IF EXISTS embedding")
