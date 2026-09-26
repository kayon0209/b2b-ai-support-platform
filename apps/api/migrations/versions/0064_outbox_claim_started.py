"""Track when a dedicated outbox consumer claimed an event.

Revision ID: 0064_outbox_claim_started
Revises: 0063_conversation_tasks
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0064_outbox_claim_started"
down_revision: str | None = "0063_conversation_tasks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "outbox_events",
        sa.Column("processing_started_at", sa.BigInteger(), nullable=True),
    )
    op.execute(
        "UPDATE outbox_events SET processing_started_at = created_at "
        "WHERE status = 'processing' AND processing_started_at IS NULL"
    )
    op.create_index(
        "ix_outbox_events_processing_started",
        "outbox_events",
        ["event_type", "processing_started_at"],
        postgresql_where=sa.text("status = 'processing'"),
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_events_processing_started", table_name="outbox_events")
    op.drop_column("outbox_events", "processing_started_at")
