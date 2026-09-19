"""Resolver for reading one conversation turn's redacted text by id.

Revision ID: 0037_turn_text_resolver
Revises: 0036_tool_citations

Why a function and not a plain SELECT
-------------------------------------
The worker reads a customer's question outside any HTTP request, so there is
no `app.tenant_id` binding in effect. Under FORCE RLS that means a direct
SELECT on `conversation_turns` returns nothing — silently, without error —
which is exactly how platform-originated questions ended up queued and never
answered.

This is the same bootstrap shape as the other resolvers in this schema
(0015 membership, 0016 OIDC, 0026 connector webhook, 0032 Chatwoot tenant):
a narrow SECURITY DEFINER function that takes one identifier and returns one
value. It is callable only by `platform_app`, equality-only, no wildcards, so
it is not an enumeration surface.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0037_turn_text_resolver"
down_revision: str | None = "0036_tool_citations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

APP_ROLE = "platform_app"

_FUNCTION = """
CREATE OR REPLACE FUNCTION resolve_turn_text(
    p_turn_id uuid
) RETURNS text
LANGUAGE sql
SECURITY DEFINER
STABLE
SET search_path = pg_catalog, public
AS $$
    SELECT t.text_redacted
    FROM public.conversation_turns t
    WHERE t.id = p_turn_id
    LIMIT 1
$$;
"""


def upgrade() -> None:
    op.execute(_FUNCTION)
    op.execute("REVOKE ALL ON FUNCTION resolve_turn_text(uuid) FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION resolve_turn_text(uuid) FROM {APP_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION resolve_turn_text(uuid) TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS resolve_turn_text(uuid)")
