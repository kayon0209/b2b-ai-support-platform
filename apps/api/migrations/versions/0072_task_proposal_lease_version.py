"""Bind pending task proposals to the lease version that prepared them.

Revision ID: 0072_task_proposal_lease_version
Revises: 0071_task_command_idempotency
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0072_task_proposal_lease_version"
down_revision: str | None = "0071_task_command_idempotency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable for existing rows. A pending legacy proposal without a recorded
    # lease version fails closed at confirmation/execution and must be prepared
    # again by the current human owner.
    op.add_column(
        "conversation_tasks",
        sa.Column("proposal_lease_version", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_conversation_tasks_proposal",
        "conversation_tasks",
        ["tenant_id", "proposal_id"],
        postgresql_where=sa.text("proposal_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_conversation_tasks_proposal", table_name="conversation_tasks")
    op.drop_column("conversation_tasks", "proposal_lease_version")
