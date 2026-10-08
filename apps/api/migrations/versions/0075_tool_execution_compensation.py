"""Append-only outcomes for approved business compensators.

Revision ID: 0075_tool_exec_comp
Revises: 0074_inbox_retry_budget
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0075_tool_exec_comp"
down_revision: str | None = "0074_inbox_retry_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "tool_execution_compensations"
APP_ROLE = "platform_app"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=63), nullable=False),
        sa.Column("outcome", sa.String(length=31), nullable=False),
        sa.Column("reason_code", sa.String(length=63), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_tool_execution_compensation_idempotency",
        ),
        sa.ForeignKeyConstraint(["execution_id"], ["tool_executions.id"]),
        sa.ForeignKeyConstraint(["proposal_id"], ["tool_proposals.id"]),
    )
    op.create_index(
        "ix_tool_execution_compensations_execution", TABLE, ["tenant_id", "execution_id"]
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
