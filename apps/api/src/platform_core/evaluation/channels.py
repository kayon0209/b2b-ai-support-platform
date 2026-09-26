"""Per-channel volume and automation rate (feature list 8.2's other axis).

8.2 asks for the automation rate "按业务线、按问题类型拆分". This is the third
axis and the one an operator reaches for first, because a channel is something
they can *act* on: "the WeChat rate is half the web rate" is a finding, and
"the rate is 61%" is not.

Where the channel comes from
----------------------------
`conversation_contacts.channel`, written by the inbound adapters (migration 0050
wired the writer). Three buckets, and the distinction between them is the point:

- a named channel - the answer;
- **a contact row with no channel** - the platform knows *who* but not *where*.
  A CRM sync often cannot name one, and folding these into a named channel would
  invent an attribution;
- **no contact row at all** - the platform's own surface (`/support`), and
  anything predating migration 0045. Reported rather than dropped, because this
  is where the platform-surface traffic lands and a reader who cannot see it
  would wonder why the channels do not add up to the total.

The automation rate is `categories.automated_run_ids` - the same strict rule the
per-category report uses, deliberately shared rather than restated. A run counts
as automated only if it completed, recorded no abstention, and the customer did
not have to ask again; a per-channel rate computed more loosely would disagree
with the per-category one and neither number would be trusted.
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.agent_runtime.models import AgentRun, RunStatus, run_executed
from platform_core.evaluation.categories import automated_run_ids
from platform_core.support_bridge.continuity_models import ConversationContact

# Bounded so a dashboard cannot run an unbounded scan. Same discipline as
# `categories.MAX_RUNS_SCANNED`.
MAX_RUNS_SCANNED = 20000

# The buckets for traffic the channel column cannot name. Not channel names, so
# they cannot collide with a real one.
UNLINKED = "(unlinked)"
UNNAMED = "(unnamed)"


@dataclass
class ChannelStat:
    channel: str
    runs: int = 0
    automated: int = 0
    escalated: int = 0
    # None over an empty channel - the same rule as everywhere else. A channel
    # with no traffic has no rate, and 0.0 would read as "everything escalated".
    automation_rate: float | None = None


@dataclass
class ChannelDistribution:
    window_seconds: int
    total_runs: int = 0
    unlinked_runs: int = 0
    unnamed_channel_runs: int = 0
    by_channel: dict[str, ChannelStat] = field(default_factory=dict)
    truncated: bool = False

    @property
    def automation_rate(self) -> float | None:
        measured = sum(s.runs for s in self.by_channel.values())
        if not measured:
            return None
        return round(sum(s.automated for s in self.by_channel.values()) / measured, 4)


async def aggregate_channel_distribution(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    window_seconds: int = 7 * 24 * 3600,
) -> ChannelDistribution:
    """Volume and automation rate per channel. RLS context is the caller's."""
    cutoff = _now() - window_seconds
    rows = (
        (
            await session.execute(
                select(AgentRun)
                .where(
                    AgentRun.tenant_id == tenant_id,
                    AgentRun.started_at.is_not(None),
                    AgentRun.started_at >= cutoff,
                    run_executed(),
                )
                .order_by(AgentRun.started_at)
                .limit(MAX_RUNS_SCANNED + 1)
            )
        )
        .scalars()
        .all()
    )
    runs = list(rows[:MAX_RUNS_SCANNED])

    result = ChannelDistribution(
        window_seconds=window_seconds, truncated=len(rows) > MAX_RUNS_SCANNED
    )
    if not runs:
        return result

    # One query for every contact row involved, not one per run.
    refs = {run.conversation_ref_id for run in runs}
    contacts = {
        row.conversation_ref_id: row.channel
        for row in (
            await session.execute(
                select(ConversationContact.conversation_ref_id, ConversationContact.channel).where(
                    ConversationContact.tenant_id == tenant_id,
                    ConversationContact.conversation_ref_id.in_(refs),
                )
            )
        ).all()
    }

    automated = automated_run_ids(runs)
    result.total_runs = len(runs)
    for run in runs:
        if run.conversation_ref_id not in contacts:
            key = UNLINKED
            result.unlinked_runs += 1
        else:
            named = (contacts[run.conversation_ref_id] or "").strip()
            key = named or UNNAMED
            if not named:
                result.unnamed_channel_runs += 1

        stat = result.by_channel.get(key)
        if stat is None:
            stat = ChannelStat(channel=key)
            result.by_channel[key] = stat
        stat.runs += 1
        if run.id in automated:
            stat.automated += 1
        elif run.status in (RunStatus.ABSTAINED.value, RunStatus.HANDED_OFF.value):
            stat.escalated += 1

    for stat in result.by_channel.values():
        stat.automation_rate = round(stat.automated / stat.runs, 4) if stat.runs else None
    return result


def _now() -> int:
    import time

    return int(time.time())
