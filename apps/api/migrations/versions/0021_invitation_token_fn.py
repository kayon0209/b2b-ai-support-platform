"""invitation token resolution function + users grants (Phase 5 identity admin).

Revision ID: 0021_invitation_token_fn
Revises: 0020_membership_invitations
Create Date: 2026-09-17

Two things, both required by the tenant self-service identity API:

1. `accept` runs before the invitee has any tenant context, and
   `membership_invitations` is FORCE RLS, so an unbound read returns nothing.
   `resolve_invitation_by_token(uuid)` is the same narrow, read-only
   SECURITY DEFINER exception used by `resolve_active_membership` (0015) and
   `claim_ingestion_versions` (0018): it can only ever return the single row
   whose opaque token matches, and the grant is EXECUTE only, never SELECT.

2. `platform_app` had no table privileges on `users`. Every other
   tenant-owned table is granted in its own migration, but `users` is global
   reference data and was never granted -- so member listing (a join to
   `users`) and invite acceptance (which creates a `User`) would fail with
   "permission denied for table users" on a database built purely from
   migrations. The grant below closes that gap; it is idempotent on
   environments where the privilege was applied out-of-band.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0021_invitation_token_fn"
down_revision: str | None = "0020_membership_invitations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"

_FUNCTION = """
CREATE OR REPLACE FUNCTION resolve_invitation_by_token(p_token uuid)
RETURNS TABLE (
    id uuid,
    tenant_id uuid,
    email text,
    role text,
    status text,
    expires_at bigint
)
LANGUAGE sql
SECURITY DEFINER
STABLE
SET search_path = pg_catalog, public
AS $$
    SELECT i.id, i.tenant_id, i.email, i.role, i.status, i.expires_at
    FROM public.membership_invitations i
    WHERE i.token = p_token
    LIMIT 1
$$;
"""


def upgrade() -> None:
    op.execute(_FUNCTION)
    op.execute("REVOKE ALL ON FUNCTION resolve_invitation_by_token(uuid) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION resolve_invitation_by_token(uuid) TO {APP_ROLE}")

    # `users` is global reference data (no RLS, no tenant_id). The app role
    # must read it to list members and insert it to accept an invite.
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON users TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON FUNCTION resolve_invitation_by_token(uuid) FROM {APP_ROLE}")
    op.execute("REVOKE ALL ON FUNCTION resolve_invitation_by_token(uuid) FROM PUBLIC")
    op.execute("DROP FUNCTION IF EXISTS resolve_invitation_by_token(uuid)")
    # The `users` grant predates this revision on some environments, so it is
    # deliberately not revoked on the way down (revoking could break auth on a
    # database where it was granted elsewhere).
