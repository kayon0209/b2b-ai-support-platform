"""Conversation turns: redacted multi-turn memory (iteration plan 2.1).

Revision ID: 0034_conversation_turns
Revises: 0033_retrieval_multipath
Create Date: 2026-09-19

Memory has to read the WORDS of prior turns to resolve anaphora ("how about
monthly?" needs "annual plan" from the turn before), so a hash-only store
cannot work. But docs/security.md gives raw customer content exactly one
home: Chatwoot. The compromise this table encodes is `text_redacted` - the
sentence with PII-shaped values masked by `evaluation.pii.redact_text` at
the write boundary - plus `text_hash`, an integrity anchor over the
original bytes.

Retention: rows are pruned after APP_CONVERSATION_TURN_DAYS (registered in
`evaluation/pii.py::RetentionPolicy`); Chatwoot stays the system of record
and the live fetch (plan 2.2) rebuilds what this store dropped.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_conversation_turns"
down_revision: str | None = "0033_retrieval_multipath"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"
TABLE = "conversation_turns"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_ref_id", sa.Uuid(), nullable=False, index=True),
        sa.Column("role", sa.String(15), nullable=False),
        sa.Column("text_redacted", sa.Text(), nullable=False),
        sa.Column("text_hash", sa.String(127), nullable=False),
        sa.Column("ts", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("ref", sa.String(127), nullable=False, server_default=""),
        sa.Column("source", sa.String(15), nullable=False, server_default="platform"),
        sa.Column("created_at", sa.BigInteger(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_conversation_turns_tenant",
        ),
    )
    # The read pattern is "latest N turns of one conversation": the composite
    # index serves it directly, newest first.
    op.execute(
        f"CREATE INDEX ix_turns_conversation_ts ON {TABLE} "
        "(tenant_id, conversation_ref_id, ts DESC)"
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
    op.execute("DROP INDEX IF EXISTS ix_turns_conversation_ts")
    op.drop_table(TABLE)
