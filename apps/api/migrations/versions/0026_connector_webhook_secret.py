"""Connector webhook secret reference + the tenant bootstrap it needs.

Revision ID: 0026_connector_webhook_secret
Revises: 0025_fix_claim_content_type
Create Date: 2026-09-18

Two things, because the second is unusable without the first.

1. `connectors.webhook_secret_ref`
----------------------------------
A connector already has `credential_ref`: the *outbound* credential this
platform presents to the provider. Verifying an inbound webhook needs a
different secret - the one the provider signs with - and they rotate
independently, so overloading one column would mean rotating either breaks
the other. Nullable, so every existing row stays valid and a connector simply
cannot receive webhooks until one is set.

2. `resolve_connector_for_webhook(connector_id)`
------------------------------------------------
The third instance of the bootstrap chicken-and-egg (0015 membership, 0016
OIDC, 0018/0019 ingestion). A webhook arrives with no bearer token - the
signature *is* the authentication - so the tenant must be resolved from the
connector id in the path. But `connectors` is FORCE-RLS'd on

    USING (tenant_id::text = current_setting('app.tenant_id', true))

so an unbound app-role read finds the row only if the tenant is already
bound, and the tenant is what we are trying to discover. Measured for the
same shape on `document_versions`: unbound app role sees 0 rows.

Why a function and not a policy exception
-----------------------------------------
`USING (app.tenant_id IS NULL OR ...)` would expose every tenant's connector
rows to any unbound connection - including `credential_ref`, which is a path
into the secret manager, and `configuration`, which carries provider base
URLs. That is a cross-tenant disclosure reachable from the webhook path
itself, which is the worst place for one.

The function is the narrowest thing that breaks the cycle: it takes the exact
uuid from the path and returns at most one row. It cannot be enumerated
(uuid equality, no wildcard, no range), and it returns only what inbound
verification needs - the tenant, the provider name, the inbound secret
reference and the status. Notably **not** `configuration` and **not**
`credential_ref`: a webhook verifier has no business reading the outbound
credential.

`search_path` is pinned, for the same reason as 0015/0016/0018/0019: an
unpinned search_path on a SECURITY DEFINER function lets a caller shadow
`public` with a look-alike schema and choose what the function reads.

`status` is returned so a `disabled` connector's webhook is refused rather
than accepted and acted on - switching a connector off has to mean its
inbound traffic stops too, not just its outbound calls.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_connector_webhook_secret"
down_revision: str | None = "0025_fix_claim_content_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"

_FUNCTION = """
CREATE OR REPLACE FUNCTION resolve_connector_for_webhook(
    p_connector_id uuid
) RETURNS TABLE (
    tenant_id uuid,
    provider text,
    webhook_secret_ref text,
    status text
)
LANGUAGE sql
SECURITY DEFINER
STABLE
SET search_path = pg_catalog, public
AS $$
    SELECT c.tenant_id, c.provider, c.webhook_secret_ref, c.status
    FROM public.connectors c
    WHERE c.id = p_connector_id
    LIMIT 1
$$;
"""


def upgrade() -> None:
    op.add_column(
        "connectors",
        sa.Column("webhook_secret_ref", sa.String(255), nullable=True),
    )

    op.execute(_FUNCTION)
    op.execute("REVOKE ALL ON FUNCTION resolve_connector_for_webhook(uuid) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION resolve_connector_for_webhook(uuid) TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("REVOKE ALL ON FUNCTION resolve_connector_for_webhook(uuid) FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION resolve_connector_for_webhook(uuid) FROM {APP_ROLE}")
    op.execute("DROP FUNCTION IF EXISTS resolve_connector_for_webhook(uuid)")
    op.drop_column("connectors", "webhook_secret_ref")
