"""Store independently signed release evaluation provenance.

Revision ID: 0069_signed_release_evidence
Revises: 0068_standard_flow_instances
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0069_signed_release_evidence"
down_revision: str | None = "0068_standard_flow_instances"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "knowledge_release_evaluations",
        sa.Column("attestation_json", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "knowledge_release_evaluations",
        sa.Column("attestation_sha256", sa.String(64), nullable=True),
    )
    op.create_unique_constraint(
        "uq_knowledge_release_eval_attestation",
        "knowledge_release_evaluations",
        ["tenant_id", "attestation_sha256"],
    )
    op.add_column(
        "knowledge_release_post_tests",
        sa.Column("attestation_json", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "knowledge_release_post_tests",
        sa.Column("attestation_sha256", sa.String(64), nullable=True),
    )
    op.create_unique_constraint(
        "uq_knowledge_release_post_test_attestation",
        "knowledge_release_post_tests",
        ["tenant_id", "attestation_sha256"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_knowledge_release_post_test_attestation",
        "knowledge_release_post_tests",
        type_="unique",
    )
    op.drop_column("knowledge_release_post_tests", "attestation_sha256")
    op.drop_column("knowledge_release_post_tests", "attestation_json")
    op.drop_constraint(
        "uq_knowledge_release_eval_attestation",
        "knowledge_release_evaluations",
        type_="unique",
    )
    op.drop_column("knowledge_release_evaluations", "attestation_sha256")
    op.drop_column("knowledge_release_evaluations", "attestation_json")
