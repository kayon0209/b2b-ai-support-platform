"""Case domain logic: explicit state machine + SLA policy math (ticket 21).

(docs/domain-model.md) All transitions are explicit commands, audited by
the caller. SLA pause behavior comes from policy, never inferred from
labels. Optimistic concurrency via the `version` column — every state
command carries expected_version.
"""

import enum
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
)
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
    # int(), not float: a deadline is an epoch-second timestamp and must land
    # on a whole second. The multiplier is fractional (p0 = 0.25), so the
    # product is a float; truncating toward zero keeps the deadline no later
    # than the policy intends, which is the safe direction for an SLA clock.
    return opened_at + max(int(target_seconds) - elapsed_running_seconds, 0)


def is_breached(deadline_ts: int, now: int | None = None) -> bool:
    return (now or int(time.time())) > deadline_ts


# --- contract tier -> SLA policy -------------------------------------------
#
# A customer's contract tier scales the SLA targets. It does **not** change
# `running_states`: which statuses stop a clock is a property of the support
# workflow, not of what the customer bought, and letting a tier change it would
# mean two customers with the same workflow paused at different points.
TIER_TARGET_MULTIPLIERS: dict[str, float] = {
    "strategic": 0.25,
    "enterprise": 0.5,
    "standard": 1.0,
    "basic": 2.0,
}


def sla_policy_for_tier(tier: str | None, *, contract_status: str | None = "active") -> SlaPolicy:
    """The SLA policy in force for a Case on this account.

    Two decisions worth stating, because both are silent if they go the other
    way:

    - **A tier only applies while the contract is active.** `pending`,
      `suspended` and `churned` all resolve to `standard`. An account that
      stopped paying does not keep a 15-minute first-response target: a tighter
      clock that the customer is no longer entitled to would fire escalations
      nobody agreed to.
    - **An unknown or absent tier resolves to `standard`,** which is exactly
      the policy that was in force before tiers existed. So a Case with no
      account, or an account predating this field, gets the deadline it would
      have got yesterday rather than a shorter one. A typo cannot reach here -
      the column carries a CHECK constraint - but a `None` reaches here
      constantly, and that must be the safe direction.

    The multiplier is applied to the target minutes and floored at one minute:
    a tier is not permitted to produce a zero-length window, which would make
    every Case instantly breached.
    """

    multiplier = (
        TIER_TARGET_MULTIPLIERS.get(tier or "", 1.0) if _is_active(contract_status) else 1.0
    )
    return replace(
        DEFAULT_SLA,
        first_response_minutes=max(int(DEFAULT_SLA.first_response_minutes * multiplier), 1),
        resolution_minutes=max(int(DEFAULT_SLA.resolution_minutes * multiplier), 1),
    )


def _is_active(contract_status: str | None) -> bool:
    """Absent means active. A Case created before accounts existed has no
    status to consult, and treating that as *not* active would silently loosen
    every pre-existing Case's clock."""
    return contract_status is None or contract_status == "active"


# --- ORM models ---


class Case(Base, PkMixin, TenantMixin):
    __tablename__ = "cases"
    __table_args__ = (
        Index("ix_cases_status", "status"),
        UniqueConstraint("id", "tenant_id", name="uq_cases_id_tenant"),
        # Composite: a Case cannot belong to another tenant's account. It also
        # turns the column from "a uuid that might mean something" into a
        # reference - before 0028 this had no FK and no target table, so any
        # uuid at all could sit here and nothing would report it.
        ForeignKeyConstraint(
            ["enterprise_account_id", "tenant_id"],
            ["enterprise_accounts.id", "enterprise_accounts.tenant_id"],
            name="fk_cases_account_same_tenant",
        ),
    )

    enterprise_account_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    # The tier the clock was started under, snapshotted at open. The deadline
    # is recomputed on a priority change, and re-resolving the tier then would
    # mean a mid-Case contract change silently moved an already-running
    # deadline - and "why was the first-response target 30 minutes" would
    # depend on when the question is asked.
    sla_tier: Mapped[str | None] = mapped_column(String(31), nullable=True)
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
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
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


class CaseEscalation(Base, PkMixin, TenantMixin):
    """One rung of the escalation ladder, recorded once.

    `UNIQUE (case_id, clock, level)` is the idempotency mechanism rather than a
    `SELECT`-then-`INSERT` in the scanner: two workers polling concurrently
    would both read "not yet escalated" and both escalate, which is the same
    check-then-act race the ingestion claim had. With the constraint, the loser
    gets an integrity error and skips, and the guarantee holds for any future
    caller.

    The migration carries the rest of the reasoning (why `breach_seconds` is a
    snapshot, why the routing refs are not foreign keys).
    """

    __tablename__ = "case_escalations"
    __table_args__ = (
        UniqueConstraint("case_id", "clock", "level", name="uq_case_escalation_once"),
        Index("ix_case_escalations_case", "case_id"),
    )

    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id"), nullable=False)
    # 'first_response' | 'resolution'. A string rather than an enum type, so a
    # third clock is a data change rather than a type migration.
    clock: Mapped[str] = mapped_column(String(31), nullable=False)
    level: Mapped[int] = mapped_column(nullable=False)
    reason_code: Mapped[str] = mapped_column(String(63), nullable=False)
    # How far past the deadline at the moment of escalation. Recorded, not
    # derived: recomputing it later would rewrite history as the clock runs.
    breach_seconds: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    # Where the escalation was routed, snapshotted. Not foreign keys: the
    # targets are opaque external refs that get renamed.
    assignee_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    team_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    escalated_at: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
