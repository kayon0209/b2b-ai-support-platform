"""Knowledge gap queue and reviewed draft workflow (ticket 39).

Revision ID: 0013_knowledge_gaps
Revises: 0012_agent_run_started_at
Create Date: 2026-09-17

docs/development-plan.md Phase 4: "Knowledge gap queue and reviewed
knowledge-draft workflow."

Two tables. `knowledge_gaps` aggregates repeated unanswered questions so the
queue reflects demand rather than traffic; `knowledge_drafts` records a
proposed answer and its review decision, but never live knowledge - an
approved draft is published through the normal ingestion path and the
resulting document id is stored here for traceability.

Both are tenant-owned and carry RLS, like every other business table
(AGENTS.md rule 5). `target_space_id` and `published_document_id` are
nullable because a gap has no space until a reviewer picks one, and a draft
has no document until it is approved and published.

Expand-migrate-contract: this is purely additive, so the previous
application version keeps working while it rolls out.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_knowledge_gaps"
down_revision: str | None = "0012_agent_run_started_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"


def _tenant_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {table}
        USING (tenant_id::text = current_setting('app.tenant_id', true))
        WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true))
        """
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")


def upgrade() -> None:
    op.create_table(
        "knowledge_gaps",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("question_hash", sa.String(64), nullable=False),
        sa.Column("sample_question", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.String(63), nullable=False, server_default=""),
        sa.Column("status", sa.String(31), nullable=False, server_default="open"),
        sa.Column("frequency", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_seen_at", sa.BigInteger(), nullable=False),
        sa.Column("last_seen_at", sa.BigInteger(), nullable=False),
        sa.Column("acknowledged_at", sa.BigInteger(), nullable=True),
        sa.Column("target_space_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["target_space_id"], ["knowledge_spaces.id"], name="fk_gap_space"),
        # Deduplication key: one record per normalised question per tenant.
        sa.UniqueConstraint("tenant_id", "question_hash", name="uq_gap_question"),
    )
    op.create_index("ix_knowledge_gaps_tenant_id", "knowledge_gaps", ["tenant_id"])
    op.create_index("ix_knowledge_gaps_last_seen_at", "knowledge_gaps", ["last_seen_at"])
    op.create_index("ix_gaps_status_frequency", "knowledge_gaps", ["status", "frequency"])
    _tenant_rls("knowledge_gaps")

    op.create_table(
        "knowledge_drafts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("gap_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(31), nullable=False, server_default="pending"),
        sa.Column("author_kind", sa.String(31), nullable=False, server_default="human"),
        sa.Column("reviewed_by", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", sa.BigInteger(), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("published_document_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["gap_id"], ["knowledge_gaps.id"], name="fk_draft_gap"),
        sa.ForeignKeyConstraint(
            ["published_document_id"], ["documents.id"], name="fk_draft_document"
        ),
    )
    op.create_index("ix_knowledge_drafts_tenant_id", "knowledge_drafts", ["tenant_id"])
    op.create_index("ix_knowledge_drafts_gap_id", "knowledge_drafts", ["gap_id"])
    op.create_index("ix_drafts_status", "knowledge_drafts", ["status"])
    _tenant_rls("knowledge_drafts")


def downgrade() -> None:
    op.drop_table("knowledge_drafts")
    op.drop_table("knowledge_gaps")
