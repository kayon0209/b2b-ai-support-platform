"""A/B experiments: a declarative arm assignment that something actually uses.

Revision ID: 0052_ab_experiments
Revises: 0051_turn_origin
Create Date: 2026-09-23

`evaluation/ab.py` has had a correct, deterministic, well-tested bucketer since
feature 8.6 was written - and **nothing outside its own tests ever called it**.
That is the same defect this repository keeps finding, one level up: an
experiment nobody runs is a comparison nobody can read.

What an arm *changes* here is the **prompt version**. That is the lever an
operator actually wants to test ("does the shorter draft resolve more?"), and it
is the one the platform can vary per run without a deploy: `prompt_versions` is
already immutable and versioned, and the orchestrator already records which
version produced an answer.

`prompt_version_id` is nullable, and a variant that omits it is a legitimate
arm: "current behaviour" is the control. Requiring a row for the control would
force an operator to publish a duplicate of the live prompt just to compare
against it.

Weights are relative, matching `ab.Variant` - "2 to 1" and "0.67 to 0.33" behave
identically, so an unequal split does not require the numbers to sum to anything
in particular.

Assignment is **deterministic per conversation**, not per request: a unit that
switches arms mid-experiment contributes to both arms and to neither, which is
the property `ab.assign_variant` exists to guarantee. The chosen arm is recorded
on the run's `model_config`, so the results endpoint reads history rather than
re-deriving it - re-deriving would silently re-bucket every past run if a weight
ever changed.
"""

import sqlalchemy as sa
from alembic import op

revision = "0052_ab_experiments"
down_revision = "0051_turn_origin"
branch_labels = None
depends_on = None

TABLE = "ab_experiments"
APP_ROLE = "platform_app"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(127), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        # [{"name": "control", "weight": 50, "prompt_version_id": null}, ...]
        sa.Column("variants", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.UniqueConstraint("tenant_id", "key", name="uq_ab_experiment_key"),
    )
    op.create_index("ix_ab_experiments_tenant", TABLE, ["tenant_id", "enabled"])

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO {APP_ROLE}")

    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    # NULLIF, not a bare cast - see migration 0046.
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {TABLE}
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {TABLE}")
    op.drop_index("ix_ab_experiments_tenant", table_name=TABLE)
    op.drop_table(TABLE)
