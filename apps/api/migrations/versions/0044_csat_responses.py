"""Where a customer's satisfaction score is kept.

Revision ID: 0044_csat_responses
Revises: 0043_answer_corrections
Create Date: 2026-09-22

Feature list 7.10 (满意度回访). Until now the quality dashboard could only
report *indirect* signals - abstention rate, handoff rate, supported
resolution - none of which is the customer saying whether they were helped.
CSAT is the number every support organisation is asked for, and without a table
it does not exist.

Three decisions worth stating:

- **One response per conversation.** Enforced by a unique constraint rather
  than by application logic, because the survey can be answered twice (the
  customer taps the link again, or two messages arrive together) and a
  duplicated score silently double-weights that conversation in every average.
- **`score` is constrained to 1..5 in the database.** A CHECK rather than a
  validation-only path: an average is only meaningful if its inputs are on one
  scale, and an out-of-range row makes every later report wrong in a way nobody
  can see from the dashboard.
- **The comment is optional and free text.** It is customer content, so it gets
  the same minimisation as any other customer text - the row is not a place to
  keep a transcript.

A correction here is a *new* response, not an edit: `updated_at` exists for the
same-response case (a re-tap overwriting its own score), and the unique
constraint is what makes both paths safe.
"""

import sqlalchemy as sa
from alembic import op

revision = "0044_csat_responses"
down_revision = "0043_answer_corrections"
branch_labels = None
depends_on = None

TABLE = "csat_responses"
APP_ROLE = "platform_app"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_ref_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=True),
        sa.Column("agent_run_id", sa.Uuid(), nullable=True),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("channel", sa.String(31), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column(
            "updated_at",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        # One score per conversation - see the module docstring for why this is
        # a constraint and not an application check.
        sa.UniqueConstraint("tenant_id", "conversation_ref_id", name="uq_csat_per_conversation"),
        # Same scale for every row, or the averages are fiction.
        sa.CheckConstraint("score >= 1 AND score <= 5", name="ck_csat_score_range"),
    )
    op.create_index(f"ix_{TABLE}_tenant_created", TABLE, ["tenant_id", "created_at"])
    op.create_index(f"ix_{TABLE}_tenant_score", TABLE, ["tenant_id", "score"])

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
    op.drop_index(f"ix_{TABLE}_tenant_score", table_name=TABLE)
    op.drop_index(f"ix_{TABLE}_tenant_created", table_name=TABLE)
    op.drop_table(TABLE)
