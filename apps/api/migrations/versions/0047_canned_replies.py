"""Reusable agent replies with a shortcut.

Revision ID: 0047_canned_replies
Revises: 0046_issue_categories
Create Date: 2026-09-23

Every mainstream service desk has these and this platform did not. The gap
matters more than the size suggests: an agent who retypes "标准交期 7 天，加急 3
天，最终以报价单为准" fifty times a week will eventually paraphrase it, and fifty
paraphrases of a commercial commitment is exactly how a red line gets said
differently every time - which is the failure `6.1` exists to prevent.

Why it is not in `knowledge_*`
------------------------------
It looks like knowledge - both are stored text an employee reads - but the rules
are opposite. A knowledge document is *evidence*: retrieved, cited, and a
customer answer is built from it. A canned reply is *authored*: nothing retrieves
it, an agent chooses it, and nothing cites it. Storing them together would make
a reply template retrievable, so the retrieval path could start citing one as if
it were a source - the self-reinforcing loop `AGENTS.md` prohibits.

Three decisions:

- **`shortcut` is nullable, not a defaulted empty string.** UNIQUE treats NULLs
  as distinct, so any number of replies may have no shortcut while `/eta` can
  only mean one thing. An empty-string default would make the *second* reply
  without a shortcut fail.
- **Archive, never delete.** A reply that was sent to customers is history;
  removing the row would leave those conversations unexplainable.
- **Scope columns are empty-by-default, and the query treats empty as "any".**
  One reply list must serve every queue; scoping is a narrowing convenience for
  the agent, not a permission boundary (that stays in `platform_policy`).
"""

import sqlalchemy as sa
from alembic import op

revision = "0047_canned_replies"
down_revision = "0046_issue_categories"
branch_labels = None
depends_on = None

TABLE = "canned_replies"
APP_ROLE = "platform_app"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(191), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        # NULL = no shortcut. See the module docstring.
        sa.Column("shortcut", sa.String(63), nullable=True),
        sa.Column("business_line", sa.String(31), nullable=False, server_default=""),
        sa.Column("team_ref", sa.String(63), nullable=False, server_default=""),
        sa.Column("locale", sa.String(15), nullable=False, server_default=""),
        sa.Column("usage_count", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_used_at", sa.BigInteger(), nullable=True),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.UniqueConstraint("tenant_id", "shortcut", name="uq_canned_shortcut"),
    )
    op.create_index("ix_canned_replies_scope", TABLE, ["tenant_id", "business_line", "team_ref"])

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO {APP_ROLE}")

    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    # NULLIF, not a bare cast: `app.tenant_id` is set far more often than it is
    # valid, and a bare `::uuid` on an empty value raises instead of matching no
    # rows - see migration 0046 for the full reasoning.
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {TABLE}
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {TABLE}")
    op.drop_index("ix_canned_replies_scope", table_name=TABLE)
    op.drop_table(TABLE)
