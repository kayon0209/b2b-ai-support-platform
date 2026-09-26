"""The last mile: a category's operational state.

Revision ID: 0046_issue_categories
Revises: 0045_conversation_contacts
Create Date: 2026-09-22

Feature list 8.1 (漏点分析) and 8.2 (自动化率). Measured: the platform had a
leak analysis by *reason code* (`evaluation/metrics.automation_candidates`) and
a knowledge gap queue by *question* - but nothing between them. The operator's
unit of work is a **kind of question** ("加急咨询" this week, 47 times, 100% to a
person), and there was no object to mark, no state to move, and no way to see
whether a change worked. That is the last mile of the loop.

What is stored here and what is not
-----------------------------------
Only the operator's **decision**. Volume, automation rate and gap attribution are
aggregated from `agent_runs` on read (`evaluation/categories.py`), exactly as
feature 8.7's intent distribution is - so history is included from the moment
the intent snapshot existed, there is no backfill, and there is no second source
of truth for "how many".

The decision has to be persisted because nothing else can reconstruct it: who
marked this category, when, and as what.

`category_key` is a derived opaque string (`{business_line}|{scene}|{kind}`),
not a foreign key: there is no taxonomy table to point at, and inventing one
would make the taxonomy editable, which is how two tenants stop being
comparable.

`automated_at` is what closes the loop: the report measures the category's
automation rate before and after that instant, so "we automated this and it went
0% -> 62%" is a number rather than a claim.
"""

import sqlalchemy as sa
from alembic import op

revision = "0046_issue_categories"
down_revision = "0045_conversation_contacts"
branch_labels = None
depends_on = None

TABLE = "issue_categories"
APP_ROLE = "platform_app"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("category_key", sa.String(191), nullable=False),
        sa.Column("state", sa.String(31), nullable=False, server_default="observed"),
        # Empty until a human says what the gap was.
        sa.Column("fix_type", sa.String(31), nullable=False, server_default=""),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("marked_by", sa.Uuid(), nullable=True),
        sa.Column("marked_at", sa.BigInteger(), nullable=True),
        sa.Column("automated_at", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        # One row per category per tenant: two rows would make "what state is
        # this category in" depend on row order, which is the same coin-flip
        # the alias table and the conversation-contact table refuse.
        sa.UniqueConstraint("tenant_id", "category_key", name="uq_issue_category"),
    )
    op.create_index("ix_issue_categories_state", TABLE, ["tenant_id", "state"])

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO {APP_ROLE}")

    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    # `NULLIF(..., '')` and not a bare cast. `app.tenant_id` is *set* far more
    # often than it is valid - a pooled connection can carry a stale or empty
    # value from a session-level `set_config(..., false)` elsewhere - and a bare
    # `::uuid` on either raises rather than matching nothing. A policy that
    # raises is not fail-closed in the way the table wants: the caller sees a
    # DataError instead of an empty result, and "no rows" never gets asserted.
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {TABLE}
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {TABLE}")
    op.drop_index("ix_issue_categories_state", table_name=TABLE)
    op.drop_table(TABLE)
