"""Which human a conversation belongs to, so it can be continued elsewhere.

Revision ID: 0045_conversation_contacts
Revises: 0044_csat_responses
Create Date: 2026-09-22

Feature list 1.5 (跨渠道/跨设备续接). Measured: no table in this schema
associated a `conversation_ref_id` with a channel contact. `conversation_turns`
has the turns but not the person, and `conversation_control_leases` has the
lock but not the identity. So a customer who asked on WeChat and then wrote an
email was two unrelated conversations to this platform, and the agent on the
second one could not see the first.

Two decisions worth stating:

- **Keyed on the channel contact, never on the enterprise account.** Colleagues
  share an account. Continuing a conversation across an account boundary would
  show one employee's messages to another - a privacy failure dressed as a
  convenience feature. Contact-to-account binding already answers a different
  question (which contract is this) and is deliberately not reused here.
- **One row per conversation, UNIQUE on `conversation_ref_id`.** A conversation
  belongs to exactly one contact; a second row would make "whose conversation
  is this" depend on row order, which is the same coin-flip the alias table
  refuses. A re-delivered event updates the existing row rather than adding one.

`channel` is nullable because a CRM or channel sync often cannot name one, and
a guessed channel is worse than a blank - the same reasoning as
`enterprise_account_contacts.channel`.
"""

import sqlalchemy as sa
from alembic import op

revision = "0045_conversation_contacts"
down_revision = "0044_csat_responses"
branch_labels = None
depends_on = None

TABLE = "conversation_contacts"
APP_ROLE = "platform_app"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_ref_id", sa.Uuid(), nullable=False),
        sa.Column("external_contact_id", sa.String(255), nullable=False),
        # web | email | wechat | phone | ... Nullable on purpose.
        sa.Column("channel", sa.String(31), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column(
            "updated_at",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        # A conversation has exactly one owner - see the module docstring.
        sa.UniqueConstraint("tenant_id", "conversation_ref_id", name="uq_conversation_contact"),
    )
    # The continuity lookup: "everything else this contact has said".
    op.create_index(f"ix_{TABLE}_tenant_contact", TABLE, ["tenant_id", "external_contact_id"])

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO {APP_ROLE}")

    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {TABLE}
            USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)
        """
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {TABLE}")
    op.drop_index(f"ix_{TABLE}_tenant_contact", table_name=TABLE)
    op.drop_table(TABLE)
