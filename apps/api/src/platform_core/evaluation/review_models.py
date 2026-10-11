"""Tenant-scoped, append-only online quality-review batches and evidence."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from platform_core.orm_base import Base, PkMixin, TenantMixin


class QualityReviewBatch(Base, PkMixin, TenantMixin):
    __tablename__ = "quality_review_batches"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_quality_review_batches_tenant_id"),
        UniqueConstraint(
            "tenant_id", "idempotency_key_hash", name="uq_quality_review_batch_idempotency"
        ),
        Index("ix_quality_review_batches_tenant_created", "tenant_id", "created_at"),
        Index(
            "ix_quality_review_batches_tenant_prompt_created",
            "tenant_id",
            "target_prompt_version_id",
            "created_at",
        ),
    )

    created_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    seed: Mapped[str] = mapped_column(String(63), nullable=False)
    window_seconds: Mapped[int] = mapped_column(nullable=False)
    requested_size: Mapped[int] = mapped_column(nullable=False)
    target_prompt_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("prompt_versions.id"), nullable=True
    )
    population_by_stratum: Mapped[dict[str, int]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    sampler_version: Mapped[str] = mapped_column(String(31), nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class QualityReviewItem(Base, PkMixin, TenantMixin):
    __tablename__ = "quality_review_items"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "batch_id",
            "agent_run_id",
            name="uq_quality_review_item_decision_fk",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "batch_id"],
            ["quality_review_batches.tenant_id", "quality_review_batches.id"],
            name="fk_quality_review_item_batch_tenant",
        ),
        ForeignKeyConstraint(
            ["agent_run_id"], ["agent_runs.id"], name="fk_quality_review_item_agent_run"
        ),
        Index("ix_quality_review_items_tenant_batch", "tenant_id", "batch_id"),
        Index("ix_quality_review_items_tenant_run", "tenant_id", "agent_run_id"),
    )

    batch_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    agent_run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    conversation_ref_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    stratum: Mapped[str] = mapped_column(String(31), nullable=False)
    route: Mapped[str] = mapped_column(String(31), nullable=False)
    run_status: Mapped[str] = mapped_column(String(31), nullable=False)
    prompt_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("prompt_versions.id"))
    code_version: Mapped[str] = mapped_column(String(63), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(63), nullable=False)
    selected_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class QualityReviewDecision(Base, PkMixin, TenantMixin):
    __tablename__ = "quality_review_decisions"
    __table_args__ = (
        CheckConstraint("verdict IN ('agree', 'override')", name="ck_quality_review_verdict"),
        CheckConstraint(
            "(verdict = 'agree' AND reason_code IS NULL) OR "
            "(verdict = 'override' AND reason_code IS NOT NULL)",
            name="ck_quality_review_override_reason",
        ),
        CheckConstraint(
            "reason_code IS NULL OR reason_code IN "
            "('unsupported_claim', 'wrong_route', 'citation_gap', 'unsafe_action', "
            "'task_outcome_mismatch', 'other')",
            name="ck_quality_review_reason_code",
        ),
        UniqueConstraint(
            "tenant_id", "batch_id", "agent_run_id", name="uq_quality_review_decision_once"
        ),
        UniqueConstraint(
            "tenant_id", "idempotency_key_hash", name="uq_quality_review_decision_idempotency"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "batch_id", "agent_run_id"],
            [
                "quality_review_items.tenant_id",
                "quality_review_items.batch_id",
                "quality_review_items.agent_run_id",
            ],
            name="fk_quality_review_decision_selected_item",
        ),
        Index("ix_quality_review_decisions_tenant_batch", "tenant_id", "batch_id"),
    )

    batch_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    agent_run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    reviewer_actor_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    verdict: Mapped[str] = mapped_column(String(15), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(31), nullable=True)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewed_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class QualityReviewEvidence(Base, PkMixin, TenantMixin):
    __tablename__ = "quality_review_evidence"
    __table_args__ = (
        UniqueConstraint("tenant_id", "batch_id", name="uq_quality_review_evidence_batch"),
        UniqueConstraint(
            "tenant_id", "idempotency_key_hash", name="uq_quality_review_evidence_idempotency"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "batch_id"],
            ["quality_review_batches.tenant_id", "quality_review_batches.id"],
            name="fk_quality_review_evidence_batch_tenant",
        ),
        Index("ix_quality_review_evidence_tenant_created", "tenant_id", "created_at"),
    )

    batch_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


__all__ = [
    "QualityReviewBatch",
    "QualityReviewDecision",
    "QualityReviewEvidence",
    "QualityReviewItem",
]
