"""Persist idempotency receipts for Workbench task commands.

Revision ID: 0071_task_command_idempotency
Revises: 0070_outbox_processing_fence
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0071_task_command_idempotency"
down_revision: str | None = "0070_outbox_processing_fence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversation_task_events",
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "conversation_task_events",
        sa.Column("request_hash", sa.String(length=64), nullable=True),
    )
    op.create_unique_constraint(
        "uq_conversation_task_event_idempotency",
        "conversation_task_events",
        ["tenant_id", "task_id", "idempotency_key_hash"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_conversation_task_event_idempotency",
        "conversation_task_events",
        type_="unique",
    )
    op.drop_column("conversation_task_events", "request_hash")
    op.drop_column("conversation_task_events", "idempotency_key_hash")
