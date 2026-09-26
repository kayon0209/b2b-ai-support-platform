"""Persistence for answer corrections (feature list 7.8).

The lifecycle is deliberately small: pending -> approved | dismissed. There is
no "auto-applied" state on purpose - AGENTS.md forbids learning from
unreviewed conversations, and an unreviewed correction is exactly that.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import BigInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from platform_core.orm_base import Base, PkMixin, TenantMixin


class CorrectionStatus(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DISMISSED = "dismissed"


class AnswerCorrection(Base, PkMixin, TenantMixin):
    __tablename__ = "answer_corrections"

    agent_run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    correct_answer: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(31), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reviewed_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
