"""Where an agent's correction of an AI answer is kept.

Revision ID: 0043_answer_corrections
Revises: 0042_account_contact_channel
Create Date: 2026-09-21

Feature list 7.8 (人工修正回流). Without this there is no way to record "the
AI answered this wrongly" at the moment someone notices, so the knowledge that
would prevent a repeat is never captured - the correction lives in a chat
message and is gone.

Nothing here learns automatically. AGENTS.md prohibits learning from
unreviewed conversations, and a correction is exactly the kind of content that
must pass a person before it becomes something the platform will say. The
lifecycle is therefore recorded -> reviewed -> (dismissed | approved), and only
a reviewed correction is allowed to reach the knowledge base.

The corrected answer is stored as given: it is what a human asserts is right,
which is the whole value of the row, and it is not customer content.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043_answer_corrections"
down_revision: str | None = "0042_account_contact_channel"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "answer_corrections"
APP_ROLE = "platform_app"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("correct_answer", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(31), nullable=False, server_default="pending"),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("reviewed_by", sa.String(255), nullable=True),
        sa.Column("reviewed_at", sa.BigInteger(), nullable=True),
        sa.Column(
            "updated_at",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_index(f"ix_{TABLE}_tenant_status", TABLE, ["tenant_id", "status"])
    op.create_index(f"ix_{TABLE}_tenant_run", TABLE, ["tenant_id", "agent_run_id"])

    # The conventional four. Withholding DELETE was tempting - a correction is
    # evidence that an answer was wrong, and a deleted one looks like one that
    # was never made - but this table is not an append-only ledger: it holds a
    # person's words about a customer's question, and erasure has to remain
    # possible. Restriction belongs to ledgers (audit_events, billing_entries),
    # not here.
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO {APP_ROLE}")

    # RLS, matching the rest of the tenant-owned tables: the application filter
    # is defence in depth, not the mechanism.
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
    op.drop_index(f"ix_{TABLE}_tenant_run", table_name=TABLE)
    op.drop_index(f"ix_{TABLE}_tenant_status", table_name=TABLE)
    op.drop_table(TABLE)
