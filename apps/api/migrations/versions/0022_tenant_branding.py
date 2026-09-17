"""tenant branding (Phase 5: custom domains and branding).

Revision ID: 0022_tenant_branding
Revises: 0021_invitation_token_fn
Create Date: 2026-09-17

Adds the branding a tenant administrator can set: a display name, a logo URL,
a primary colour and a support contact address.

Why columns on `tenants` and not a separate table: branding is 1:1 with the
tenant and the values are public-facing, so they belong on the tenant row.
`tenants` is global reference data (no RLS) -- it is readable by slug before
any tenant context exists, which is how auth resolution works -- and the API
still scopes every read and write to the caller's server-resolved tenant.

Custom-domain *routing* (Host -> tenant at auth time) is deliberately NOT part
of this revision. Storing a domain without wiring resolution into the request
path would be dead configuration; it needs DNS/TLS and a Host-based resolver,
which is a separate, infrastructure-shaped change.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0022_tenant_branding"
down_revision: str | None = "0021_invitation_token_fn"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable: unset means "use the platform default", not "empty string".
    op.execute("ALTER TABLE tenants ADD COLUMN brand_display_name varchar(255)")
    op.execute("ALTER TABLE tenants ADD COLUMN brand_logo_url varchar(1024)")
    op.execute("ALTER TABLE tenants ADD COLUMN brand_primary_color varchar(31)")
    op.execute("ALTER TABLE tenants ADD COLUMN support_email varchar(255)")


def downgrade() -> None:
    op.execute("ALTER TABLE tenants DROP COLUMN support_email")
    op.execute("ALTER TABLE tenants DROP COLUMN brand_primary_color")
    op.execute("ALTER TABLE tenants DROP COLUMN brand_logo_url")
    op.execute("ALTER TABLE tenants DROP COLUMN brand_display_name")
