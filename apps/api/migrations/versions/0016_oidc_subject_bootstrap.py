"""OIDC subject -> identity resolver (the second bootstrap chicken-and-egg).

Revision ID: 0016_oidc_subject_bootstrap
Revises: 0015_membership_bootstrap
Create Date: 2026-09-17

The defect this fixes
---------------------
`MembershipResolver.resolve` (identity/oidc.py) maps a verified token to a
tenant by looking up `external_identities` on `(system, subject)`. That table
is FORCE-RLS'd on the same predicate as everything else:

    USING (tenant_id::text = current_setting('app.tenant_id', true))

So the lookup needs `app.tenant_id` to already be bound in order to find the
row that *tells the platform which tenant to bind*. Measured, not inferred:

    app role, unbound,             external_identities visible = 0
    app role, bound to its tenant, external_identities visible = 0

The second line is the fatal one. Binding from the token is impossible (the
token carries no trusted tenant), and binding from a slug is impossible (OIDC
callers do not present one). There is no ordering of the existing queries that
works, so **the OIDC path could never authenticate anybody** - it returned 401
for every valid token. The bootstrap-token path never hit this because it takes
a slug and can therefore read `tenants` first (migration 0015).

Why a function and not a policy exception
-----------------------------------------
`USING (app.tenant_id IS NULL OR tenant_id = ...)` would expose all tenants to
any unbound connection. `external_identities` maps IdP subjects to users, so
that is a cross-tenant identity disclosure - enumerable with a subject list.

The function is deliberately the narrowest thing that can break the cycle:
it takes the exact `(system, subject)` pair from an *already verified* token,
and returns at most one row. It cannot be enumerated (no wildcard semantics,
no range scans), and it returns only ids and the role - no tenant names, no
user details. Grant is EXECUTE only.

`search_path` is pinned for the same reason as 0015: an unpinned search_path
on a SECURITY DEFINER function lets a caller shadow `public` with a look-alike
schema and choose what the function reads.

Note on `external_identities.system`
------------------------------------
The consumers store the issuer URL in `system` (oidc.py uses `claims["iss"]`),
not a friendly provider name. There is no CHECK constraint pinning the format;
the index below is on the equality pair the lookup actually uses.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0016_oidc_subject_bootstrap"
down_revision: str | None = "0015_membership_bootstrap"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"

_FUNCTION = """
CREATE OR REPLACE FUNCTION resolve_oidc_identity(
    p_system text,
    p_subject text
) RETURNS TABLE (
    tenant_id uuid,
    user_id uuid,
    role text
)
LANGUAGE sql
SECURITY DEFINER
STABLE
SET search_path = pg_catalog, public
AS $$
    SELECT m.tenant_id, m.user_id, m.role
    FROM public.external_identities e
    JOIN public.memberships m
      ON m.tenant_id = e.tenant_id
     AND m.user_id = e.user_id
    JOIN public.tenants t ON t.id = e.tenant_id
    WHERE e.system = p_system
      AND e.subject = p_subject
      AND m.status = 'active'
      AND t.status = 'active'
    LIMIT 1
$$;
"""


def upgrade() -> None:
    op.execute(_FUNCTION)
    op.execute("REVOKE ALL ON FUNCTION resolve_oidc_identity(text, text) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION resolve_oidc_identity(text, text) TO {APP_ROLE}")

    # Migration 0009 declared the uniqueness this lookup relies on as
    # `UNIQUE (system, subject)`. Asserted again here so a future revision that
    # drops it fails this migration rather than silently degrading the lookup
    # to a sequential scan over every tenant's identities.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conrelid = 'public.external_identities'::regclass
                  AND contype = 'u'
                  AND pg_get_constraintdef(oid) = 'UNIQUE (system, subject)'
            ) THEN
                RAISE EXCEPTION
                    'external_identities needs UNIQUE (system, subject) for '
                    'resolve_oidc_identity to stay an index probe';
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("REVOKE ALL ON FUNCTION resolve_oidc_identity(text, text) FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION resolve_oidc_identity(text, text) FROM {APP_ROLE}")
    op.execute("DROP FUNCTION IF EXISTS resolve_oidc_identity(text, text)")
