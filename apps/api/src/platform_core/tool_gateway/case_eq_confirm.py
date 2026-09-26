"""case.eq_confirm: record a customer's engineering-question confirmation.

An EQ is the engineering question raised against a customer's board file
before production. It is the highest-value confirmation in a PCB/PCBA
workflow and the most expensive one to get wrong: production is released
against it, so a confirmation recorded for the wrong case, or recorded when
the customer never gave one, produces boards built to a spec nobody agreed.

That is why this is a **tool** rather than an ordinary case command:

- it is `confirmed_write`, so the agent can *propose* it and a person must
  approve the exact arguments before it runs;
- the arguments are frozen and hashed, so the case it applies to cannot
  change between approval and execution;
- it is idempotent, verified and audited like every other write.

Scope is enforced on two axes, because either one alone is insufficient: the
case must carry `category = eq_confirmation` (so the tool cannot be pointed at
an ordinary ticket) and it must be `waiting_customer` (so it cannot be
recorded before the question was asked).

**The status after a confirmation is `in_progress`, not `waiting_internal`.**
The research report proposed adding a `waiting_customer -> waiting_internal`
edge to the state machine; it was not added, and the reason is the SLA policy
rather than taste. `DEFAULT_SLA.running_states` excludes both waiting states,
so moving to `waiting_internal` would *pause* the resolution clock at exactly
the moment the customer has done their part and the work is ours - the
platform would be granting itself an unlimited extension for the interval it
is most obliged to be quick. `waiting_customer -> in_progress` is already a
legal transition, and it restarts the clock against us, which is the incentive
the flow needs. `docs/domain-model.md` was left unchanged because the
documented machine already says this.

It also matches the business rule the research report records for this customer
- "交期自 EQ 确认后起算", the lead time starts counting once the EQ is
confirmed. A state that pauses the clock would contradict that; `in_progress`
is the one that agrees with it.
"""

import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.cases.models import Case, CaseCategory, CaseStatus
from platform_core.cases.service import CaseError, CaseService

# The same reference forms `case.read` accepts: "case 12345", "#12345", or a
# uuid. Kept in step with it deliberately - a customer who can read a case by
# a reference must be able to confirm it by the same one.
_NUMERIC_REF = re.compile(r"\d{3,12}")
_UUID_REF = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)

# The input schema lives in `registry.TOOL_CATALOG` with every other write
# tool's, not here. This module imports `cases.models`, which carries a foreign
# key into `identity.models`, so importing it at registry-import time
# configures the Case mapper before `enterprise_accounts` exists and SQLAlchemy
# raises `NoReferencedTableError`. The schema is the one part of the tool that
# needs no model, so it is the part that lives where the registry can read it.

# Statuses that mean the confirmation has already been recorded. Treated as
# success rather than as a refusal: the customer confirming twice is the same
# fact, and a tool that reported failure for it would send an operator looking
# for a problem that does not exist.
_ALREADY_CONFIRMED = frozenset(
    {
        CaseStatus.IN_PROGRESS.value,
        CaseStatus.WAITING_INTERNAL.value,
        CaseStatus.WAITING_VENDOR.value,
    }
)


class CaseEqConfirmExecutor:
    """ToolExecutor for case.eq_confirm. Session-bound; never touches a connector."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def execute(
        self, tool_name: str, parameters: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any] | None:
        case_ref = str(parameters.get("case_ref") or "").strip()
        if not case_ref:
            raise ValueError("case.eq_confirm requires case_ref")

        row = await self._find_locked(case_ref)
        if row is None:
            return {"ok": False, "error_code": "CASE_NOT_FOUND", "case_ref": case_ref}

        if row.category != CaseCategory.EQ_CONFIRMATION.value:
            # The category is the guard that keeps this tool off ordinary
            # tickets. Reported as a refusal rather than as "not found": an
            # operator who pointed it at the wrong case needs to know that is
            # what happened.
            return {
                "ok": False,
                "error_code": "CASE_NOT_AN_EQ_CONFIRMATION",
                "case_id": str(row.id),
                "category": row.category,
            }

        if row.status in _ALREADY_CONFIRMED:
            return {
                "ok": True,
                "already_confirmed": True,
                "case_id": str(row.id),
                "status": row.status,
            }

        if row.status != CaseStatus.WAITING_CUSTOMER.value:
            return {
                "ok": False,
                "error_code": "CASE_NOT_AWAITING_CONFIRMATION",
                "case_id": str(row.id),
                "status": row.status,
            }

        # Through the case service, not by writing the column: the service
        # owns the transition table, the optimistic-concurrency check and the
        # SLA accrual, and a tool that set `status` directly would bypass all
        # three while looking like it worked.
        try:
            updated = await CaseService(self._session).apply_command(
                tenant_id=row.tenant_id,
                case_id=row.id,
                command="transition",
                expected_version=row.version,
                parameters={"target": CaseStatus.IN_PROGRESS.value},
            )
        except CaseError as exc:
            return {"ok": False, "error_code": exc.code, "case_id": str(row.id)}

        return {
            "ok": True,
            "already_confirmed": False,
            "case_id": str(updated.id),
            "status": updated.status,
        }

    async def verify_postcondition(
        self, tool_name: str, parameters: dict[str, Any], output: dict[str, Any] | None
    ) -> bool | None:
        """Re-read and confirm the case is no longer waiting on the customer.

        Read back from the database rather than trusting the returned dict:
        the point of a postcondition is that it is observed, and an adapter
        that reported success while the row said otherwise is exactly the
        failure this catches.
        """
        if not isinstance(output, dict) or not output.get("ok"):
            return False
        case_id = output.get("case_id")
        if not isinstance(case_id, str):
            return False
        row = (
            await self._session.execute(select(Case).where(Case.id == uuid.UUID(case_id)))
        ).scalar_one_or_none()
        if row is None:
            return None
        return row.status != CaseStatus.WAITING_CUSTOMER.value

    async def _find_locked(self, case_ref: str) -> Case | None:
        """Find the case by reference, holding a row lock.

        Locked because the status read and the transition that follows it must
        not be separable: two confirmations arriving together would otherwise
        both see `waiting_customer`, and the second would either double-accrue
        SLA time or fail on a version the caller never saw.
        """
        uuid_match = _UUID_REF.search(case_ref)
        if uuid_match:
            row = (
                await self._session.execute(
                    select(Case).where(Case.id == uuid.UUID(uuid_match.group(0))).with_for_update()
                )
            ).scalar_one_or_none()
            if row is not None:
                return row
        numeric = _NUMERIC_REF.search(case_ref)
        if numeric is None:
            return None
        rows = (
            (
                await self._session.execute(
                    select(Case).where(Case.subject.contains(numeric.group(0))).limit(5)
                )
            )
            .scalars()
            .all()
        )
        # Ambiguity is not resolved by picking one: two cases matching a
        # reference means the reference was not specific enough, and
        # confirming the wrong one releases production against the wrong spec.
        return rows[0] if len(rows) == 1 else None
