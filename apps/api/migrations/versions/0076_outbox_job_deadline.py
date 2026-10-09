"""Persist a first-attempt clock for bounded durable outbox jobs.

Revision ID: 0076_outbox_job_deadline
Revises: 0075_tool_exec_comp
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0076_outbox_job_deadline"
down_revision: str | None = "0075_tool_exec_comp"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("outbox_events", sa.Column("first_attempt_at", sa.BigInteger(), nullable=True))
    op.add_column("outbox_events", sa.Column("deadline_at", sa.BigInteger(), nullable=True))
    op.add_column("outbox_events", sa.Column("external_attempt_limit", sa.Integer(), nullable=True))
    op.create_index(
        "ix_outbox_event_type_deadline",
        "outbox_events",
        ["event_type", "deadline_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_event_type_deadline", table_name="outbox_events")
    op.drop_column("outbox_events", "external_attempt_limit")
    op.drop_column("outbox_events", "deadline_at")
    op.drop_column("outbox_events", "first_attempt_at")
