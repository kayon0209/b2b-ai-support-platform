"""Append-only tenant records of human corrections to emotion advice (R2-01)."""

import uuid

from sqlalchemy import BigInteger, CheckConstraint, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from platform_core.orm_base import Base, PkMixin, TenantMixin


class EmotionAdviceReview(Base, PkMixin, TenantMixin):
    """A supervisor's bounded correction, without copied customer wording."""

    __tablename__ = "emotion_advice_reviews"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_emotion_advice_review_idempotency",
        ),
        UniqueConstraint(
            "tenant_id",
            "conversation_ref_id",
            "advice_id",
            "reviewer_id",
            name="uq_emotion_advice_review_reviewer_revision",
        ),
        Index(
            "ix_emotion_advice_reviews_conversation_created",
            "tenant_id",
            "conversation_ref_id",
            "created_at",
        ),
        CheckConstraint(
            "suggested_level IN ('calm', 'frustrated', 'angry', 'escalation_risk')",
            name="ck_emotion_review_suggested_level",
        ),
        CheckConstraint(
            "corrected_level IN ('calm', 'frustrated', 'angry', 'escalation_risk')",
            name="ck_emotion_review_corrected_level",
        ),
        CheckConstraint(
            "reason_code IN ('overstated', 'understated', 'quoted_or_negated', "
            "'sarcasm_or_mixed_tone', 'context_missing', 'other')",
            name="ck_emotion_review_reason_code",
        ),
    )

    conversation_ref_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    advice_id: Mapped[str] = mapped_column(String(64), nullable=False)
    suggested_level: Mapped[str] = mapped_column(String(31), nullable=False)
    corrected_level: Mapped[str] = mapped_column(String(31), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(63), nullable=False)
    reviewer_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
