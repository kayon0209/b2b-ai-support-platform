"""tenant monthly agent-run quota (Phase 5: usage quotas and billing events).

Revision ID: 0023_tenant_run_quota
Revises: 0022_tenant_branding
Create Date: 2026-09-18

Adds the one quota the pilot enforces: agent runs per calendar month. NULL
means unlimited, which is the default, so existing tenants are unaffected by
this revision.

The quota is a business limit, not a security control, so it lives on the
tenant row alongside the other tenant settings rather than in a policy table.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0023_tenant_run_quota"
down_revision: str | None = "0022_tenant_branding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable: unset means unlimited. A non-negative CHECK keeps a negative
    # allowance out of the database, where it would read as "always over".
    op.execute(
        "ALTER TABLE tenants ADD COLUMN monthly_run_quota integer "
        "CHECK (monthly_run_quota IS NULL OR monthly_run_quota >= 0)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE tenants DROP COLUMN monthly_run_quota")
