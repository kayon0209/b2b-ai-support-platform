"""membership bootstrap resolver function (auth resolution fix).

Revision ID: 0015_membership_bootstrap
Revises: 0014_feature_flags
Create Date: 2026-09-17

The RLS bootstrap problem
-------------------------
`memberships` is FORCE RLS: a row is visible only inside a transaction that
has already bound `app.tenant_id` to its tenant. But auth resolution is the
step that *discovers* the tenant, so it runs before any context exists.

Ordering the lookup so `tenants` is read first (no RLS, global reference
data) solves the tenant half, but not the membership half from a *bare*
connection: `resolve_by_slug` binds the tenant it just read and then queries
`memberships` in the same transaction. That works, and is what the API uses.

What this function adds is the same read for callers that cannot afford a
two-step round trip and, more importantly, a single auditable place where
"read one membership by (slug, user)" is expressed. Keeping it as a function
means the exception is narrow and reviewable: it is read-only, it takes both
the tenant and the user, and it returns at most the one row that matches.

Why SECURITY DEFINER and not a policy exception
-----------------------------------------------
A policy exception such as `USING (app.tenant_id IS NULL OR tenant_id = ...)`
would grant *table-wide* read access to every tenant for any unbound
connection. That is a real cross-tenant leak, and the isolation tests in
`test_cross_tenant_leak_surfaces.py` exist precisely to fail on it.

A SECURITY DEFINER function scoped to a single equality lookup does not open
the table: it can only ever return the membership row for the exact
(slug, user_id) pair it was called with. The owner is the migration role, so
RLS does not filter it; the grant is EXECUTE only, never SELECT.

`search_path` is pinned to defeat the classic SECURITY DEFINER hijack, where
a caller prefixes a schema containing a look-alike `tenants` table.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015_membership_bootstrap"
down_revision: str | None = "0014_feature_flags"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"

_FUNCTION = """
CREATE OR REPLACE FUNCTION resolve_active_membership(
    p_tenant_slug text,
    p_user_id uuid
) RETURNS TABLE (
    tenant_id uuid,
    user_id uuid,
    role text,
    status text
)
LANGUAGE sql
SECURITY DEFINER
STABLE
SET search_path = pg_catalog, public
AS $$
    SELECT m.tenant_id, m.user_id, m.role, m.status
    FROM public.memberships m
    JOIN public.tenants t ON t.id = m.tenant_id
    WHERE t.slug = p_tenant_slug
      AND t.status = 'active'
      AND m.user_id = p_user_id
      AND m.status = 'active'
    LIMIT 1
$$;
"""


def upgrade() -> None:
    op.execute(_FUNCTION)
    # EXECUTE only. The app role still cannot SELECT from `memberships`
    # without binding app.tenant_id, so the table-wide leak this function
    # could have introduced does not exist.
    op.execute("REVOKE ALL ON FUNCTION resolve_active_membership(text, uuid) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION resolve_active_membership(text, uuid) TO {APP_ROLE}")

    # The lookup is (slug, user_id) -> one row; both sides are indexed
    # already (tenants.slug unique, memberships.user_id). This composite
    # index lets the function satisfy tenant + user in one probe instead of
    # scanning every membership of the user.
    op.create_index(
        "ix_memberships_tenant_user_active",
        "memberships",
        ["tenant_id", "user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_memberships_tenant_user_active", table_name="memberships")
    op.execute("REVOKE ALL ON FUNCTION resolve_active_membership(text, uuid) FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION resolve_active_membership(text, uuid) FROM {APP_ROLE}")
    op.execute("DROP FUNCTION IF EXISTS resolve_active_membership(text, uuid)")
