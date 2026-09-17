"""Feature flags with deterministic canary rollout (ticket 40).

Revision ID: 0014_feature_flags
Revises: 0013_knowledge_gaps
Create Date: 2026-09-16

docs/development-plan.md Phase 4: "Feature flags, canary release and
rollback." docs/deployment-and-operations.md step 4 ("deploy application
behind feature flags") and step 6 ("enable for internal tenant, then pilot
tenant canary").

`feature_flags` is the definition; `feature_flag_targets` is a per-tenant
override that can force one tenant in or out of a rollout. Both are
tenant-owned and carry RLS like every other business table (AGENTS.md rule
5).

Note that a flag row's `tenant_id` is the tenant that *owns the definition*,
while `feature_flag_targets.target_tenant_id` is the tenant being evaluated
- they are not the same column and deliberately not the same foreign key.
The target column has no FK to `tenants` because it must be able to name a
tenant this row's RLS policy does not otherwise grant visibility into; only
the owning tenant reads these rows.

`feature_flag_targets.flag_id` cascades on delete so removing a definition
cannot leave orphaned overrides that would resurrect on key reuse.

Expand-migrate-contract: purely additive, so the previous application
version keeps working while this rolls out.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_feature_flags"
down_revision: str | None = "0013_knowledge_gaps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"


def _tenant_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {table}
        USING (tenant_id::text = current_setting('app.tenant_id', true))
        WITH CHECK (tenant_id::text = current_setting('app.tenant_id', true))
        """
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")


def upgrade() -> None:
    op.create_table(
        "feature_flags",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(127), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        # The kill switch: false means off for everyone regardless of rollout.
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        # Percentage of tenants included, evaluated as a stable hash so a
        # tenant's answer never changes between workers or requests.
        sa.Column("rollout_percent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.BigInteger(), nullable=False, server_default="0"),
        sa.UniqueConstraint("tenant_id", "key", name="uq_flag_key_per_tenant"),
        sa.CheckConstraint(
            "rollout_percent >= 0 AND rollout_percent <= 100",
            name="ck_flag_rollout_range",
        ),
    )
    op.create_index("ix_feature_flags_key", "feature_flags", ["key"])

    op.create_table(
        "feature_flag_targets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("flag_id", sa.Uuid(), nullable=False),
        sa.Column("target_tenant_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(
            ["flag_id"], ["feature_flags.id"], name="fk_flag_target_flag", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("flag_id", "target_tenant_id", name="uq_flag_target"),
    )
    op.create_index("ix_flag_targets_flag", "feature_flag_targets", ["flag_id"])
    op.create_index("ix_flag_targets_tenant", "feature_flag_targets", ["target_tenant_id"])

    _tenant_rls("feature_flags")
    _tenant_rls("feature_flag_targets")


def downgrade() -> None:
    # Targets first: it carries the FK to feature_flags.
    op.drop_table("feature_flag_targets")
    op.drop_table("feature_flags")
