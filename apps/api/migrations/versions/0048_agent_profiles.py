"""The agent directory: who can take work, what they can take, how much.

Revision ID: 0048_agent_profiles
Revises: 0047_canned_replies
Create Date: 2026-09-23

`cases.assignee_ref` existed as a free-form string and `cases/transfer.py` says
the omission out loud: assignment to a *person* is deliberately not modelled.
That is fine for routing to a team and useless for running a queue - there was
no answer to "who is on shift", "who can read PCB", or "who is already at
capacity", so a case either sat unowned or was assigned by hand.

Two decisions:

- **`user_ref` is an opaque string, not a foreign key to `users`.** It is the
  same shape as `cases.assignee_ref`, so the value an assignment writes and the
  value a case stores are identical - no translation layer between "who owns
  this" and "who is this", which is exactly where those two drift apart.
  `users` is also a global table; the tenant link lives in `memberships`.
- **`status`, not deletion.** An agent with open cases cannot disappear, or
  those cases become unowned and unexplainable. Going off shift changes status;
  the record stays.

`skills` is a JSONB tag list matched against a case's business line and team
slug. An empty list means "accepts anything", which is the correct default: a
deployment that never tags anyone must still be able to assign work.
"""

import sqlalchemy as sa
from alembic import op

revision = "0048_agent_profiles"
down_revision = "0047_canned_replies"
branch_labels = None
depends_on = None

TABLE = "agent_profiles"
APP_ROLE = "platform_app"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_ref", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(191), nullable=False),
        sa.Column(
            "skills",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("max_concurrent", sa.BigInteger(), nullable=False, server_default=sa.text("5")),
        sa.Column("status", sa.String(15), nullable=False, server_default="active"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.UniqueConstraint("tenant_id", "user_ref", name="uq_agent_user"),
    )
    op.create_index("ix_agent_profiles_active", TABLE, ["tenant_id", "status"])

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO {APP_ROLE}")

    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    # NULLIF, not a bare cast - see migration 0046 for the reasoning.
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {TABLE}
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {TABLE}")
    op.drop_index("ix_agent_profiles_active", table_name=TABLE)
    op.drop_table(TABLE)
