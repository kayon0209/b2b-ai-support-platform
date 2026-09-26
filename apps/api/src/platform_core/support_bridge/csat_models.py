"""Persistence for customer satisfaction responses (feature list 7.10).

The row is a customer's verdict on one conversation: a score on a fixed 1..5
scale, and optionally what they chose to say about it. The scale is fixed
because an average over a moving scale is not an average; the comment is free
text because a score with no reason is hard to act on, and it is customer
content so it is minimised like any other.

One row per conversation, enforced by the unique constraint - see migration
0044 for why that is not left to application code.
"""

from __future__ import annotations

import uuid

from sqlalchemy import BigInteger, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from platform_core.orm_base import Base, PkMixin, TenantMixin


class CsatResponse(Base, PkMixin, TenantMixin):
    __tablename__ = "csat_responses"

    conversation_ref_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    # Optional links outward: a score is still useful when the conversation was
    # never turned into a case or a run, so neither is required.
    case_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Which surface the customer answered on, so a channel that consistently
    # scores lower is visible as a channel problem rather than a vague trend.
    channel: Mapped[str | None] = mapped_column(String(31), nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
