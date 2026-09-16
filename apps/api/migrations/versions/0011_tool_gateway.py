"""Tool Gateway table migrations (tickets 29-30).

Revision ID: 0011_tool_gateway
Revises: 0010_connectors
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0011_tool_gateway"
down_revision: str | None = "0010_connectors"
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
        "tool_definitions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),  # NULL = platform catalog
        sa.Column("name", sa.String(127), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("risk", sa.String(31), nullable=False, server_default="read"),
        sa.Column("input_schema", JSONB(), nullable=False, server_default="{}"),
        sa.Column("output_schema", JSONB(), nullable=False, server_default="{}"),
        sa.Column("required_permissions", JSONB(), nullable=False, server_default="[]"),
        sa.Column("timeout_ms", sa.Integer(), nullable=False, server_default="10000"),
        sa.Column("idempotent", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("requires_confirmation", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("tenant_id", "name", "version", name="uq_tool_version"),
    )
    op.create_index("ix_tool_definitions_name", "tool_definitions", ["name"])
    op.execute(
        """CREATE POLICY tenant_isolation ON tool_definitions
           USING (tenant_id IS NULL OR tenant_id::text = current_setting('app.tenant_id', true))
           WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true))"""
    )
    op.execute("ALTER TABLE tool_definitions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tool_definitions FORCE ROW LEVEL SECURITY")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON tool_definitions TO {APP_ROLE}")

    op.create_table(
        "tool_proposals",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "tool_definition_id", sa.Uuid(), sa.ForeignKey("tool_definitions.id"), nullable=False
        ),
        sa.Column("action_hash", sa.String(127), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("sanitized_input", JSONB(), nullable=False, server_default="{}"),
        sa.Column("sanitized_output", JSONB(), nullable=True),
        sa.Column("status", sa.String(31), nullable=False, server_default="proposed"),
        sa.Column("permission_decision", sa.String(31), nullable=False, server_default="pending"),
        sa.Column("permission_reason", sa.String(63), nullable=False, server_default=""),
        sa.Column("required_confirmation", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("expires_at", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("error_code", sa.String(63), nullable=True),
    )
    op.create_index("ix_tool_proposals_status", "tool_proposals", ["status"])
    op.create_index("ix_tool_proposals_action_hash", "tool_proposals", ["action_hash"])
    op.create_index("ix_tool_proposals_tenant", "tool_proposals", ["tenant_id"])
    _tenant_rls("tool_proposals")

    op.create_table(
        "action_confirmations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("proposal_id", sa.Uuid(), sa.ForeignKey("tool_proposals.id"), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("action_hash", sa.String(127), nullable=False),
        sa.Column("scope", sa.String(63), nullable=False, server_default="single_execution"),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("confirmed_at", sa.BigInteger(), nullable=False, server_default="0"),
        sa.UniqueConstraint("proposal_id", "action_hash", name="uq_confirmation_per_action"),
    )
    op.create_index("ix_action_confirmations_proposal", "action_confirmations", ["proposal_id"])
    op.create_index("ix_action_confirmations_tenant", "action_confirmations", ["tenant_id"])
    _tenant_rls("action_confirmations")

    op.create_table(
        "tool_executions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("proposal_id", sa.Uuid(), sa.ForeignKey("tool_proposals.id"), nullable=True),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column(
            "tool_definition_id", sa.Uuid(), sa.ForeignKey("tool_definitions.id"), nullable=False
        ),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("status", sa.String(31), nullable=False, server_default="executing"),
        sa.Column("sanitized_input", JSONB(), nullable=False, server_default="{}"),
        sa.Column("sanitized_output", JSONB(), nullable=True),
        sa.Column("verification_status", sa.String(31), nullable=True),
        sa.Column("started_at", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("completed_at", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.String(63), nullable=True),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_execution_idempotency"),
    )
    op.create_index("ix_tool_executions_status", "tool_executions", ["status"])
    op.create_index("ix_tool_executions_tenant", "tool_executions", ["tenant_id"])
    _tenant_rls("tool_executions")


def downgrade() -> None:
    for table in ("tool_executions", "action_confirmations", "tool_proposals", "tool_definitions"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.drop_table(table)
