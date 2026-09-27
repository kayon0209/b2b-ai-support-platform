"""Append-only evaluation, approval, post-test, and rollback evidence."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from platform_core.orm_base import Base, PkMixin, TenantMixin


class KnowledgeReleaseEvaluation(Base, PkMixin, TenantMixin):
    """A trusted worker's immutable pre-publish comparison for one draft."""

    __tablename__ = "knowledge_release_evaluations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_knowledge_release_evaluations_tenant_id"),
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_knowledge_release_eval_idempotency"
        ),
        UniqueConstraint(
            "tenant_id", "candidate_version_id", name="uq_knowledge_release_candidate_version"
        ),
        UniqueConstraint(
            "tenant_id",
            "draft_id",
            "candidate_fingerprint",
            name="uq_knowledge_release_eval_candidate",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "draft_id"],
            ["knowledge_drafts.tenant_id", "knowledge_drafts.id"],
            name="fk_knowledge_release_eval_draft_tenant",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "knowledge_space_id"],
            ["knowledge_spaces.tenant_id", "knowledge_spaces.id"],
            name="fk_knowledge_release_eval_space_tenant",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "baseline_version_id"],
            ["document_versions.tenant_id", "document_versions.id"],
            name="fk_knowledge_release_eval_baseline_tenant",
        ),
        CheckConstraint(
            "status IN ('eligible', 'blocked')", name="ck_knowledge_release_eval_status"
        ),
        Index("ix_knowledge_release_eval_draft", "tenant_id", "draft_id", "created_at"),
    )

    draft_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    knowledge_space_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    baseline_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    candidate_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    author_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    baseline_run: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    candidate_run: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    candidate_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(15), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(63), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class KnowledgeReleaseApproval(Base, PkMixin, TenantMixin):
    """One immutable human approval, bound to the complete evaluation hash."""

    __tablename__ = "knowledge_release_approvals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "evaluation_id"],
            ["knowledge_release_evaluations.tenant_id", "knowledge_release_evaluations.id"],
            name="fk_knowledge_release_approval_eval_tenant",
        ),
        UniqueConstraint(
            "tenant_id",
            "evaluation_id",
            "reviewer_id",
            name="uq_knowledge_release_approval_reviewer",
        ),
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_knowledge_release_approval_idempotency"
        ),
        CheckConstraint(
            "reviewer_role IN ('knowledge_manager', 'tenant_owner')",
            name="ck_knowledge_release_approval_role",
        ),
        Index("ix_knowledge_release_approval_eval", "tenant_id", "evaluation_id"),
    )

    evaluation_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    reviewer_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    reviewer_role: Mapped[str] = mapped_column(String(31), nullable=False)
    candidate_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class KnowledgeReleasePostTest(Base, PkMixin, TenantMixin):
    """A post-publish measurement. It never replaces the pre-publish run."""

    __tablename__ = "knowledge_release_post_tests"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "evaluation_id"],
            ["knowledge_release_evaluations.tenant_id", "knowledge_release_evaluations.id"],
            name="fk_knowledge_release_post_test_eval_tenant",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_knowledge_release_post_test_idempotency",
        ),
        CheckConstraint(
            "status IN ('passed', 'blocked')", name="ck_knowledge_release_post_test_status"
        ),
        Index("ix_knowledge_release_post_test_eval", "tenant_id", "evaluation_id", "created_at"),
    )

    evaluation_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    run: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(15), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(63), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class KnowledgeReleaseEvent(Base, PkMixin, TenantMixin):
    """Append-only release/activation/rollback ledger for operator review."""

    __tablename__ = "knowledge_release_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "evaluation_id"],
            ["knowledge_release_evaluations.tenant_id", "knowledge_release_evaluations.id"],
            name="fk_knowledge_release_event_eval_tenant",
        ),
        CheckConstraint(
            "action IN ('publish_requested', 'activated', 'post_test_passed', "
            "'rollback_required', 'rollback_completed', 'evaluation_created', "
            "'approval_recorded')",
            name="ck_knowledge_release_event_action",
        ),
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_knowledge_release_event_idempotency"
        ),
        Index("ix_knowledge_release_event_eval", "tenant_id", "evaluation_id", "created_at"),
    )

    evaluation_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    action: Mapped[str] = mapped_column(String(31), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    from_version_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    to_version_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    reason_code: Mapped[str] = mapped_column(String(63), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


__all__ = [
    "KnowledgeReleaseApproval",
    "KnowledgeReleaseEvaluation",
    "KnowledgeReleaseEvent",
    "KnowledgeReleasePostTest",
]
