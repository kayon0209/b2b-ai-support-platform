"""Case domain logic: explicit state machine + SLA policy math (ticket 21).

(docs/domain-model.md) All transitions are explicit commands, audited by
the caller. SLA pause behavior comes from policy, never inferred from
labels. Optimistic concurrency via the `version` column — every state
command carries expected_version.
"""

import enum
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import BigInteger, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from platform_core.orm_base import Base, PkMixin, TenantMixin


class CaseStatus(enum.StrEnum):
    NEW = "new"
    TRIAGED = "triaged"
    IN_PROGRESS = "in_progress"
    WAITING_CUSTOMER = "waiting_customer"
    WAITING_INTERNAL = "waiting_internal"
    WAITING_VENDOR = "waiting_vendor"
    RESOLVED = "resolved"
    CLOSED = "closed"
    REOPENED = "reopened"


# Explicit transition table (docs/domain-model.md case states).
TRANSITIONS: dict[CaseStatus, set[CaseStatus]] = {
    CaseStatus.NEW: {CaseStatus.TRIAGED, CaseStatus.CLOSED},
    CaseStatus.TRIAGED: {CaseStatus.IN_PROGRESS, CaseStatus.CLOSED},
    CaseStatus.IN_PROGRESS: {
        CaseStatus.WAITING_CUSTOMER,
        CaseStatus.WAITING_INTERNAL,
        CaseStatus.WAITING_VENDOR,
        CaseStatus.RESOLVED,
    },
    CaseStatus.WAITING_CUSTOMER: {CaseStatus.IN_PROGRESS, CaseStatus.RESOLVED, CaseStatus.CLOSED},
    CaseStatus.WAITING_INTERNAL: {CaseStatus.IN_PROGRESS, CaseStatus.RESOLVED},
    CaseStatus.WAITING_VENDOR: {CaseStatus.IN_PROGRESS, CaseStatus.RESOLVED},
    CaseStatus.RESOLVED: {CaseStatus.CLOSED, CaseStatus.REOPENED},
    CaseStatus.CLOSED: {CaseStatus.REOPENED},
    CaseStatus.REOPENED: {CaseStatus.IN_PROGRESS, CaseStatus.CLOSED},
}


class TransitionNotAllowed(Exception):
    pass


class VersionConflict(Exception):
    pass


def check_transition(current: CaseStatus, target: CaseStatus) -> None:
    if target not in TRANSITIONS.get(current, set()):
        raise TransitionNotAllowed(f"{current.value} -> {target.value} not allowed")


def check_version(current_version: int, expected_version: int | None) -> None:
    """Optimistic concurrency (docs/api-contracts.md case command API)."""
    if expected_version is not None and current_version != expected_version:
        raise VersionConflict(f"expected {expected_version}, got {current_version}")


# --- SLA policy (docs/domain-model.md: pause behavior is policy-driven) ---


@dataclass(frozen=True)
class SlaPolicy:
    """Clocks tick only in states listed in running_states."""

    first_response_minutes: int
    resolution_minutes: int
    # which statuses keep the clocks running
    running_states: frozenset[CaseStatus]
    priority_multipliers: dict[str, float]

    def paused(self, status: CaseStatus) -> bool:
        return status not in self.running_states

    def deadline(self, opened_at: int, elapsed_before_pause: int, target_minutes: int) -> int:
        return opened_at + (target_minutes - elapsed_before_pause) * 60


DEFAULT_SLA = SlaPolicy(
    first_response_minutes=60,
    resolution_minutes=8 * 60,
    running_states=frozenset(
        {
            CaseStatus.NEW,
            CaseStatus.TRIAGED,
            CaseStatus.IN_PROGRESS,
            CaseStatus.REOPENED,
        }
    ),
    priority_multipliers={"p0": 0.25, "p1": 0.5, "p2": 1.0, "p3": 2.0},
)


def sla_deadline(
    policy: SlaPolicy,
    *,
    priority: str,
    opened_at: int,
    elapsed_running_seconds: int,
    first_response: bool,
) -> int:
    """Compute the absolute SLA deadline for a case.

    elapsed_running_seconds counts only time in running states (caller
    persists accruals on each pause/resume). Multiplier scales the target:
    p0 gets a quarter of the standard window, etc.
    """
    multiplier = policy.priority_multipliers.get(priority, 1.0)
    target = policy.first_response_minutes if first_response else policy.resolution_minutes
    target_seconds = target * 60 * multiplier
    return opened_at + max(target_seconds - elapsed_running_seconds, 0)


def is_breached(deadline_ts: int, now: int | None = None) -> bool:
    return (now or int(time.time())) > deadline_ts


# --- ORM models ---


class Case(Base, PkMixin, TenantMixin):
    __tablename__ = "cases"
    __table_args__ = (Index("ix_cases_status", "status"),)

    enterprise_account_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    category: Mapped[str] = mapped_column(String(63), nullable=False, default="general")
    priority: Mapped[str] = mapped_column(String(7), nullable=False, default="p2")
    status: Mapped[str] = mapped_column(String(31), nullable=False, default="new")
    assignee_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    team_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    opened_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    first_response_due_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    resolution_due_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    first_responded_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    resolved_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    closed_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    elapsed_running_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_state_changed_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    metadata_json: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default="{}"
    )


class CaseConversation(Base, PkMixin, TenantMixin):
    """A Case may link multiple Chatwoot conversations (external refs)."""

    __tablename__ = "case_conversations"
    __table_args__ = (
        UniqueConstraint("case_id", "conversation_ref_id", name="uq_case_conversation"),
    )

    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id"), nullable=False, index=True)
    conversation_ref_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    relationship: Mapped[str] = mapped_column(String(31), nullable=False, default="origin")
