"""Persist customer resolution-feedback exposure and explicit response events.

Revision ID: 0077_resolution_feedback
Revises: 0076_outbox_job_deadline
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0077_resolution_feedback"
down_revision: str | None = "0076_outbox_job_deadline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "customer_resolution_feedback_events"
APP_ROLE = "platform_app"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_ref_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(length=15), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.BigInteger(), nullable=False),
        sa.Column(
            "source",
            sa.String(length=31),
            nullable=False,
            server_default="support_surface",
        ),
        sa.CheckConstraint(
            "event_type IN ('requested', 'confirmed', 'rejected')",
            name="ck_customer_resolution_feedback_event_type",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_customer_feedback_idempotency",
        ),
    )
    op.create_index(
        "uq_customer_feedback_request_once",
        TABLE,
        ["tenant_id", "conversation_ref_id"],
        unique=True,
        postgresql_where=sa.text("event_type = 'requested'"),
    )
    op.create_index(
        "ix_customer_feedback_tenant_conversation_time",
        TABLE,
        ["tenant_id", "conversation_ref_id", "occurred_at"],
    )
    op.create_index("ix_customer_feedback_tenant_time", TABLE, ["tenant_id", "occurred_at"])
    op.create_index(
        "ix_customer_feedback_tenant_case_time",
        TABLE,
        ["tenant_id", "case_id", "occurred_at"],
        postgresql_where=sa.text("case_id IS NOT NULL"),
    )
    op.execute(f"GRANT SELECT, INSERT ON {TABLE} TO {APP_ROLE}")
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {TABLE} "
        "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
        "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
    )


def downgrade() -> None:
    op.drop_table(TABLE)
