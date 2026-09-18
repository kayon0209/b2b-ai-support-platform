"""SAML connections and SCIM provisioning tokens, plus their two resolvers.

Revision ID: 0030_saml_scim
Revises: 0029_case_escalations
Create Date: 2026-09-18

`docs/development-plan.md` Phase 5: "SAML and SCIM". `docs/integrations.md`
describes SCIM as "group-to-role/department mapping", which is exactly what
`scim_tokens` and the Department table added in 0028 make possible.

Three tables, each with a reason it is not something that already exists.

**`saml_connections` is per tenant, not global.** An IdP signing certificate,
an entity id and an SSO URL belong to the tenant's own identity provider. A
deployment-wide setting would mean two tenants cannot use two IdPs, which is
the first thing an enterprise asks for. `idp_certificate` holds the **public**
certificate only: the platform verifies with it and has no use for a private
key, so storing one would be a liability with no feature attached.

**`saml_consumed_assertions` is the replay guard.** A SAML assertion is a
bearer credential with an expiry, and an intercepted one is replayable for its
whole validity window unless the relying party remembers what it has already
accepted. `UNIQUE (connection_id, assertion_id)` makes a second presentation
unrepresentable rather than merely unlikely - the same mechanism as
`uq_inbox_delivery`, and for the same reason: a check-then-insert loses the race
and a constraint does not.

It is scoped by connection rather than by tenant because assertion ids are the
IdP's to allocate: two IdPs may legitimately issue the same id, and a
tenant-wide unique key would reject a valid second IdP's assertion as a replay.

**`scim_tokens` stores a hash, never the token.** The token is a bearer
credential for the whole tenant - it can create users and move them between
departments - and it is presented on every provisioning request, so it would
otherwise sit in every log and every backup. `token_hash` is `UNIQUE` globally
because the lookup happens **before** any tenant is known: the token is what
establishes the tenant, and a per-tenant unique key would require knowing the
tenant first. `revoked_at` is a timestamp rather than a delete, so "when did
this token stop working" survives.

Two more instances of the RLS bootstrap (0015, 0016, 0018, 0019, 0026, 0027):
SAML's ACS endpoint and every SCIM request must resolve a tenant **before** any
binding exists, and both tables are FORCE-RLS'd on the binding being resolved.
Same narrow shape - pinned `search_path`, EXECUTE to the app role only, narrow
projections. `resolve_scim_token` returns the tenant and the token id and
nothing else; `resolve_saml_connection` returns what the ACS handler needs to
build a verifier, and deliberately **not** the tenant's other configuration.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_saml_scim"
down_revision: str | None = "0029_case_escalations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"

SAML_CONNECTION = """
CREATE OR REPLACE FUNCTION resolve_saml_connection(p_connection_id uuid)
RETURNS TABLE (
    tenant_id uuid,
    idp_entity_id text,
    idp_sso_url text,
    idp_certificate text,
    sp_entity_id text,
    status text
)
LANGUAGE sql
SECURITY DEFINER
STABLE
SET search_path = pg_catalog, public
AS $$
    SELECT c.tenant_id, c.idp_entity_id, c.idp_sso_url, c.idp_certificate,
           c.sp_entity_id, c.status
    FROM public.saml_connections c
    WHERE c.id = p_connection_id
    LIMIT 1
$$;
"""

SCIM_RESOLVER = """
CREATE OR REPLACE FUNCTION resolve_scim_token(p_token_hash text)
RETURNS TABLE (
    tenant_id uuid,
    token_id uuid
)
LANGUAGE sql
SECURITY DEFINER
STABLE
SET search_path = pg_catalog, public
AS $$
    SELECT t.tenant_id, t.id
    FROM public.scim_tokens t
    WHERE t.token_hash = p_token_hash
      AND t.revoked_at IS NULL
    LIMIT 1
$$;
"""

_RLS = """
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON {table}
    USING (tenant_id::text = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true));
"""


def upgrade() -> None:
    op.create_table(
        "saml_connections",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(63), nullable=False),
        sa.Column("idp_entity_id", sa.String(512), nullable=False),
        sa.Column("idp_sso_url", sa.String(1024), nullable=False),
        # PEM, public certificate only. `text` rather than a bounded varchar
        # because a PEM bundle with its own line breaks is easy to get wrong at
        # a fixed width.
        sa.Column("idp_certificate", sa.Text(), nullable=False),
        sa.Column("sp_entity_id", sa.String(512), nullable=False),
        sa.Column("status", sa.String(31), nullable=False, server_default="active"),
        sa.Column("created_at", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False, server_default="0"),
        sa.CheckConstraint("status IN ('active','disabled')", name="ck_saml_connection_status"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_saml_connection_name"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_saml_connections_id_tenant"),
    )
    op.create_index("ix_saml_connections_tenant", "saml_connections", ["tenant_id"])

    op.create_table(
        "saml_consumed_assertions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("assertion_id", sa.String(255), nullable=False),
        sa.Column("consumed_at", sa.BigInteger(), nullable=False, server_default="0"),
        # The replay guard. Named so the handler can recognise the violation it
        # expects and answer with a clear "already used" rather than a 500.
        sa.UniqueConstraint("connection_id", "assertion_id", name="uq_saml_assertion_once"),
        sa.ForeignKeyConstraint(
            ["connection_id", "tenant_id"],
            ["saml_connections.id", "saml_connections.tenant_id"],
            name="fk_saml_assertion_same_tenant",
        ),
    )
    op.create_index("ix_saml_assertions_connection", "saml_consumed_assertions", ["connection_id"])

    op.create_table(
        "scim_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(63), nullable=False),
        # SHA-256 of the presented token. Not bcrypt: this is a high-entropy
        # random string, not a password, so there is nothing to slow down - and
        # a per-request KDF on the provisioning path would be a latency cost for
        # no security gain.
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False, server_default="0"),
        # A timestamp rather than a delete: "when did this stop working"
        # survives, and a revoked token cannot be quietly resurrected by
        # re-inserting the same hash.
        sa.Column("revoked_at", sa.BigInteger(), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_scim_token_hash"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_scim_token_name"),
    )
    op.create_index("ix_scim_tokens_tenant", "scim_tokens", ["tenant_id"])

    for table in ("saml_connections", "saml_consumed_assertions", "scim_tokens"):
        op.execute(_RLS.format(table=table))
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")

    op.execute(SAML_CONNECTION)
    op.execute("REVOKE ALL ON FUNCTION resolve_saml_connection(uuid) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION resolve_saml_connection(uuid) TO {APP_ROLE}")

    op.execute(SCIM_RESOLVER)
    op.execute("REVOKE ALL ON FUNCTION resolve_scim_token(text) FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION resolve_scim_token(text) TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("REVOKE ALL ON FUNCTION resolve_scim_token(text) FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION resolve_scim_token(text) FROM {APP_ROLE}")
    op.execute("DROP FUNCTION IF EXISTS resolve_scim_token(text)")

    op.execute("REVOKE ALL ON FUNCTION resolve_saml_connection(uuid) FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION resolve_saml_connection(uuid) FROM {APP_ROLE}")
    op.execute("DROP FUNCTION IF EXISTS resolve_saml_connection(uuid)")

    for table, indexes in (
        ("scim_tokens", ["ix_scim_tokens_tenant"]),
        ("saml_consumed_assertions", ["ix_saml_assertions_connection"]),
        ("saml_connections", ["ix_saml_connections_tenant"]),
    ):
        op.execute(f"REVOKE ALL ON {table} FROM {APP_ROLE}")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        for index in indexes:
            op.drop_index(index, table_name=table)
        op.drop_table(table)
