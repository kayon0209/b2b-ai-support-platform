"""case.read: the internal read tool (iteration plan 3.2).

Reads the platform's OWN Case table - no connector involved, so the registry
resolves it outside the provider machinery. The tool exists because a case's
status is live state the corpus must never contain (ADR 0006); it is also the
read tool the evaluation dataset declared (`allowed_tools`) that was never
registered.

The executor is session-bound: it reads under the run's RLS-bound
transaction, so a case from another tenant is invisible rather than
merely filtered afterwards.
"""

import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.cases.models import Case

# A case reference as a customer writes it: "case 12345", "#12345", or the
# bare uuid a previous reply quoted.
_NUMERIC_REF = re.compile(r"\d{3,12}")
_UUID_REF = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)


class CaseReadExecutor:
    """ToolExecutor for case.read. Reads only; never mutates a case."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def execute(
        self, tool_name: str, parameters: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any] | None:
        case_ref = str(parameters.get("case_ref") or "").strip()
        if not case_ref:
            raise ValueError("case.read requires case_ref")
        row = await self._find(case_ref)
        if row is None:
            return {"found": False, "case_ref": case_ref}
        return {
            "found": True,
            "case": {
                "case_id": str(row.id),
                "status": row.status,
                "priority": row.priority,
                "subject": row.subject,
                "first_response_due_at": row.first_response_due_at,
                "resolution_due_at": row.resolution_due_at,
            },
        }

    async def verify_postcondition(
        self, tool_name: str, parameters: dict[str, Any], output: dict[str, Any] | None
    ) -> bool | None:
        return isinstance(output, dict) and "found" in output

    async def _find(self, case_ref: str) -> Case | None:
        uuid_match = _UUID_REF.search(case_ref)
        if uuid_match:
            row = (
                await self._session.execute(
                    select(Case).where(Case.id == uuid.UUID(uuid_match.group(0)))
                )
            ).scalar_one_or_none()
            if row is not None:
                return row
        numeric = _NUMERIC_REF.search(case_ref)
        if numeric is None:
            return None
        # The subject search is the customer-facing handle ("case 12345" in
        # the subject), bounded to a small window - a LIKE scan is acceptable
        # inside one tenant, never across one.
        rows = (
            (
                await self._session.execute(
                    select(Case).where(Case.subject.contains(numeric.group(0))).limit(5)
                )
            )
            .scalars()
            .all()
        )
        return rows[0] if len(rows) == 1 else None
