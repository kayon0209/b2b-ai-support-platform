"""Tenant custom domains, and the Host -> tenant resolver they need.

Revision ID: 0027_tenant_domains
Revises: 0026_connector_webhook_secret
Create Date: 2026-09-18

Migration 0022 stored tenant branding and deferred routing, on the grounds that
storing a domain without wiring resolution would be dead configuration. This is
the routing half.

Three decisions, each of which is a security property rather than a preference.

**A separate table, not a column on `tenants`.** A tenant legitimately has more
than one host - an apex and a `www` - and each claim has its own verification
state. A column would force "one domain per tenant", which is not the shape of
the problem.

**Domain uniqueness is global, not per tenant.** If two tenants could register
the same host, the Host -> tenant mapping would be ambiguous and the answer
would depend on row order: one tenant's branded page could be served for
another tenant's domain. A `UNIQUE (domain)` constraint makes that
unrepresentable rather than unlikely.

**Only *verified* domains resolve.** This is the load-bearing one. Anyone can
send a request with any Host header; if an unverified claim resolved, a tenant
could claim `example-bank.com`, publish nothing, and have the platform serve
*their* branding (and their help content) on a domain they do not own. So the
resolver filters on `verified_at IS NOT NULL`, and the API cannot mark a claim
verified by itself.

`CHECK (domain = lower(domain))` is deliberately structural. Normalising in the
application layer only would let a raw insert or a future code path store
`Example.COM`, and then two rows would describe the same host while `UNIQUE`
saw two different values.

The resolver is the sixth instance of the RLS bootstrap (0015, 0016, 0018,
0019, 0026): `tenant_domains` is FORCE-RLS'd on a binding that does not exist
yet, because resolving the tenant is the point. Same narrow shape, pinned
`search_path`, EXECUTE to the app role only, and it returns a single uuid -
nothing about the tenant, nothing about its configuration.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_tenant_domains"
down_revision: str | None = "0026_connector_webhook_secret"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"

_RESOLVER = """
CREATE OR REPLACE FUNCTION resolve_domain_tenant(p_domain text)
RETURNS uuid
LANGUAGE sql
SECURITY DEFINER
STABLE
SET search_path = pg_catalog, public
AS $$
    SELECT d.tenant_id
    FROM public.tenant_domains d
    JOIN public.tenants t ON t.id = d.tenant_id
    WHERE lower(d.domain) = lower(p_domain)
      AND d.verified_at IS NOT NULL
      AND t.status = 'active'
    LIMIT 1
$$;
"""


def upgrade() -> None:
    op.create_table(
        "tenant_domains",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("domain", sa.String(255), nullable=False),
        sa.Column("verification_token", sa.String(64), nullable=False),
        sa.Column("verified_at", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("domain = lower(domain)", name="ck_tenant_domain_lowercase"),
        sa.UniqueConstraint("domain", name="uq_tenant_domain"),
    )
    op.create_index("ix_tenant_domains_tenant", "tenant_domains", ["tenant_id"])

    op.execute("ALTER TABLE tenant_domains ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant_domains FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON tenant_domains "
        "USING (tenant_id::text = current_setting('app.tenant_id', true)) "
        "WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true))"
    )

    # Table privileges, matching every other tenant-owned table migration.
    # Omitting this is not a harmless oversight: the table exists, RLS is
    # configured, the migration applies cleanly, and the *first request* fails
    # with `permission denied for table tenant_domains` - on a fresh database
    # only, because an established one may have the grant out of band. That is
    # the same shape as the `users` grant added in 0021, and it is why
    # `test_schema_privileges.py` now checks every table.
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_domains TO {APP_ROLE}")

    op.execute(_RESOLVER)
    op.execute("REVOKE ALL ON FUNCTION resolve_domain_tenant(text) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION resolve_domain_tenant(text) TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("REVOKE ALL ON FUNCTION resolve_domain_tenant(text) FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION resolve_domain_tenant(text) FROM {APP_ROLE}")
    op.execute("DROP FUNCTION IF EXISTS resolve_domain_tenant(text)")
    op.execute(f"REVOKE ALL ON tenant_domains FROM {APP_ROLE}")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON tenant_domains")
    op.drop_index("ix_tenant_domains_tenant", table_name="tenant_domains")
    op.drop_table("tenant_domains")
