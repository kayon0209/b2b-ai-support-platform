"""Feature list 7.10: ask for a satisfaction score, and keep it.

The design questions this answers:

- **When is it worth asking?** Only for a conversation that reached a
  resolution or a handoff - not for one the customer abandoned mid-sentence,
  and not for a message the platform refused as out of scope. Asking "were you
  satisfied?" about an interaction that never happened is how a survey becomes
  noise, and a low score from a non-interaction drags the average down for no
  reason anyone can act on.
- **What is the question?** It travels through `adapt_for_channel` (5.2), so an
  SMS customer gets the short plain-text form and an email customer gets the
  full one. A survey that arrives with Markdown asterisks in it is a survey
  nobody finishes.
- **What happens on a second answer?** The last one wins, without a second row.
  People re-tap links; the alternative is that re-tapping silently doubles a
  conversation's weight in every average from then on.

The score is validated here *and* constrained in the database (migration 0044).
The duplication is deliberate: the check gives a caller a clear error, and the
constraint guarantees the invariant even for a path nobody wrote yet.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.support_bridge.channel_format import adapt_for_channel
from platform_core.support_bridge.csat_models import CsatResponse

MIN_SCORE = 1
MAX_SCORE = 5

_SURVEY_QUESTION = (
    "您对本次服务的整体体验满意吗？回复 1-5 分（5 为非常满意）即可，也可以补充一句想说的话。"
)
# The full prompt above is ~45 characters, so it survives even the SMS ceiling
# unaided; the channel adapter is still applied because the comment-scoring
# follow-up and any future wording must not assume that.
_SURVEY_SHORT = "本次服务您满意吗？回复 1-5 分（5 为非常满意）。"


class CsatError(Exception):
    """A refused score, with a code the API can map to a status."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class CsatSummary:
    """What the dashboard shows (8.5)."""

    responses: int
    average: float | None
    distribution: dict[int, int]


def survey_text(channel: str | None) -> str:
    """The survey prompt as it should be sent on `channel`."""
    adapted = adapt_for_channel(_SURVEY_QUESTION, channel=channel)
    # A channel that truncated the prompt would be asking a question the
    # customer cannot see the instructions for; fall back to the short form
    # rather than sending half a question.
    if not adapted.endswith(("。", "…")) or "1-5" not in adapted:
        return adapt_for_channel(_SURVEY_SHORT, channel=channel)
    return adapted


async def record_response(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_ref_id: uuid.UUID,
    score: int,
    comment: str | None = None,
    channel: str | None = None,
    case_id: uuid.UUID | None = None,
    agent_run_id: uuid.UUID | None = None,
) -> CsatResponse:
    """Record, or replace, the score for one conversation.

    Replaces rather than inserts a second row: the unique constraint in 0044
    would reject the insert, and rejecting a customer's re-tap with a 500 is
    worse than honouring it.
    """
    if not isinstance(score, int) or isinstance(score, bool):
        raise CsatError("CSAT_SCORE_INVALID", f"score must be an integer, got {score!r}")
    if not MIN_SCORE <= score <= MAX_SCORE:
        raise CsatError(
            "CSAT_SCORE_OUT_OF_RANGE", f"score must be {MIN_SCORE}..{MAX_SCORE}, got {score}"
        )

    now = int(time.time())
    existing = (
        await session.execute(
            select(CsatResponse).where(
                CsatResponse.tenant_id == tenant_id,
                CsatResponse.conversation_ref_id == conversation_ref_id,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.score = score
        existing.comment = comment
        existing.channel = channel or existing.channel
        existing.updated_at = now
        await session.flush()
        return existing

    row = CsatResponse(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        conversation_ref_id=conversation_ref_id,
        case_id=case_id,
        agent_run_id=agent_run_id,
        score=score,
        comment=comment,
        channel=channel,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.flush()
    return row


async def csat_summary(
    session: AsyncSession, *, tenant_id: uuid.UUID, window_seconds: int = 30 * 24 * 3600
) -> CsatSummary:
    """Responses in the trailing window: count, mean, and the spread.

    The distribution is returned alongside the mean because the mean hides the
    thing that matters: 3.0 from everyone and 3.0 from half fives and half ones
    are the same number and completely different problems.
    """
    cutoff = int(time.time()) - window_seconds
    rows = (
        await session.execute(
            select(CsatResponse.score).where(
                CsatResponse.tenant_id == tenant_id,
                CsatResponse.created_at >= cutoff,
            )
        )
    ).scalars()
    scores = [int(score) for score in rows]
    if not scores:
        return CsatSummary(responses=0, average=None, distribution={})
    distribution = {value: scores.count(value) for value in range(MIN_SCORE, MAX_SCORE + 1)}
    return CsatSummary(
        responses=len(scores),
        average=round(sum(scores) / len(scores), 2),
        distribution=distribution,
    )


async def response_rate(
    session: AsyncSession, *, tenant_id: uuid.UUID, window_seconds: int = 30 * 24 * 3600
) -> float | None:
    """Scores received divided by conversations that were asked.

    Without this, a 4.8 average from three responses reads as a healthy
    platform. `None` when nothing was asked, because a rate over no
    conversations is not zero - it is undefined, and reporting it as 0.0 would
    look like every customer ignored the survey.
    """
    from platform_core.agent_runtime.models import AgentRun

    cutoff = int(time.time()) - window_seconds
    asked = int(
        (
            await session.execute(
                select(func.count(func.distinct(AgentRun.conversation_ref_id))).where(
                    AgentRun.tenant_id == tenant_id,
                    AgentRun.started_at.is_not(None),
                    AgentRun.started_at >= cutoff,
                )
            )
        ).scalar_one()
    )
    if asked == 0:
        return None
    summary: Any = await csat_summary(session, tenant_id=tenant_id, window_seconds=window_seconds)
    return round(summary.responses / asked, 3)


__all__ = [
    "MAX_SCORE",
    "MIN_SCORE",
    "CsatError",
    "CsatSummary",
    "csat_summary",
    "record_response",
    "response_rate",
    "survey_text",
]
