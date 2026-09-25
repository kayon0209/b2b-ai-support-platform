"""An agent's correction of an AI answer, and the review it has to pass.

Feature list 7.8 (人工修正回流) - the loop that stops the same wrong answer
being given twice. An agent sees a bad answer, records what it should have
said, and that record goes to a reviewer.

**Nothing here learns automatically.** AGENTS.md prohibits learning from
unreviewed conversations, and a correction is precisely the content that must
not become platform behaviour on the strength of one person's typing - a
confident agent can be wrong, and the platform would then say it with
authority. So the lifecycle is recorded -> reviewed, and only a reviewed
correction may reach the knowledge base.

The corrected answer is stored verbatim. It is the assertion of a person about
what is right, which is the entire value of the row.
"""

from __future__ import annotations

import time
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.identity.tenant_context import TenantContext
from platform_core.knowledge.correction_models import AnswerCorrection, CorrectionStatus


class CorrectionError(Exception):
    """A correction could not be recorded or reviewed."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


async def record_correction(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    agent_run_id: uuid.UUID,
    question: str,
    correct_answer: str,
    note: str | None = None,
) -> AnswerCorrection:
    """Record that an answer was wrong, and what it should have been."""
    if not question.strip():
        raise CorrectionError("EMPTY_QUESTION", "the question is required")
    if not correct_answer.strip():
        raise CorrectionError("EMPTY_ANSWER", "the corrected answer is required")

    stamp = int(time.time())
    correction = AnswerCorrection(
        tenant_id=ctx.tenant_id,
        agent_run_id=agent_run_id,
        question=question.strip()[:2000],
        correct_answer=correct_answer.strip()[:8000],
        note=(note or "").strip()[:2000] or None,
        status=CorrectionStatus.PENDING.value,
        created_by=str(ctx.actor_id) if ctx.actor_id else None,
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(correction)
    await session.flush()
    return correction


async def list_corrections(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    status: str | None = None,
    limit: int = 50,
) -> list[AnswerCorrection]:
    """Corrections awaiting (or having had) review.

    Explicit tenant filter is defence in depth - RLS is the mechanism, and this
    query runs inside a tenant-bound session.
    """
    stmt = select(AnswerCorrection).where(AnswerCorrection.tenant_id == ctx.tenant_id)
    if status is not None:
        stmt = stmt.where(AnswerCorrection.status == status)
    rows = await session.execute(stmt.order_by(AnswerCorrection.created_at.desc()).limit(limit))
    return list(rows.scalars().all())


async def review_correction(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    correction_id: uuid.UUID,
    approve: bool,
) -> AnswerCorrection:
    """Approve or dismiss a correction. Both are terminal.

    Approving does not itself publish anything - it says this correction is
    good enough to become knowledge. Publishing stays a separate act with its
    own review (draft -> review -> publish), which is the point: one person
    typing a correction is not two people agreeing on content.
    """
    row = (
        await session.execute(
            select(AnswerCorrection).where(
                AnswerCorrection.tenant_id == ctx.tenant_id,
                AnswerCorrection.id == correction_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise CorrectionError("NOT_FOUND", "no such correction")
    if row.status != CorrectionStatus.PENDING.value:
        raise CorrectionError("ALREADY_REVIEWED", "this correction was already reviewed")

    row.status = CorrectionStatus.APPROVED.value if approve else CorrectionStatus.DISMISSED.value
    row.reviewed_by = str(ctx.actor_id) if ctx.actor_id else None
    row.reviewed_at = int(time.time())
    row.updated_at = row.reviewed_at
    await session.flush()

    # Approving writes the correction into the knowledge pipeline, so it does
    # not depend on anyone remembering to retype it. It becomes a *draft*, not
    # knowledge: approving checked that the correction is right, not that it
    # reads well as documentation, and publishing keeps its own review.
    if approve:
        await _propose_as_draft(session, ctx=ctx, correction=row)
    return row


async def _propose_as_draft(
    session: AsyncSession, *, ctx: TenantContext, correction: AnswerCorrection
) -> None:
    """Open a gap and draft the corrected answer against it.

    A wrong answer is a knowledge defect, so it joins the same queue as any
    other - reviewers see one list rather than two, and it is published by the
    same reviewed path.
    """
    from platform_core.knowledge import gap_service

    record = await gap_service.record_gap(
        session,
        tenant_id=ctx.tenant_id,
        question=correction.question,
        reason_code="AGENT_CORRECTION",
    )
    if record.gap_id is None:
        # An unclassifiable question is not a reason to lose the correction;
        # it stays approved and visible in the queue.
        return
    await gap_service.create_draft(
        session,
        ctx=ctx,
        gap_id=record.gap_id,
        title=correction.question[:80],
        body=correction.correct_answer,
        # No conversation here. `Correction` carries none, and guessing one
        # from the question text would manufacture a provenance claim this row
        # cannot support - which is worse than an honest NULL.
    )
