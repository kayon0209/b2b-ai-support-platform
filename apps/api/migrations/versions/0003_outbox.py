"""outbox_events table (ticket 7).

Revision ID: 0003_outbox
Revises: 0002_bridge_mappings
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0003_outbox"
down_revision: str | None = "0002_bridge_mappings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"


def upgrade() -> None:
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("event_type", sa.String(127), nullable=False),
        sa.Column("event_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("aggregate_type", sa.String(63), nullable=False),
        sa.Column("aggregate_id", sa.String(255), nullable=False),
        sa.Column("payload", JSONB(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(31), nullable=False, server_default="queued"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("published_at", sa.BigInteger(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("trace_id", sa.String(63), nullable=False, server_default=""),
    )
    op.create_index("ix_outbox_events_status_id", "outbox_events", ["status", "id"])
    op.create_index("ix_outbox_events_tenant_id", "outbox_events", ["tenant_id"])

    op.execute("ALTER TABLE outbox_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE outbox_events FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON outbox_events
        USING (tenant_id::text = current_setting('app.tenant_id', true))
        WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true))
        """
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON outbox_events TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON outbox_events")
    op.drop_table("outbox_events")
