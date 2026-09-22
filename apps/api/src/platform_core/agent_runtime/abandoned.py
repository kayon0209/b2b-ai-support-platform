"""Closing out runs that were accepted and never executed.

The queue endpoint writes a `queued` row before any work happens, so the
caller can be told "accepted" and the request can be counted against quota.
Most placeholders are adopted by `orchestrator._answer_run` and leave `queued`
within seconds. Some never are - the request was dropped, or it came from a
path that does not execute - and until this module existed they stayed
`queued` forever.

Why that is not a cosmetic problem:

- **The quota gate reads the same counter.** `usage_snapshot` is what
  `chat_service.queue_agent_run` consults to refuse a burst with 429. It
  therefore *must* count accepted-but-pending runs, or a thousand concurrent
  requests would all see "under quota" and all be admitted. So the obvious fix
  - ignore rows with no `input_hash` - silently disarms admission control. That
  was the first version of this change; `test_usage_counts_queued_runs` caught
  it, and it was the existing test that was right.
- **Every surface had to re-derive the same judgement.** The replay list, the
  quality metrics and the intent distribution all ask "did this run happen",
  and each answered it differently, because the row itself did not say.

Sweeping them into a terminal state answers the question once, in the row:
`queued` means waiting, `abandoned` means accepted and given up on. Nothing is
deleted - the row keeps its timestamps, tenant and trace id - so "how many
requests did we accept and never serve" stays answerable, and a quota figure
can exclude them without guessing from an age.

Only rows with a `started_at` are eligible. A row with no timestamp has no age,
and a sweep that guesses which timeless rows are old is how it closes something
that was about to run.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.agent_runtime.models import AgentRun, RunStatus

# Bounds one sweep. The retention loop runs per tenant per cycle, so a large
# backlog drains over several cycles rather than holding one long transaction
# against the runs table.
DEFAULT_BATCH = 500


def _affected(result: object) -> int:
    """DML row count.

    `AsyncSession.execute` is typed as returning `Result[Any]`, which does not
    expose `rowcount` - but every DML statement actually returns a
    `CursorResult` that does.
    """
    return int(cast(CursorResult[Any], result).rowcount or 0)


async def abandon_stale_placeholders(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    older_than_seconds: int,
    now: int,
    batch: int = DEFAULT_BATCH,
) -> int:
    """Mark abandoned placeholders. RLS context must be set by the caller.

    Returns how many rows were closed. Idempotent: a row already `abandoned`
    no longer matches the `queued` predicate, so a re-run is a no-op.
    """
    cutoff = now - older_than_seconds
    stale = (
        select(AgentRun.id)
        .where(
            AgentRun.tenant_id == tenant_id,
            AgentRun.status == RunStatus.QUEUED.value,
            # The definition of "never executed" lives in one place
            # (`models.run_executed`), so the sweep and the aggregations cannot
            # disagree about which rows are placeholders.
            AgentRun.input_hash == "",
            AgentRun.started_at.is_not(None),
            AgentRun.started_at < cutoff,
        )
        # Oldest first: a bounded batch should clear the most stale rows, not
        # an arbitrary slice of them.
        .order_by(AgentRun.started_at.asc())
        .limit(batch)
    )
    result = await session.execute(
        update(AgentRun)
        .where(AgentRun.id.in_(stale.scalar_subquery()))
        .values(status=RunStatus.ABANDONED.value)
    )
    return _affected(result)
