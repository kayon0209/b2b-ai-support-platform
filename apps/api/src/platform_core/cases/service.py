"""Case command service: the only way Cases change state (ticket 21).

Every command: validates transition + optimistic version, accrues SLA
time, recomputes deadlines, and writes an audit event in the SAME
transaction. The outbox enqueue rides along, so integrations observe
case.events without any extra writes from the caller.
"""

import time
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.cases.models import (
    DEFAULT_SLA,
    Case,
    CaseEscalation,
    CaseStatus,
    check_transition,
    check_version,
    sla_deadline,
    sla_policy_for_tier,
)


class CaseError(Exception):
    """A Case command that must be refused with a caller-readable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


class CaseService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_case(
        self,
        *,
        tenant_id: uuid.UUID,
        subject: str,
        description: str = "",
        priority: str = "p2",
        category: str = "general",
        actor_id: uuid.UUID | None = None,
        enterprise_account_id: uuid.UUID | None = None,
    ) -> Case:
        """Open a Case, deriving its SLA clocks from the account's contract.

        `enterprise_account_id` was a column nothing could write before this:
        the API did not accept it and this method never set it, so a Case could
        not be attached to the account it was about. The FK added in migration
        0028 makes a cross-tenant account unrepresentable, and because RLS
        cannot see the other tenant's row, an unknown id and a foreign id both
        report `ACCOUNT_NOT_FOUND` - so this cannot be used to discover which
        accounts exist elsewhere.
        """
        now = int(time.time())

        tier: str | None = None
        contract_status: str | None = None
        if enterprise_account_id is not None:
            # Imported here rather than at module import: `cases` depends on one
            # narrow identity read, and a module-level import of the identity
            # package would make the dependency look like a cycle to anyone
            # reading the graph.
            from platform_core.identity.org import account_sla_facts

            facts = await account_sla_facts(
                self._session, tenant_id=tenant_id, account_id=enterprise_account_id
            )
            if facts is None:
                raise CaseError("ACCOUNT_NOT_FOUND", str(enterprise_account_id))
            tier, contract_status = facts

        policy = sla_policy_for_tier(tier, contract_status=contract_status)

        case = Case(
            tenant_id=tenant_id,
            subject=subject,
            description=description,
            priority=priority,
            category=category,
            status=CaseStatus.NEW.value,
            version=1,
            opened_at=now,
            last_state_changed_at=now,
            enterprise_account_id=enterprise_account_id,
            # Snapshotted, not resolved later: the deadline is recomputed on a
            # priority change, and re-reading the account then would let a
            # mid-Case contract change move a clock that is already running.
            sla_tier=tier,
        )
        self._session.add(case)
        await self._session.flush()
        case.first_response_due_at = sla_deadline(
            policy,
            priority=priority,
            opened_at=now,
            elapsed_running_seconds=0,
            first_response=True,
        )
        case.resolution_due_at = sla_deadline(
            policy,
            priority=priority,
            opened_at=now,
            elapsed_running_seconds=0,
            first_response=False,
        )
        return case

    async def apply_command(
        self,
        *,
        tenant_id: uuid.UUID,
        case_id: uuid.UUID,
        command: str,
        expected_version: int | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> Case:
        """Dispatch a case command: transition / change_priority / assign.

        Raises TransitionNotAllowed / VersionConflict; the caller rolls
        back and returns CASE_TRANSITION_NOT_ALLOWED / 409.
        """
        row = (
            await self._session.execute(
                select(Case)
                .where(Case.tenant_id == tenant_id, Case.id == case_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise LookupError("case not found")

        check_version(row.version, expected_version)
        now = int(time.time())
        params = parameters or {}

        if command == "transition":
            target = CaseStatus(params["target"])
            check_transition(CaseStatus(row.status), target)
            self._accrue_sla_time(row, now)
            row.status = target.value
            if target == CaseStatus.RESOLVED:
                row.resolved_at = now
            if target == CaseStatus.CLOSED:
                row.closed_at = now
        elif command == "change_priority":
            row.priority = params["priority"]
            # The tier recorded at open, not the account's current one. A
            # priority change is a decision about this Case; a contract edit
            # that happened in between is not, and letting it through here
            # would mean the same Case had a different contractual window
            # depending on when the deadline happened to be recomputed.
            policy = sla_policy_for_tier(row.sla_tier)
            row.first_response_due_at = sla_deadline(
                policy,
                priority=row.priority,
                opened_at=row.opened_at,
                elapsed_running_seconds=row.elapsed_running_seconds,
                first_response=True,
            )
            row.resolution_due_at = sla_deadline(
                policy,
                priority=row.priority,
                opened_at=row.opened_at,
                elapsed_running_seconds=row.elapsed_running_seconds,
                first_response=False,
            )
        elif command == "assign":
            row.assignee_ref = params.get("assignee_ref")
            row.team_ref = params.get("team_ref")
        elif command == "record_first_response":
            if row.first_responded_at is None:
                row.first_responded_at = now
        else:
            raise ValueError(f"unknown case command: {command}")

        row.version += 1
        row.last_state_changed_at = now
        return row

    @staticmethod
    def _accrue_sla_time(case: Case, now: int) -> None:
        """Add time since the last state change if the clock was running."""
        status = CaseStatus(case.status)
        if status in DEFAULT_SLA.running_states and case.last_state_changed_at:
            case.elapsed_running_seconds += max(now - case.last_state_changed_at, 0)


async def export_cases(
    session: AsyncSession, *, tenant_id: uuid.UUID, since: int, until: int, limit: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """A bounded extract of this tenant's Cases and their escalation ledger.

    Returns `(cases, escalations, truncated)`. Two lists rather than nested
    objects: a compliance extract is read by a person or a spreadsheet, and a
    flat ledger keyed by `case_id` is easier to check than a tree.

    Opening the export window on `opened_at` rather than on the last update is
    deliberate. "Show me everything from Q3" means Cases that *arose* in Q3; a
    window on `last_state_changed_at` would hide a Case opened in Q2 and
    resolved in Q3, which is exactly the one an auditor is looking for.
    """
    rows = (
        await session.execute(
            select(Case)
            .where(
                Case.tenant_id == tenant_id,
                Case.opened_at >= since,
                Case.opened_at <= until,
            )
            .order_by(Case.opened_at, Case.id)
            .limit(limit + 1)
        )
    ).scalars()
    cases = list(rows)
    truncated = len(cases) > limit
    cases = cases[:limit]

    case_ids = [c.id for c in cases]
    escalations: list[dict[str, Any]] = []
    if case_ids:
        escalation_rows = (
            await session.execute(
                select(CaseEscalation)
                .where(
                    CaseEscalation.tenant_id == tenant_id,
                    CaseEscalation.case_id.in_(case_ids),
                )
                .order_by(CaseEscalation.escalated_at, CaseEscalation.id)
            )
        ).scalars()
        escalations = [
            {
                "case_id": str(e.case_id),
                "clock": e.clock,
                "level": int(e.level),
                "reason_code": e.reason_code,
                "breach_seconds": int(e.breach_seconds),
                "routed_to": e.team_ref,
                "escalated_at": e.escalated_at,
            }
            for e in escalation_rows
        ]

    return (
        [
            {
                "case_id": str(c.id),
                "subject": c.subject,
                "description": c.description,
                "category": c.category,
                "priority": c.priority,
                "status": c.status,
                "enterprise_account_id": (
                    str(c.enterprise_account_id) if c.enterprise_account_id else None
                ),
                "sla_tier": c.sla_tier,
                "assignee_ref": c.assignee_ref,
                "team_ref": c.team_ref,
                "opened_at": c.opened_at,
                "first_response_due_at": c.first_response_due_at,
                "resolution_due_at": c.resolution_due_at,
                "first_responded_at": c.first_responded_at,
                "resolved_at": c.resolved_at,
                "closed_at": c.closed_at,
            }
            for c in cases
        ],
        escalations,
        truncated,
    )
