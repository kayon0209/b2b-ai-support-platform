"""external_identities table (ticket 23).

Revision ID: 0009_external_identities
Revises: 0008_cases_sla
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_external_identities"
down_revision: str | None = "0008_cases_sla"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"


def upgrade() -> None:
    op.create_table(
        "external_identities",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("system", sa.String(255), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.UniqueConstraint("system", "subject", name="uq_external_identity_subject"),
    )
    op.create_index("ix_external_identities_subject", "external_identities", ["subject"])
    op.create_index("ix_external_identities_tenant", "external_identities", ["tenant_id"])

    op.execute("ALTER TABLE external_identities ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE external_identities FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON external_identities
        USING (tenant_id::text = current_setting('app.tenant_id', true))
        WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true))
        """
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON external_identities TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON external_identities")
    op.drop_table("external_identities")
