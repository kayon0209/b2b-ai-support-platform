"""Integration Hub tables (ticket 27).

Revision ID: 0010_connectors
Revises: 0009_external_identities
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0010_connectors"
down_revision: str | None = "0009_external_identities"
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
        "connectors",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(63), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(31), nullable=False, server_default="active"),
        sa.Column("capabilities", JSONB(), nullable=False, server_default="[]"),
        sa.Column("configuration", JSONB(), nullable=False, server_default="{}"),
        sa.Column("credential_ref", sa.String(255), nullable=True),
        sa.Column("last_health_at", sa.BigInteger(), nullable=True),
        sa.UniqueConstraint("tenant_id", "provider", "name", name="uq_connector_per_tenant"),
    )
    op.create_index("ix_connectors_provider", "connectors", ["provider"])
    op.create_index("ix_connectors_tenant_id", "connectors", ["tenant_id"])
    _tenant_rls("connectors")

    op.create_table(
        "sync_cursors",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("connector_id", sa.Uuid(), sa.ForeignKey("connectors.id"), nullable=False),
        sa.Column("resource_type", sa.String(63), nullable=False),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("watermark", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.BigInteger(), nullable=False, server_default="0"),
        sa.UniqueConstraint("connector_id", "resource_type", name="uq_cursor_per_resource"),
    )
    op.create_index("ix_sync_cursors_tenant", "sync_cursors", ["tenant_id"])
    _tenant_rls("sync_cursors")

    op.create_table(
        "dead_letter_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("connector_id", sa.Uuid(), sa.ForeignKey("connectors.id"), nullable=True),
        sa.Column("resource_type", sa.String(63), nullable=False),
        sa.Column("operation", sa.String(63), nullable=False),
        sa.Column("operation_digest", sa.String(127), nullable=False),
        sa.Column("error_code", sa.String(63), nullable=False),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(31), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("resolved_at", sa.BigInteger(), nullable=True),
    )
    op.create_index("ix_dead_letter_connector", "dead_letter_items", ["connector_id"])
    op.create_index("ix_dead_letter_tenant", "dead_letter_items", ["tenant_id"])
    _tenant_rls("dead_letter_items")


def downgrade() -> None:
    for table in ("dead_letter_items", "sync_cursors", "connectors"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.drop_table(table)
