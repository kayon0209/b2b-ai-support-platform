"""Contact facts: durable cross-conversation memory (iteration plan 2.5).

Revision ID: 0035_contact_facts
Revises: 0034_conversation_turns
Create Date: 2026-09-19

UNIQUE (tenant_id, contact_ref, key) is the conflict policy: current
statements override history via ON CONFLICT DO UPDATE, because the customer
correcting themselves is the normal case and "latest wins" needs no
arbitration. Values are redacted before they reach this table and only
CUSTOMER-turn facts are ever written - an assistant's own claim must not
become the model's memory.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035_contact_facts"
down_revision: str | None = "0034_conversation_turns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"
TABLE = "contact_facts"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("contact_ref", sa.Uuid(), nullable=False, index=True),
        sa.Column("key", sa.String(63), nullable=False),
        sa.Column("value", sa.String(255), nullable=False),
        sa.Column("source_turn_id", sa.Uuid(), nullable=True),
        sa.Column("confidence", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("updated_at", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("expires_at", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], name="fk_contact_facts_tenant"),
    )
    op.execute(
        f"ALTER TABLE {TABLE} ADD CONSTRAINT uq_contact_fact_key "
        f'UNIQUE (tenant_id, contact_ref, "key")'
    )
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
    op.execute("ALTER TABLE contact_facts DROP CONSTRAINT IF EXISTS uq_contact_fact_key")
    op.drop_table(TABLE)
