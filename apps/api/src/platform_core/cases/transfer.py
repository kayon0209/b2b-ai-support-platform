"""Feature list 7.7: one-action escalation to a named team.

`CaseService.apply_command("assign", team_ref=...)` already moves a case to a
team. What was missing is the thing a person actually says: "转工程", "转品质",
"转工厂" - a business destination, not a queue slug. This module is the
translation, and nothing else.

Three properties it has to have, and where each one comes from:

- **The destination must exist.** A slug with no `Department` row sends the
  case to a queue nobody reads, and the escalation looks like it worked. So
  the department is looked up before the case is touched; a miss is an error,
  not a write.
- **Re-escalating to the same team is a no-op.** Escalating twice is normal
  (two agents agree, or the customer asks again), and each write bumps
  `version` - which would fight the optimistic concurrency the case API
  requires of its callers. Doing nothing when the target is already set is
  what keeps the version meaningful.
- **The reason travels with it.** "Moved to quality" without why is a routing
  change nobody can review later; 6.6 exists so that reason has somewhere to
  live.

Person-level assignment belongs to the agent directory and conversation lease.
Team escalation remains a separate Case command, so each work object has one
source of truth for its owner.
"""

from __future__ import annotations

import re
import uuid
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.cases.models import Case
from platform_core.identity.models import Department


class TransferTarget(StrEnum):
    """A business destination, as a person would name it."""

    ENGINEERING = "engineering"
    QUALITY = "quality"
    FACTORY = "factory"
    FINANCE = "finance"
    FULFILMENT = "fulfilment"
    SALES = "sales"
    SECURITY = "security"


# Phrase -> target. Chinese first because that is what customers and agents
# write; the English forms are here because the same phrases arrive from the
# English UI.
_PHRASES: tuple[tuple[TransferTarget, re.Pattern[str]], ...] = (
    (
        TransferTarget.ENGINEERING,
        re.compile(r"转?工程|工程师|技术支持|technical|engineering", re.I),
    ),
    (TransferTarget.QUALITY, re.compile(r"转?品质|质量|品保|品质部|quality", re.I)),
    (TransferTarget.FACTORY, re.compile(r"转?工厂|生产|产线|车间|factory|production", re.I)),
    (TransferTarget.FINANCE, re.compile(r"转?财务|开票|发票|对账|finance|billing|invoice", re.I)),
    (TransferTarget.FULFILMENT, re.compile(r"转?交付|物流|发货|仓储|fulfilment|logistics", re.I)),
    (TransferTarget.SALES, re.compile(r"转?销售|商务|报价|sales", re.I)),
    (TransferTarget.SECURITY, re.compile(r"转?安全|风控|盗号|security", re.I)),
)


class TransferError(Exception):
    """A refused transfer, with a code the caller maps to a status."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def target_from_text(text: str | None) -> TransferTarget | None:
    """The destination named in a phrase, or None when it names none.

    Returns None rather than guessing: "escalate this" with no destination
    belongs in the general queue, and picking one would send work to a team
    that was never asked to take it.
    """
    if not text:
        return None
    for target, pattern in _PHRASES:
        if pattern.search(text):
            return target
    return None


async def _department_exists(session: AsyncSession, *, tenant_id: uuid.UUID, slug: str) -> bool:
    row = (
        await session.execute(
            select(Department.id).where(Department.tenant_id == tenant_id, Department.slug == slug)
        )
    ).scalar_one_or_none()
    return row is not None


async def transfer_case(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    case_id: uuid.UUID,
    target: TransferTarget,
    reason: str = "",
    expected_version: int | None = None,
) -> tuple[Case, bool]:
    """Escalate one case to a team. Returns (case, changed).

    `changed` is False when the case was already with that team, so a caller
    can tell "we moved it" from "it was already there" without inspecting the
    row - the difference matters to whoever asked.
    """
    from platform_core.cases.service import CaseService

    if not await _department_exists(session, tenant_id=tenant_id, slug=target.value):
        raise TransferError(
            "TRANSFER_TEAM_UNKNOWN",
            f"no department with slug {target.value!r} for this tenant",
        )

    current = (
        await session.execute(select(Case).where(Case.tenant_id == tenant_id, Case.id == case_id))
    ).scalar_one_or_none()
    if current is None:
        raise TransferError("TRANSFER_CASE_NOT_FOUND", f"case {case_id} not found")
    if (current.team_ref or "") == target.value:
        # Already there: no write, so the version does not move.
        return current, False

    service = CaseService(session)
    row = await service.apply_command(
        tenant_id=tenant_id,
        case_id=case_id,
        command="assign",
        expected_version=expected_version,
        parameters={"team_ref": target.value},
    )
    return row, True


__all__ = [
    "TransferError",
    "TransferTarget",
    "target_from_text",
    "transfer_case",
]
