"""external_resource_refs + inbox_events (tickets 4-6)

Revision ID: 0002_bridge_mappings
Revises: 0001_identity_rls
Create Date: 2026-09-16

RLS applies to both tables (tenant-owned). inbox_events additionally
deduplicates by UNIQUE(tenant_id, delivery_id).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0002_bridge_mappings"
down_revision: str | None = "0001_identity_rls"
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
        "external_resource_refs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("system", sa.String(31), nullable=False, server_default="chatwoot"),
        sa.Column("resource_type", sa.String(63), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("external_url", sa.Text(), nullable=True),
        sa.Column("source_version", sa.String(63), nullable=True),
        sa.Column("last_synced_at", sa.BigInteger(), nullable=True),
        sa.Column("metadata", JSONB(), nullable=False, server_default="{}"),
        sa.UniqueConstraint(
            "tenant_id", "system", "resource_type", "external_id", name="uq_external_ref"
        ),
    )
    op.create_index(
        "ix_external_resource_refs_resource_type", "external_resource_refs", ["resource_type"]
    )
    op.create_index("ix_external_resource_refs_tenant_id", "external_resource_refs", ["tenant_id"])

    op.create_table(
        "inbox_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("delivery_id", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(127), nullable=False),
        sa.Column("payload_hash", sa.String(127), nullable=False),
        sa.Column("minimized_payload", JSONB(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(31), nullable=False, server_default="received"),
        sa.Column("received_at", sa.BigInteger(), nullable=False),
        sa.Column("processed_at", sa.BigInteger(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "conversation_ref_id",
            sa.Uuid(),
            sa.ForeignKey("external_resource_refs.id"),
            nullable=True,
        ),
        sa.UniqueConstraint("tenant_id", "delivery_id", name="uq_inbox_delivery"),
    )
    op.create_index("ix_inbox_events_status", "inbox_events", ["status"])
    op.create_index("ix_inbox_events_tenant_id", "inbox_events", ["tenant_id"])

    _tenant_rls("external_resource_refs")
    _tenant_rls("inbox_events")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON inbox_events")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON external_resource_refs")
    op.drop_table("inbox_events")
    op.drop_table("external_resource_refs")
