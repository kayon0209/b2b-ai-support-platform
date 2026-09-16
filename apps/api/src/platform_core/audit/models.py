"""AuditEvent model. Append-only: no update/delete paths are provided."""

import uuid
from typing import Any

from sqlalchemy import BigInteger, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from platform_core.orm_base import Base, PkMixin, TenantMixin


class AuditEvent(Base, PkMixin, TenantMixin):
    __tablename__ = "audit_events"

    occurred_at: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    actor_type: Mapped[str] = mapped_column(String(31), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    action: Mapped[str] = mapped_column(String(127), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(63), nullable=False)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    decision: Mapped[str] = mapped_column(String(31), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(63), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(63), nullable=False)
    before_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_redacted: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default="{}"
    )
