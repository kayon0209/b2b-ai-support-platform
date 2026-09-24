"""Record ended visitor sessions so their tokens stop working.

Revision ID: 0057_visitor_session_revocation
Revises: 0056_inbox_claim_heartbeat
Create Date: 2026-09-24

`visitor_token` signed a credential with a twelve-hour TTL and nothing else.
There was no way to withdraw one: no jti, no record, no endpoint. A customer who
clicked "end conversation" on a shared machine locked nobody out, and a token
captured from a browser history or a proxy log kept reading and writing that
conversation until the clock ran out on its own. The audit measured the
consequence - reopening with the same `visitor_id` returned the same
conversation and a freshly signed token over it.

The key is (tenant_id, token_jti): one row per withdrawn *credential*.

The first version keyed on (tenant_id, conversation_ref) and that was wrong.
Testing it showed the case customers actually hit: end a session, come back
later, and the newly issued token was rejected too, because the conversation was
still marked revoked. The customer could never return to a chat they had not
deleted. "End" means stop holding this credential; only deletion - which this
platform does not offer - should make returning impossible.

`token_jti` is a uuid minted per token and carried in the signed payload, so it
is not guessable and adds no new secret. `conversation_ref` is still recorded,
for the audit trail: "who ended what" is a question an operator will ask, and a
jti alone cannot answer it.

Tokens minted before this migration carry no jti and are treated as
unwithdrawable, so shipping this does not log every open customer window out at
once. They age out on their own TTL.

`revoked_at` is stored rather than a boolean so retention can delete rows that
can no longer matter: a revocation older than the maximum token lifetime is
unreachable, because the token it withdraws has expired by then. That keeps the
table proportional to live credentials rather than to all traffic.

The unique index is on (tenant_id, token_jti) so a retried end-session request
is a no-op rather than a second row - the customer page retries, double-clicks,
and can fire again from a tab closing mid-flight, and a 500 on the second
attempt would teach the client that ending a session is unreliable.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0057_visitor_session_revocation"
down_revision: str | None = "0056_inbox_claim_heartbeat"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"

_TABLE = """
CREATE TABLE visitor_session_revocations (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    -- Which credential was withdrawn. Nullable so a row can record that a
    -- conversation was closed without naming a token; the withdrawal path
    -- always sets it.
    token_jti uuid,
    -- Not a foreign key: the ref is derived from (tenant, external id) and is
    -- minted by the channel adapter, so it is not necessarily a row in any
    -- single table this migration could point at. Kept for the audit trail.
    conversation_ref uuid NOT NULL,
    revoked_at bigint NOT NULL,
    -- Kept for the audit trail: who ended it. A visitor has no membership and
    -- no actor id, so this is the conversation they held, not a principal.
    ended_by varchar(32) NOT NULL DEFAULT 'visitor'
)
"""


def upgrade() -> None:
    op.execute(_TABLE)
    # The lookup is "is this conversation revoked", always scoped to a tenant -
    # so the unique index and the RLS policy are the same shape.
    # A plain unique index, not a partial one: `ON CONFLICT (tenant_id, token_jti)`
    # needs a constraint it can infer, and a partial index is not one. Postgres
    # allows many NULLs in a unique column anyway, so the rows written without a
    # jti do not collide with each other.
    op.execute(
        "CREATE UNIQUE INDEX uq_visitor_revocation_tenant_jti "
        "ON visitor_session_revocations (tenant_id, token_jti)"
    )
    # Retention: rows older than the longest token lifetime cannot match any
    # token that still verifies, so the sweep has a definition of "expired".
    op.execute(
        "CREATE INDEX ix_visitor_revocation_revoked_at ON visitor_session_revocations (revoked_at)"
    )
    op.execute("ALTER TABLE visitor_session_revocations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE visitor_session_revocations FORCE ROW LEVEL SECURITY")
    # Text comparison rather than a uuid cast: `current_setting(..., true)`
    # returns an empty string rather than NULL on an unbound connection, and a
    # direct cast raises instead of denying - which turns "forgot to bind the
    # tenant" into a 500. 0057 is the same correction 0055 applied elsewhere.
    op.execute(
        "CREATE POLICY tenant_isolation ON visitor_session_revocations "
        "USING (tenant_id::text = NULLIF(current_setting('app.tenant_id', true), '')) "
        "WITH CHECK (tenant_id::text = NULLIF(current_setting('app.tenant_id', true), ''))"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON visitor_session_revocations TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS visitor_session_revocations")
