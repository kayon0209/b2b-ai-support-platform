"""Transactional outbox (ticket 7).

Pattern (docs/architecture.md, AGENTS.md coding rules): business state and
the outbound event are written in ONE database transaction. A relay worker
publishes queued rows afterwards and marks them sent. Crash between commit
and publish loses nothing — the row is still queued.
"""

import enum
import uuid
from typing import Any

from sqlalchemy import BigInteger, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from platform_core.orm_base import Base, PkMixin, TenantMixin


class OutboxStatus(enum.StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    SENT = "sent"
    FAILED = "failed"


class OutboxEvent(Base, PkMixin, TenantMixin):
    __tablename__ = "outbox_events"
    __table_args__ = (Index("ix_outbox_events_status_id", "status", "id"),)

    # Idempotency across the whole pipeline: consumers dedupe by event_id.
    event_id: Mapped[uuid.UUID] = mapped_column(unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(127), nullable=False)
    event_version: Mapped[int] = mapped_column(nullable=False, default=1)
    aggregate_type: Mapped[str] = mapped_column(String(63), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        String(31), nullable=False, default=OutboxStatus.QUEUED.value, index=True
    )
    processing_started_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Fences a recovered queue claim from a worker that resumed after its
    # lease expired. The relay commits this token before tenant work starts;
    # completion updates compare it so a stale process cannot settle a new claim.
    processing_token: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # First attempt time is stable across retries. Dedicated bounded jobs use
    # it to enforce one logical deadline instead of getting a fresh window
    # every time a worker restarts or reclaims a claim.
    first_attempt_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    deadline_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    external_attempt_limit: Mapped[int | None] = mapped_column(nullable=True)
    published_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str] = mapped_column(String(63), nullable=False, default="")
