"""Billing ledger model (migration 0031, docs/development-plan.md Phase 5).

Append-only by grant: the app role holds SELECT and INSERT, not UPDATE or
DELETE. Corrections are new rows with `entry_kind='adjustment'`.
"""

import enum
import uuid
from typing import Any

from sqlalchemy import BigInteger, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from platform_core.orm_base import Base, PkMixin, TenantMixin


class EntryKind(enum.StrEnum):
    USAGE = "usage"
    ADJUSTMENT = "adjustment"


class BillingEntry(Base, PkMixin, TenantMixin):
    __tablename__ = "billing_entries"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_billing_entry_event"),
        Index("ix_billing_entries_tenant_period", "tenant_id", "period_start"),
        Index("ix_billing_entries_run", "run_id"),
    )

    # The outbox event that produced this row; unique so a redelivered event
    # cannot bill twice.
    event_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    entry_kind: Mapped[str] = mapped_column(String(31), nullable=False, default="usage")
    route: Mapped[str] = mapped_column(String(31), nullable=False, default="")
    run_status: Mapped[str] = mapped_column(String(31), nullable=False, default="")
    prompt_tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    # Calendar month the entry belongs to, as UTC epoch seconds of its first
    # instant. Stored, not derived: a March row must belong to March forever,
    # independent of any later timezone change.
    period_start: Mapped[int] = mapped_column(BigInteger, nullable=False)
    recorded_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "run_id": str(self.run_id),
            "entry_kind": self.entry_kind,
            "route": self.route,
            "run_status": self.run_status,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "period_start": self.period_start,
            "recorded_at": self.recorded_at,
        }
