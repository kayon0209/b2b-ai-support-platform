"""knowledge_acls table (ticket 15).

Revision ID: 0006_knowledge_acls
Revises: 0005_hybrid_search
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_knowledge_acls"
down_revision: str | None = "0005_hybrid_search"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"


def upgrade() -> None:
    op.create_table(
        "knowledge_acls",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("resource_type", sa.String(31), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("principal_type", sa.String(31), nullable=False),
        sa.Column("principal_id", sa.String(255), nullable=False),
        sa.Column("permission", sa.String(31), nullable=False, server_default="read"),
        sa.UniqueConstraint(
            "resource_type",
            "resource_id",
            "principal_type",
            "principal_id",
            name="uq_acl_entry",
        ),
    )
    op.create_index(
        "ix_knowledge_acls_principal", "knowledge_acls", ["principal_type", "principal_id"]
    )
    op.create_index("ix_knowledge_acls_tenant_id", "knowledge_acls", ["tenant_id"])

    op.execute("ALTER TABLE knowledge_acls ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE knowledge_acls FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON knowledge_acls
        USING (tenant_id::text = current_setting('app.tenant_id', true))
        WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true))
        """
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON knowledge_acls TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON knowledge_acls")
    op.drop_table("knowledge_acls")
