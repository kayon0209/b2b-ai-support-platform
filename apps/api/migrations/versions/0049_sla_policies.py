"""Tenant-configurable SLA targets per contract tier.

Revision ID: 0049_sla_policies
Revises: 0048_agent_profiles
Create Date: 2026-09-23

`DEFAULT_SLA` and `TIER_TARGET_MULTIPLIERS` lived in `cases/models.py`, so the
only way a tenant could agree a four-hour first response with one customer was
to edit the source and redeploy. In practice nobody did, and every tenant ran on
the same 60-minute clock.

What is configurable, and what is not
-------------------------------------
The two **target minutes** per tier, plus optional priority multipliers.

`running_states` is deliberately **not** exposed. The comment on
`sla_policy_for_tier` already argues why and it has not weakened: which statuses
stop a clock is a property of the support workflow, not of what the customer
bought. Exposing it per tenant would let two customers with the same workflow
pause at different points, and "why did this clock stop" would stop having one
answer.

Absence means "use the code default"
------------------------------------
No seeded rows and no `enabled` column. A tenant with no configuration gets
exactly `sla_policy_for_tier`'s answer, so the fallback is byte-identical rather
than approximately the same - which is also why DELETE is the natural reset:
removing the row restores the default, and there is no second way to express it.

The tier key is free-form rather than an enum: a tenant may agree a tier this
codebase has never heard of, and refusing it here would send them back to
editing source, which is the thing this migration exists to stop.
"""

import sqlalchemy as sa
from alembic import op

revision = "0049_sla_policies"
down_revision = "0048_agent_profiles"
branch_labels = None
depends_on = None

TABLE = "sla_policies"
APP_ROLE = "platform_app"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("tier", sa.String(31), nullable=False),
        sa.Column("first_response_minutes", sa.BigInteger(), nullable=False),
        sa.Column("resolution_minutes", sa.BigInteger(), nullable=False),
        # NULL = use the default multipliers.
        sa.Column("priority_multipliers", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.UniqueConstraint("tenant_id", "tier", name="uq_sla_policy_tier"),
    )
    op.create_index("ix_sla_policies_tenant", TABLE, ["tenant_id"])

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
    op.drop_index("ix_sla_policies_tenant", table_name=TABLE)
    op.drop_table(TABLE)
