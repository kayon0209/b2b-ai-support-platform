"""Chatwoot account -> tenant resolver, and an app-role ingest path.

Revision ID: 0032_chatwoot_tenant_resolver
Revises: 0031_billing_ledger
Create Date: 2026-09-18

The Chatwoot webhook wrote its InboxEvent through the bootstrap owner
session - a superuser with BYPASSRLS. The tenant binding applied with
`set_config` was therefore the ONLY isolation on the path, and a logic error
in the resolver or the payload handling would not be contained by RLS. Every
interactive request path already connects as `platform_app`; the ingest path
did not because it has a bootstrap chicken-and-egg: the row that names the
tenant is itself FORCE-RLS'd, so an unbound app-role read finds nothing.

The resolver function breaks the cycle (0015 membership, 0016 OIDC, 0026
connector webhook are the prior instances). It takes the exact Chatwoot
account id from the *signature-verified* payload - never reachable without a
valid HMAC - and returns at most one uuid. Equality only: no wildcard, no
range, so nothing is enumerable, and it returns nothing but the tenant id.

With the function in place the endpoint can run entirely as `platform_app`:
resolve through the function, bind `app.tenant_id`, then let RLS hold on the
INSERT the same way it does everywhere else.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0032_chatwoot_tenant_resolver"
down_revision: str | None = "0031_billing_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"

_FUNCTION = """
CREATE OR REPLACE FUNCTION resolve_chatwoot_tenant(
    p_account_id text
) RETURNS uuid
LANGUAGE sql
SECURITY DEFINER
STABLE
SET search_path = pg_catalog, public
AS $$
    SELECT r.tenant_id
    FROM public.external_resource_refs r
    WHERE r.system = 'chatwoot'
      AND r.resource_type = 'account'
      AND r.external_id = p_account_id
    LIMIT 1
$$;
"""


def upgrade() -> None:
    op.execute(_FUNCTION)
    op.execute("REVOKE ALL ON FUNCTION resolve_chatwoot_tenant(text) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION resolve_chatwoot_tenant(text) TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("REVOKE ALL ON FUNCTION resolve_chatwoot_tenant(text) FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION resolve_chatwoot_tenant(text) FROM {APP_ROLE}")
    op.execute("DROP FUNCTION IF EXISTS resolve_chatwoot_tenant(text)")
