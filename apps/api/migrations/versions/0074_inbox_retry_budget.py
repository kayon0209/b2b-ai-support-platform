"""Persist inbox delivery attempt counts across worker process restarts.

Revision ID: 0074_inbox_retry_budget
Revises: 0073_tool_exec_reconcile
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0074_inbox_retry_budget"
down_revision: str | None = "0073_tool_exec_reconcile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "inbox_events",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "inbox_events",
        sa.Column("first_started_at", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("inbox_events", "first_started_at")
    op.drop_column("inbox_events", "attempts")
