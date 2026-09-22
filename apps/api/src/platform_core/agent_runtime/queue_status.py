"""Feature list 1.7: queue position and waiting experience.

What this can and cannot know, and why the difference is the design:

- **Position is real.** It is counted from the control leases that are
  currently owned by the queue, ordered by when each conversation entered it.
  No estimation, no model, no smoothing.
- **Wait time is only given when there is a source for it.** The platform has
  no historical record of how long a human takes to pick up a conversation:
  `cases` has no creation timestamp, and a lease row only holds its *current*
  owner, not how long it waited. Deriving "about 5 minutes" from nothing would
  be the exact fabrication ADR 0006 forbids - a number the customer would
  reasonably act on and the platform cannot defend.

So the estimate comes from an operator-declared average handling time, and
when that is unset the reply says how many people are ahead and stops there.
"Three people ahead of you" is true and actionable; "about 8 minutes" invented
from no data is neither.

Outside opening hours the wait is not the point, so `hours.offline_notice`
(which names when someone will look) is used instead - the two are mutually
exclusive by design, never concatenated.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from sqlalchemy import func, select

from platform_core.agent_runtime.hours import is_open
from platform_core.identity.control_lease import ConversationControlLease

# Set by an operator from their own figures (queue service, historical
# averages, SLA report). 0 means "no source", which is the honest default:
# the alternative is a hardcoded guess that looks authoritative.
_AVG_HANDLE_ENV = "APP_QUEUE_AVG_HANDLE_MINUTES"

# Below this many completed samples an average is noise, not a measurement.
_MIN_SAMPLES_FOR_ESTIMATE = 1


@dataclass(frozen=True)
class QueueStatus:
    """Where this conversation sits in the human queue."""

    position: int  # 1-based; 0 when the conversation is not queued
    ahead: int  # how many conversations are ahead of it
    estimated_wait_minutes: int | None  # None = no source, deliberately
    open_now: bool

    @property
    def queued(self) -> bool:
        return self.position > 0


def _avg_handle_minutes() -> int | None:
    """The operator-declared average, or None when nobody declared one."""
    raw = os.environ.get(_AVG_HANDLE_ENV, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


async def queue_status(session, *, tenant_id, conversation_ref_id) -> QueueStatus:
    """Count how many queue-owned conversations entered before this one."""
    mine = (
        await session.execute(
            select(ConversationControlLease).where(
                ConversationControlLease.tenant_id == tenant_id,
                ConversationControlLease.conversation_ref_id == conversation_ref_id,
            )
        )
    ).scalar_one_or_none()

    open_now = is_open()

    if mine is None or mine.owner_type != "queue":
        return QueueStatus(position=0, ahead=0, estimated_wait_minutes=None, open_now=open_now)

    ahead = int(
        (
            await session.execute(
                select(func.count())
                .select_from(ConversationControlLease)
                .where(
                    ConversationControlLease.tenant_id == tenant_id,
                    ConversationControlLease.owner_type == "queue",
                    ConversationControlLease.updated_at < mine.updated_at,
                )
            )
        ).scalar_one()
    )

    avg = _avg_handle_minutes()
    # Position includes this conversation, so the wait is what is ahead of
    # it - a conversation being served right now is not waiting for itself.
    estimated = (ahead + 1) * avg if avg is not None else None

    return QueueStatus(
        position=ahead + 1,
        ahead=ahead,
        estimated_wait_minutes=estimated,
        open_now=open_now,
    )


def queue_notice(status: QueueStatus) -> str | None:
    """What to append to a handoff notice, or None when there is nothing true
    to add.

    Returns None for a conversation that is not queued, so a customer who is
    not waiting is not told they are. Saying "you are number 1 in the queue"
    to someone whose conversation went straight to a person is a small lie
    that costs trust and buys nothing.
    """
    if not status.queued:
        return None
    if status.ahead <= 0:
        return "正在为您接入人工同事。"
    text = f"您已进入人工队列，前面还有 {status.ahead} 位。"
    if status.estimated_wait_minutes is not None:
        text += f"预计等待约 {status.estimated_wait_minutes} 分钟。"
    return text
