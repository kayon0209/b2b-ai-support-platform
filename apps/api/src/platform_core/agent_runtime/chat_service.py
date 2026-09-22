"""Conversation operations shared by every chat surface.

Three places accept a customer question or queue the work for one: the operator
console's verification panel (`customer_router`), the visitor chat window
(`support_router`), and the run endpoint (`router`). The read, the
redact-and-dedupe append and the quota-gated enqueue are the same work in all
three; writing them out three times is how two surfaces that are supposed to
agree quietly stop agreeing.

**What is deliberately NOT shared is the authorization.** The operator panel
gates on `CASE_READ` / `CASE_UPDATE`; a visitor holds a token bound to one
conversation and has no role at all. Those are different questions and each
router answers its own.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.agent_runtime.models import AgentRun, ConversationTurn, RunStatus
from platform_core.agent_runtime.tool_card import build_card
from platform_core.audit import service as audit_service
from platform_core.config import get_settings
from platform_core.evaluation.pii import redact_text
from platform_core.identity.tenant_context import TenantContext
from platform_core.identity.usage import usage_snapshot
from platform_core.support_bridge import inbox
from platform_core.support_bridge.minimize import payload_hash
from platform_core.support_bridge.models import InboxEvent, InboxEventStatus


async def read_timeline(
    session: AsyncSession, *, ref_id: uuid.UUID, limit: int
) -> list[dict[str, Any]]:
    """The redacted exchange for one conversation, oldest first.

    Tenant scoping is RLS, not a filter: callers run this inside
    `tenant_session`, so a conversation belonging to another tenant returns
    nothing rather than leaking.
    """
    rows = (
        (
            await session.execute(
                select(ConversationTurn)
                .where(ConversationTurn.conversation_ref_id == ref_id)
                .order_by(ConversationTurn.ts.asc(), ConversationTurn.id.asc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "role": r.role,
            "text": r.text_redacted,
            "at": r.ts,
            "source": getattr(r, "source", "") or "",
            # A `tool` turn's `text` is the receipt itself - JSON, not a
            # sentence. Handing the parsed card over alongside it means a
            # surface decides how to render by looking at structure, instead of
            # trying to parse redacted text and printing raw JSON when it
            # cannot. `text` is kept as-is so an existing reader that expects
            # the receipt keeps working.
            "card": build_card(r.text_redacted) if r.role == "tool" else None,
        }
        for r in rows
    ]


async def append_customer_turn(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    ref_id: uuid.UUID,
    text: str,
) -> tuple[ConversationTurn, bool]:
    """Persist one customer turn. Returns (turn, was_duplicate).

    The text is redacted before storage, matching how Chatwoot-sourced turns are
    written: the platform must not hold raw customer PII. Retention is not
    invented here -- `RetentionPolicy.conversation_turn_days` already governs
    `conversation_turns` and its sweep prunes expired rows, so this copy stays a
    bounded cache rather than a second system of record.

    The check-and-insert is serialised with a transaction-scoped advisory lock.
    The pair is a race on its own: ten simultaneous submissions of the same text
    all read nothing and all insert -- measured, not theorised, at four rows
    from ten parallel requests. Sequential tests cannot see it, which is why the
    suite was green while the table held duplicate groups. An advisory lock
    rather than a unique index, because the index would also constrain the
    Chatwoot ingestion path, which records messages this endpoint never sees and
    has its own reasons to keep every one of them.
    """
    redacted, _count = redact_text(text)
    digest = payload_hash(text.encode())

    await session.execute(
        sa_text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"{tenant_id}:{ref_id}:{digest}"},
    )

    existing = (
        await session.execute(
            select(ConversationTurn)
            .where(
                ConversationTurn.conversation_ref_id == ref_id,
                ConversationTurn.text_hash == digest,
                ConversationTurn.role == "customer",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, True

    turn = ConversationTurn(
        tenant_id=tenant_id,
        conversation_ref_id=ref_id,
        role="customer",
        text_redacted=redacted,
        text_hash=digest,
        ts=int(time.time()),
        source="platform",
    )
    session.add(turn)
    await session.flush()
    return turn, False


class QueueRefused(Exception):
    """A gate declined the run. Carries the response the caller should return."""

    def __init__(self, response: object) -> None:
        super().__init__("agent run refused")
        self.response = response


async def queue_agent_run(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    conversation_ref_id: uuid.UUID,
    external_ref: str,
    trigger_message_ref: str,
    idem: str,
    mode: str,
    trace_id: str,
    audit_extra: dict[str, Any] | None = None,
    verified_account: str | None = None,
) -> dict[str, Any]:
    """Queue one agent run for a persisted turn, or raise `QueueRefused`.

    `external_ref` is the **raw** id the conversation ref was derived from, not
    the ref itself. The worker re-derives the ref from this payload, so handing
    it an already-derived value makes it derive twice: the run is then filed
    under a different conversation than the turn it answers, and the answer is
    produced and never found. Measured before the fix - the API wrote the turn
    under `6995d1b4-...` while the worker's run landed on `92c2a744-...`.

    Two gates run before any model spend, and both refuse with 429 on purpose: a
    caller must be able to tell "declined for capacity" from "no supporting
    evidence", which would otherwise look identical -- no answer either way.

    `audit_extra` is lineage the caller wants on the `agent_run.queued` event
    (the run endpoint records the control version it was asked to expect there).
    It is a parameter rather than something each caller writes itself, because a
    second audit call site is how the two events drift apart.
    """
    # Quota is the tenant's monthly budget for agent runs.
    usage = await usage_snapshot(session, tenant_id=ctx.tenant_id)
    if usage.over_quota:
        from platform_core.api import error_response

        raise QueueRefused(
            error_response(
                "QUOTA_EXCEEDED",
                f"monthly agent-run quota ({usage.quota}) is exhausted",
                status_code=429,
                details={"quota": usage.quota, "runs_used": usage.runs_used},
            )
        )

    # Backpressure: a bounded backlog beats unbounded latency. Depth is global
    # (all tenants), which is honest -- the workers, not any one tenant, are the
    # shared resource being protected.
    depth = int(
        (
            await session.execute(
                select(sa_func.count())
                .select_from(InboxEvent)
                .where(InboxEvent.status == InboxEventStatus.RECEIVED.value)
            )
        ).scalar_one()
    )
    max_depth = int(get_settings().queue_max_depth)
    if depth >= max_depth:
        from platform_core.api import error_response

        raise QueueRefused(
            error_response(
                "QUEUE_SATURATED",
                f"inbox depth {depth} reached the configured cap ({max_depth}); retry with backoff",
                status_code=429,
                details={"depth": depth, "max_depth": max_depth},
            )
        )

    # The inbox row is keyed by delivery id, which gives this the same
    # at-least-once + dedup semantics as a webhook delivery.
    raw_payload = {
        "event": "message_created",
        "id": trigger_message_ref,
        "message_type": "incoming",
        "conversation": {"id": external_ref},
    }
    # Feature 2.5: the account this visitor proved ownership of, so the worker
    # can refuse to publish somebody else's order. Omitted entirely for operator
    # runs (verified_account is None) - the worker reads its *absence* as "no
    # gate", and must not confuse it with an anonymous visitor's empty string,
    # which would gate the operator out of reading orders. The visitor surface
    # always passes a string ("" when unverified), so only operators leave this
    # key absent.
    if verified_account is not None:
        raw_payload["verified_account"] = verified_account

    result = await inbox.persist_inbox_event(
        session,
        tenant_id=ctx.tenant_id,
        delivery_id=f"agent-run:{idem}",
        event_type="message_created",
        raw_body=b"",
        raw_payload=raw_payload,
    )
    if result.duplicate:
        return {
            "status": RunStatus.QUEUED.value,
            "conversation_ref": str(conversation_ref_id),
            "duplicate": True,
            "idempotency_key": idem,
        }

    run = AgentRun(
        tenant_id=ctx.tenant_id,
        conversation_ref_id=conversation_ref_id,
        route="knowledge_qa",
        status=RunStatus.QUEUED.value,
        # `started_at` is the quality dashboard's window column. Leaving it
        # unset made every run invisible to `/v1/quality/metrics`.
        started_at=int(time.time()),
        model_config={"mode": mode},
        retrieval_config={},
        policy_version="v1",
        code_version="0.1.0",
        trace_id=trace_id,
        input_hash="",
        token_usage={},
    )
    session.add(run)
    await session.flush()

    await audit_service.record(
        session,
        ctx=ctx,
        action="agent_run.queued",
        resource_type="agent_run",
        resource_id=run.id,
        decision="completed",
        reason_code="OK",
        after={
            "mode": mode,
            "inbox_event_id": str(result.event_id),
            **(audit_extra or {}),
        },
        trace_id=trace_id,
    )
    return {
        "run_id": str(run.id),
        "status": RunStatus.QUEUED.value,
        "conversation_ref": str(conversation_ref_id),
        "idempotency_key": idem,
    }
