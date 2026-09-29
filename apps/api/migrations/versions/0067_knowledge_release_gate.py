"""Persist fixed-set knowledge release evidence, approvals, tests and rollback events.

Revision ID: 0067_knowledge_release_gate
Revises: 0066_emotion_advice_reviews
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0067_knowledge_release_gate"
down_revision: str | None = "0066_emotion_advice_reviews"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "platform_app"


def _enable_tenant_append_only(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
        "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
    )
    op.execute(f"GRANT SELECT, INSERT ON {table} TO {APP_ROLE}")


def upgrade() -> None:
    # Composite unique keys let release evidence prove every reference belongs
    # to the same tenant at the database boundary.
    op.create_unique_constraint(
        "uq_knowledge_spaces_tenant_id", "knowledge_spaces", ["tenant_id", "id"]
    )
    op.create_unique_constraint(
        "uq_document_versions_tenant_id", "document_versions", ["tenant_id", "id"]
    )
    op.create_unique_constraint(
        "uq_knowledge_drafts_tenant_id", "knowledge_drafts", ["tenant_id", "id"]
    )
    op.add_column("knowledge_drafts", sa.Column("author_id", sa.Uuid(), nullable=True))

    op.create_table(
        "knowledge_release_evaluations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column("knowledge_space_id", sa.Uuid(), nullable=False),
        sa.Column("baseline_version_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_version_id", sa.Uuid(), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=False),
        sa.Column("baseline_run", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("candidate_run", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("candidate_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(15), nullable=False),
        sa.Column("reason_code", sa.String(63), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_knowledge_release_evaluations_tenant_id"),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_knowledge_release_eval_idempotency"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "candidate_version_id",
            name="uq_knowledge_release_candidate_version",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "draft_id",
            "candidate_fingerprint",
            name="uq_knowledge_release_eval_candidate",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "draft_id"],
            ["knowledge_drafts.tenant_id", "knowledge_drafts.id"],
            name="fk_knowledge_release_eval_draft_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "knowledge_space_id"],
            ["knowledge_spaces.tenant_id", "knowledge_spaces.id"],
            name="fk_knowledge_release_eval_space_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "baseline_version_id"],
            ["document_versions.tenant_id", "document_versions.id"],
            name="fk_knowledge_release_eval_baseline_tenant",
        ),
        sa.CheckConstraint(
            "status IN ('eligible', 'blocked')", name="ck_knowledge_release_eval_status"
        ),
    )
    op.create_index(
        "ix_knowledge_release_eval_draft",
        "knowledge_release_evaluations",
        ["tenant_id", "draft_id", "created_at"],
    )
    _enable_tenant_append_only("knowledge_release_evaluations")

    op.create_table(
        "knowledge_release_approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("evaluation_id", sa.Uuid(), nullable=False),
        sa.Column("reviewer_id", sa.Uuid(), nullable=False),
        sa.Column("reviewer_role", sa.String(31), nullable=False),
        sa.Column("candidate_fingerprint", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "evaluation_id"],
            ["knowledge_release_evaluations.tenant_id", "knowledge_release_evaluations.id"],
            name="fk_knowledge_release_approval_eval_tenant",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "evaluation_id",
            "reviewer_id",
            name="uq_knowledge_release_approval_reviewer",
        ),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_knowledge_release_approval_idempotency"
        ),
        sa.CheckConstraint(
            "reviewer_role IN ('knowledge_manager', 'tenant_owner')",
            name="ck_knowledge_release_approval_role",
        ),
    )
    op.create_index(
        "ix_knowledge_release_approval_eval",
        "knowledge_release_approvals",
        ["tenant_id", "evaluation_id"],
    )
    _enable_tenant_append_only("knowledge_release_approvals")

    op.create_table(
        "knowledge_release_post_tests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("evaluation_id", sa.Uuid(), nullable=False),
        sa.Column("run", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(15), nullable=False),
        sa.Column("reason_code", sa.String(63), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "evaluation_id"],
            ["knowledge_release_evaluations.tenant_id", "knowledge_release_evaluations.id"],
            name="fk_knowledge_release_post_test_eval_tenant",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_knowledge_release_post_test_idempotency",
        ),
        sa.CheckConstraint(
            "status IN ('passed', 'blocked')", name="ck_knowledge_release_post_test_status"
        ),
    )
    op.create_index(
        "ix_knowledge_release_post_test_eval",
        "knowledge_release_post_tests",
        ["tenant_id", "evaluation_id", "created_at"],
    )
    _enable_tenant_append_only("knowledge_release_post_tests")

    op.create_table(
        "knowledge_release_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("evaluation_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(31), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("from_version_id", sa.Uuid(), nullable=True),
        sa.Column("to_version_id", sa.Uuid(), nullable=True),
        sa.Column("reason_code", sa.String(63), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "evaluation_id"],
            ["knowledge_release_evaluations.tenant_id", "knowledge_release_evaluations.id"],
            name="fk_knowledge_release_event_eval_tenant",
        ),
        sa.CheckConstraint(
            "action IN ('publish_requested', 'activated', 'post_test_passed', "
            "'rollback_required', 'rollback_completed', 'evaluation_created', "
            "'approval_recorded')",
            name="ck_knowledge_release_event_action",
        ),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_knowledge_release_event_idempotency"
        ),
    )
    op.create_index(
        "ix_knowledge_release_event_eval",
        "knowledge_release_events",
        ["tenant_id", "evaluation_id", "created_at"],
    )
    _enable_tenant_append_only("knowledge_release_events")


def downgrade() -> None:
    for table in (
        "knowledge_release_events",
        "knowledge_release_post_tests",
        "knowledge_release_approvals",
        "knowledge_release_evaluations",
    ):
        op.drop_table(table)
    op.drop_column("knowledge_drafts", "author_id")
    op.drop_constraint("uq_knowledge_drafts_tenant_id", "knowledge_drafts", type_="unique")
    op.drop_constraint("uq_document_versions_tenant_id", "document_versions", type_="unique")
    op.drop_constraint("uq_knowledge_spaces_tenant_id", "knowledge_spaces", type_="unique")
