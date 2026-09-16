"""cases + case_conversations (ticket 21).

Revision ID: 0008_cases_sla
Revises: 0007_agent_runtime
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0008_cases_sla"
down_revision: str | None = "0007_agent_runtime"
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
        "cases",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("enterprise_account_id", sa.Uuid(), nullable=True),
        sa.Column("subject", sa.String(512), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("category", sa.String(63), nullable=False, server_default="general"),
        sa.Column("priority", sa.String(7), nullable=False, server_default="p2"),
        sa.Column("status", sa.String(31), nullable=False, server_default="new"),
        sa.Column("assignee_ref", sa.String(255), nullable=True),
        sa.Column("team_ref", sa.String(255), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("opened_at", sa.BigInteger(), nullable=False),
        sa.Column("first_response_due_at", sa.BigInteger(), nullable=True),
        sa.Column("resolution_due_at", sa.BigInteger(), nullable=True),
        sa.Column("first_responded_at", sa.BigInteger(), nullable=True),
        sa.Column("resolved_at", sa.BigInteger(), nullable=True),
        sa.Column("closed_at", sa.BigInteger(), nullable=True),
        sa.Column("elapsed_running_seconds", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_state_changed_at", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("metadata", JSONB(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_cases_status", "cases", ["status"])
    op.create_index("ix_cases_tenant_id", "cases", ["tenant_id"])
    _tenant_rls("cases")

    op.create_table(
        "case_conversations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), sa.ForeignKey("cases.id"), nullable=False),
        sa.Column("conversation_ref_id", sa.Uuid(), nullable=False),
        sa.Column("relationship", sa.String(31), nullable=False, server_default="origin"),
        sa.UniqueConstraint("case_id", "conversation_ref_id", name="uq_case_conversation"),
    )
    op.create_index("ix_case_conversations_case", "case_conversations", ["case_id"])
    op.create_index("ix_case_conversations_tenant", "case_conversations", ["tenant_id"])
    _tenant_rls("case_conversations")


def downgrade() -> None:
    for table in ("case_conversations", "cases"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.drop_table(table)
