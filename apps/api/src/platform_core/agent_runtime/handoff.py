"""Handing a conversation over, and everything that has to happen with it.

The lease change is one line. The consequences are the work.

A conversation leaves the AI for any of several reasons - the clarification
limit was reached, it is outside opening hours, a safety rule refused, the
evidence conflicts. In every one of them the platform may no longer answer
anything in that conversation, and the customer may already have several
questions sitting in the queue that it accepted a moment earlier. Those runs
exist, the tenant's monthly quota has already counted them, and after the
handoff nothing owns them.

Left alone, each one is eventually picked up by a worker, stopped by the
ownership guard, and recorded as `handed_off` - a status that asserts a person
is on the question. Measured on a live stack: twenty messages in one
conversation, three replies out, and eighteen runs reported as handed to a
human. Nobody had been handed anything. The questions were in a queue, the
customer had been told nothing, and every list, replay and outcome metric read
the same comfortable story.

So the handover is one operation here rather than three scattered call sites:

1. move the lease to the human queue;
2. close out the runs this conversation was still holding, with a status that
   says what actually happened - accepted, never executed, nobody on it;
3. tell the customer once, with the count, if anything was closed.

The notice is the only part that genuinely cannot be done later. It is written
from the moment ownership moves because that is the only moment the count is
known; by the time a worker reaches a stale run, the customer has long since
been shown silence.

The workbench "release" path is deliberately not routed through here. It
requires a human to already own the conversation, so the questions are in
front of a person and the workbench itself is the notice.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.agent_runtime import chat_service
from platform_core.identity import lease_service


async def hand_off_to_human_queue(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_ref_id: uuid.UUID,
    reason: str,
) -> int:
    """Move the conversation to the human queue and settle what it leaves behind.

    Returns the number of runs closed out, which is also the number of queued
    questions the customer has just been told are waiting. Zero means there was
    nothing pending and no notice is written - a notice with no questions to
    explain is noise the customer has to read for nothing.

    Callers must have bound `app.tenant_id` in *this* transaction. Every
    function reached from here filters on `tenant_id`, and under RLS an
    unbound transaction matches nothing - which surfaces as "lease row
    missing" rather than as a permission error, so it is easy to misread.
    """
    await lease_service.release_to_queue(
        session,
        tenant_id=tenant_id,
        conversation_ref_id=conversation_ref_id,
        reason=reason,
    )
    orphaned = await chat_service.supersede_queued_runs(
        session,
        tenant_id=tenant_id,
        conversation_ref_id=conversation_ref_id,
    )
    if orphaned:
        await chat_service.append_system_turn(
            session,
            tenant_id=tenant_id,
            ref_id=conversation_ref_id,
            text=(
                f"你刚才一并提出的另外 {orphaned} 个问题已加入人工队列，"
                "会由客服按顺序处理，AI 不会重复回答它们。"
            ),
        )
    return orphaned


__all__ = ["hand_off_to_human_queue"]
