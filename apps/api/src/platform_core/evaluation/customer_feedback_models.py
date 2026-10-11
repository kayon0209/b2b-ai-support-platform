"""Append-only customer resolution-feedback events for online evaluation."""

from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, CheckConstraint, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from platform_core.orm_base import Base, PkMixin, TenantMixin


class CustomerResolutionFeedbackEvent(Base, PkMixin, TenantMixin):
    __tablename__ = "customer_resolution_feedback_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('requested', 'confirmed', 'rejected')",
            name="ck_customer_resolution_feedback_event_type",
        ),
        UniqueConstraint(
            "tenant_id", "idempotency_key_hash", name="uq_customer_feedback_idempotency"
        ),
        Index(
            "uq_customer_feedback_request_once",
            "tenant_id",
            "conversation_ref_id",
            unique=True,
            postgresql_where=text("event_type = 'requested'"),
        ),
        Index(
            "ix_customer_feedback_tenant_conversation_time",
            "tenant_id",
            "conversation_ref_id",
            "occurred_at",
        ),
        Index(
            "ix_customer_feedback_tenant_time",
            "tenant_id",
            "occurred_at",
        ),
        Index(
            "ix_customer_feedback_tenant_case_time",
            "tenant_id",
            "case_id",
            "occurred_at",
            postgresql_where=text("case_id IS NOT NULL"),
        ),
    )

    conversation_ref_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    # Derived by the server from tenant-scoped CaseConversation links; never
    # accepted from the visitor request body.
    case_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    event_type: Mapped[str] = mapped_column(String(15), nullable=False)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source: Mapped[str] = mapped_column(String(31), nullable=False, default="support_surface")


__all__ = ["CustomerResolutionFeedbackEvent"]
