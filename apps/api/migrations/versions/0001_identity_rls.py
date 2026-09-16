"""tenants, users, memberships, audit_events + RLS

Revision ID: 0001_identity_rls
Revises:
Create Date: 2026-09-16

RLS design (docs/security.md tenant isolation):
- app role `platform_app` has no BYPASSRLS.
- Policies use current_setting('app.tenant_id', true); when unset, nothing
  matches (fail closed).
- Tenants table itself is readable without tenant filter (global reference).
- audit_events are append-only: INSERT/SELECT only, no UPDATE/DELETE grants.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_identity_rls"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"


def _app_role_exists() -> bool:
    bind = op.get_bind()
    row = bind.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": APP_ROLE}
    ).scalar()
    return row is not None


def _create_app_role() -> None:
    if not _app_role_exists():
        op.execute(f"CREATE ROLE {APP_ROLE} NOLOGIN NOBYPASSRLS")
    else:
        op.execute(f"ALTER ROLE {APP_ROLE} NOBYPASSRLS")


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
    _create_app_role()

    op.create_table(
        "tenants",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("slug", sa.String(63), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "status",
            sa.String(31),
            nullable=False,
            server_default="active",
        ),
        sa.Column("default_timezone", sa.String(63), nullable=False, server_default="UTC"),
        sa.Column("data_region", sa.String(31), nullable=False, server_default="cn-north-1"),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("primary_email", sa.String(255), nullable=False, unique=True),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("is_service_account", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.create_table(
        "memberships",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role", sa.String(63), nullable=False),
        sa.Column("status", sa.String(31), nullable=False, server_default="active"),
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_membership_tenant_user"),
    )
    op.create_index("ix_memberships_tenant_id", "memberships", ["tenant_id"])
    op.create_index("ix_memberships_user_id", "memberships", ["user_id"])

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("occurred_at", sa.BigInteger(), nullable=False),
        sa.Column("actor_type", sa.String(31), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(127), nullable=False),
        sa.Column("resource_type", sa.String(63), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=True),
        sa.Column("decision", sa.String(31), nullable=False),
        sa.Column("reason_code", sa.String(63), nullable=False),
        sa.Column("trace_id", sa.String(63), nullable=False),
        sa.Column("before_hash", sa.Text(), nullable=True),
        sa.Column("after_hash", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_audit_events_tenant_id", "audit_events", ["tenant_id"])
    op.create_index("ix_audit_events_occurred_at", "audit_events", ["occurred_at"])
    op.create_index("ix_audit_events_action", "audit_events", ["action"])

    # RLS on tenant-owned tables. tenants is global reference data: readable
    # by slug lookup but not writable through the app role without context.
    _tenant_rls("memberships")
    _tenant_rls("audit_events")

    op.execute(f"GRANT SELECT ON tenants TO {APP_ROLE}")
    op.execute(f"GRANT INSERT, UPDATE ON tenants TO {APP_ROLE}")

    # Append-only guarantee for audit events at the DB level.
    op.execute(f"REVOKE UPDATE, DELETE ON audit_events FROM {APP_ROLE}")


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("memberships")
    op.drop_table("users")
    op.drop_table("tenants")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON memberships")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON audit_events")
