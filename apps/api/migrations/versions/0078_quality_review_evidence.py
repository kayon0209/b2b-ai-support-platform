"""Persist sampled human reviews and immutable versioned evidence.

Revision ID: 0078_quality_reviews
Revises: 0077_resolution_feedback
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0078_quality_reviews"
down_revision: str | None = "0077_resolution_feedback"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"
TABLES = (
    "quality_review_batches",
    "quality_review_items",
    "quality_review_decisions",
    "quality_review_evidence",
)


def _tenant_rls(table: str) -> None:
    op.execute(f"GRANT SELECT, INSERT ON {table} TO {APP_ROLE}")
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
        "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
    )


def upgrade() -> None:
    op.create_table(
        "quality_review_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("seed", sa.String(length=63), nullable=False),
        sa.Column("window_seconds", sa.Integer(), nullable=False),
        sa.Column("requested_size", sa.Integer(), nullable=False),
        sa.Column("target_prompt_version_id", sa.Uuid(), nullable=True),
        sa.Column(
            "population_by_stratum",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("sampler_version", sa.String(length=31), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_quality_review_batches_tenant_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_quality_review_batch_idempotency",
        ),
        sa.ForeignKeyConstraint(
            ["target_prompt_version_id"],
            ["prompt_versions.id"],
            name="fk_quality_review_batches_target_prompt",
        ),
    )
    op.create_index(
        "ix_quality_review_batches_tenant_created",
        "quality_review_batches",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "ix_quality_review_batches_tenant_prompt_created",
        "quality_review_batches",
        ["tenant_id", "target_prompt_version_id", "created_at"],
    )

    op.create_table(
        "quality_review_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_ref_id", sa.Uuid(), nullable=False),
        sa.Column("stratum", sa.String(length=31), nullable=False),
        sa.Column("route", sa.String(length=31), nullable=False),
        sa.Column("run_status", sa.String(length=31), nullable=False),
        sa.Column("prompt_version_id", sa.Uuid(), nullable=True),
        sa.Column("code_version", sa.String(length=63), nullable=False),
        sa.Column("policy_version", sa.String(length=63), nullable=False),
        sa.Column("selected_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "batch_id",
            "agent_run_id",
            name="uq_quality_review_item_decision_fk",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "batch_id"],
            ["quality_review_batches.tenant_id", "quality_review_batches.id"],
            name="fk_quality_review_item_batch_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["agent_run_id"], ["agent_runs.id"], name="fk_quality_review_item_agent_run"
        ),
        sa.ForeignKeyConstraint(
            ["prompt_version_id"], ["prompt_versions.id"], name="fk_quality_review_item_prompt"
        ),
    )
    op.create_index(
        "ix_quality_review_items_tenant_batch",
        "quality_review_items",
        ["tenant_id", "batch_id"],
    )
    op.create_index(
        "ix_quality_review_items_tenant_run",
        "quality_review_items",
        ["tenant_id", "agent_run_id"],
    )
    op.create_index(
        "ix_quality_review_items_prompt_version",
        "quality_review_items",
        ["prompt_version_id"],
    )

    op.create_table(
        "quality_review_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=False),
        sa.Column("reviewer_actor_id", sa.Uuid(), nullable=False),
        sa.Column("verdict", sa.String(length=15), nullable=False),
        sa.Column("reason_code", sa.String(length=31), nullable=True),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("reviewed_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("verdict IN ('agree', 'override')", name="ck_quality_review_verdict"),
        sa.CheckConstraint(
            "(verdict = 'agree' AND reason_code IS NULL) OR "
            "(verdict = 'override' AND reason_code IS NOT NULL)",
            name="ck_quality_review_override_reason",
        ),
        sa.CheckConstraint(
            "reason_code IS NULL OR reason_code IN "
            "('unsupported_claim', 'wrong_route', 'citation_gap', 'unsafe_action', "
            "'task_outcome_mismatch', 'other')",
            name="ck_quality_review_reason_code",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "batch_id",
            "agent_run_id",
            name="uq_quality_review_decision_once",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_quality_review_decision_idempotency",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "batch_id", "agent_run_id"],
            [
                "quality_review_items.tenant_id",
                "quality_review_items.batch_id",
                "quality_review_items.agent_run_id",
            ],
            name="fk_quality_review_decision_selected_item",
        ),
    )
    op.create_index(
        "ix_quality_review_decisions_tenant_batch",
        "quality_review_decisions",
        ["tenant_id", "batch_id"],
    )

    op.create_table(
        "quality_review_evidence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "batch_id", name="uq_quality_review_evidence_batch"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_quality_review_evidence_idempotency",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "batch_id"],
            ["quality_review_batches.tenant_id", "quality_review_batches.id"],
            name="fk_quality_review_evidence_batch_tenant",
        ),
    )
    op.create_index(
        "ix_quality_review_evidence_tenant_created",
        "quality_review_evidence",
        ["tenant_id", "created_at"],
    )

    for table in TABLES:
        _tenant_rls(table)


def downgrade() -> None:
    for table in reversed(TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.drop_table(table)
