"""R2-01: append-only tenant-scoped supervisor corrections for emotion advice.

Revision ID: 0066_emotion_advice_reviews
Revises: 0065_copilot_reply_provenance
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0066_emotion_advice_reviews"
down_revision: str | None = "0065_copilot_reply_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"


def upgrade() -> None:
    op.create_table(
        "emotion_advice_reviews",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_ref_id", sa.Uuid(), nullable=False),
        sa.Column("advice_id", sa.String(64), nullable=False),
        sa.Column("suggested_level", sa.String(31), nullable=False),
        sa.Column("corrected_level", sa.String(31), nullable=False),
        sa.Column("reason_code", sa.String(63), nullable=False),
        sa.Column("reviewer_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_emotion_advice_review_idempotency"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "conversation_ref_id",
            "advice_id",
            "reviewer_id",
            name="uq_emotion_advice_review_reviewer_revision",
        ),
        sa.CheckConstraint(
            "suggested_level IN ('calm', 'frustrated', 'angry', 'escalation_risk')",
            name="ck_emotion_review_suggested_level",
        ),
        sa.CheckConstraint(
            "corrected_level IN ('calm', 'frustrated', 'angry', 'escalation_risk')",
            name="ck_emotion_review_corrected_level",
        ),
        sa.CheckConstraint(
            "reason_code IN ('overstated', 'understated', 'quoted_or_negated', "
            "'sarcasm_or_mixed_tone', 'context_missing', 'other')",
            name="ck_emotion_review_reason_code",
        ),
    )
    op.create_index(
        "ix_emotion_advice_reviews_conversation_created",
        "emotion_advice_reviews",
        ["tenant_id", "conversation_ref_id", "created_at"],
    )
    op.execute("ALTER TABLE emotion_advice_reviews ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE emotion_advice_reviews FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON emotion_advice_reviews "
        "USING (tenant_id::text = current_setting('app.tenant_id', true)) "
        "WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true))"
    )
    # Corrections are append-only feedback. Reviewers may read and add rows;
    # editing or deleting feedback would break the supervisor audit trail.
    op.execute(f"GRANT SELECT, INSERT ON emotion_advice_reviews TO {APP_ROLE}")


def downgrade() -> None:
    op.drop_table("emotion_advice_reviews")
