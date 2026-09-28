"""Bind operator-started standard flows to conversation tasks.

Revision ID: 0068_standard_flow_instances
Revises: 0067_knowledge_release_gate
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0068_standard_flow_instances"
down_revision: str | None = "0067_knowledge_release_gate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"


def _enable_append_only_rls(table: str) -> None:
    op.execute(f"GRANT SELECT, INSERT ON {table} TO {APP_ROLE}")
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
        "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
    )


def upgrade() -> None:
    op.add_column("conversation_tasks", sa.Column("flow_key", sa.String(63), nullable=True))
    op.add_column("conversation_tasks", sa.Column("flow_version", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_conversation_tasks_flow_binding",
        "conversation_tasks",
        "(flow_key IS NULL AND flow_version IS NULL) OR "
        "(flow_key IS NOT NULL AND flow_version IS NOT NULL AND flow_version > 0)",
    )

    op.create_table(
        "standard_flow_start_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_ref_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "conversation_ref_id",
            "idempotency_key_hash",
            name="uq_standard_flow_start_idempotency",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id", "conversation_ref_id"],
            [
                "conversation_tasks.tenant_id",
                "conversation_tasks.id",
                "conversation_tasks.conversation_ref_id",
            ],
            name="fk_standard_flow_start_task_tenant",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_standard_flow_start_task",
        "standard_flow_start_requests",
        ["tenant_id", "task_id"],
    )
    _enable_append_only_rls("standard_flow_start_requests")


def downgrade() -> None:
    op.drop_table("standard_flow_start_requests")
    op.drop_constraint("ck_conversation_tasks_flow_binding", "conversation_tasks", type_="check")
    op.drop_column("conversation_tasks", "flow_version")
    op.drop_column("conversation_tasks", "flow_key")
