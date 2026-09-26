"""Bind a Chatwoot contact to the tenant's EnterpriseAccount.

Revision ID: 0041_account_contact_bindings
Revises: 0040_case_attachments
Create Date: 2026-09-20

The research report (难点 5) asks for tier-driven routing: a strategic account's
complaint goes 直转专属人工, and the AI only registers and summarises. The SLA
half already existed (`sla_policy_for_tier` over `Case.enterprise_account_id`);
what was missing is the fact the route depends on - *which* account a
conversation belongs to. Nothing in the platform could say.

**Why contact, and why it is not read from the webhook.** An inbox is a channel
(architecture.md puts inboxes with channels, not customers), so binding an inbox
to an account would make every customer in that channel a key account. The
contact is the customer. But the contact id is **not in the webhook payload** -
measured over every stored `inbox_events` row: `contact_id` present 0/29,
`sender_id` 1/29. It is read from the Chatwoot API instead, from the message the
run already fetches.

**`UNIQUE (tenant_id, external_contact_id)`**, not just on the contact: a contact
id is only meaningful within the Chatwoot account that issued it, and one
contact must not be claimable by two of this tenant's accounts - the routing
decision has to be single-valued.

**Composite FK `(enterprise_account_id, tenant_id)`**, matching
`cases.enterprise_account_id` and the parent link on `enterprise_accounts`: a
single-column FK would accept another tenant's account, RLS would hide the row,
and the binding would silently resolve to nothing instead of failing.

**RLS** like every tenant-owned table: `FORCE` as well as `ENABLE`, same
`tenant_isolation` shape, `platform_app` the only grantee.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0041_account_contact_bindings"
down_revision: str | None = "0040_case_attachments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "enterprise_account_contacts"
APP_ROLE = "platform_app"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("enterprise_account_id", sa.Uuid(), nullable=False),
        sa.Column("external_contact_id", sa.String(255), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["enterprise_account_id", "tenant_id"],
            ["enterprise_accounts.id", "enterprise_accounts.tenant_id"],
            name="fk_account_contact_same_tenant",
        ),
        sa.UniqueConstraint("tenant_id", "external_contact_id", name="uq_account_contact_external"),
    )
    op.create_index("ix_account_contacts_account", TABLE, ["tenant_id", "enterprise_account_id"])

    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {TABLE} "
        "USING (tenant_id::text = current_setting('app.tenant_id', true)) "
        "WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true))"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {TABLE}")
    op.execute("DROP INDEX IF EXISTS ix_account_contacts_account")
    op.drop_table(TABLE)
