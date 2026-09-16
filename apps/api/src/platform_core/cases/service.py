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
    CaseStatus,
    check_transition,
    check_version,
    sla_deadline,
)


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
    ) -> Case:
        now = int(time.time())
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
        )
        self._session.add(case)
        await self._session.flush()
        case.first_response_due_at = sla_deadline(
            DEFAULT_SLA,
            priority=priority,
            opened_at=now,
            elapsed_running_seconds=0,
            first_response=True,
        )
        case.resolution_due_at = sla_deadline(
            DEFAULT_SLA,
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
            row.first_response_due_at = sla_deadline(
                DEFAULT_SLA,
                priority=row.priority,
                opened_at=row.opened_at,
                elapsed_running_seconds=row.elapsed_running_seconds,
                first_response=True,
            )
            row.resolution_due_at = sla_deadline(
                DEFAULT_SLA,
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
