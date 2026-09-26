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
from sqlalchemy import select, tuple_
from sqlalchemy import text as sa_text
from sqlalchemy import update as sa_update
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
    """The latest redacted exchange for one conversation, oldest first.

    Tenant scoping is RLS, not a filter: callers run this inside
    `tenant_session`, so a conversation belonging to another tenant returns
    nothing rather than leaking.
    """
    items, _older = await read_timeline_page(session, ref_id=ref_id, limit=limit)
    return items


async def read_timeline_page(
    session: AsyncSession,
    *,
    ref_id: uuid.UUID,
    limit: int,
    before_id: uuid.UUID | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Page backwards without dropping recent replies from a long thread."""
    stmt = select(ConversationTurn).where(ConversationTurn.conversation_ref_id == ref_id)
    if before_id is not None:
        anchor = (
            await session.execute(
                select(ConversationTurn.ts, ConversationTurn.id).where(
                    ConversationTurn.conversation_ref_id == ref_id,
                    ConversationTurn.id == before_id,
                )
            )
        ).one_or_none()
        if anchor is None:
            return [], None
        stmt = stmt.where(tuple_(ConversationTurn.ts, ConversationTurn.id) < tuple_(*anchor))
    rows = (
        (
            await session.execute(
                stmt.order_by(ConversationTurn.ts.desc(), ConversationTurn.id.desc()).limit(
                    limit + 1
                )
            )
        )
        .scalars()
        .all()
    )
    has_older = len(rows) > limit
    rows = list(reversed(rows[:limit]))
    items = [
        {
            "turn_id": str(r.id),
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
    return items, str(rows[0].id) if has_older and rows else None


async def latest_previews(
    session: AsyncSession, *, tenant_id: uuid.UUID, refs: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, Any]]:
    """One latest turn per visible conversation; never return a tool JSON blob."""
    if not refs:
        return {}
    rows = (
        (
            await session.execute(
                select(ConversationTurn)
                .where(
                    ConversationTurn.tenant_id == tenant_id,
                    ConversationTurn.conversation_ref_id.in_(refs),
                )
                .distinct(ConversationTurn.conversation_ref_id)
                .order_by(
                    ConversationTurn.conversation_ref_id,
                    ConversationTurn.ts.desc(),
                    ConversationTurn.id.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    customer_rows = (
        (
            await session.execute(
                select(ConversationTurn)
                .where(
                    ConversationTurn.tenant_id == tenant_id,
                    ConversationTurn.conversation_ref_id.in_(refs),
                    ConversationTurn.role == "customer",
                )
                .distinct(ConversationTurn.conversation_ref_id)
                .order_by(
                    ConversationTurn.conversation_ref_id,
                    ConversationTurn.ts.desc(),
                    ConversationTurn.id.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    customer_text = {row.conversation_ref_id: row.text_redacted[:160] for row in customer_rows}
    return {
        row.conversation_ref_id: {
            "text": "已查询业务数据" if row.role == "tool" else row.text_redacted[:160],
            "role": row.role,
            "at": int(row.ts),
            "customer_text": customer_text.get(row.conversation_ref_id, ""),
        }
        for row in rows
    }


async def search_conversation_refs(
    session: AsyncSession, *, tenant_id: uuid.UUID, term: str
) -> set[uuid.UUID]:
    """Search redacted conversation wording; tenant scope is explicit and RLS backed."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = (
        (
            await session.execute(
                select(ConversationTurn.conversation_ref_id)
                .where(
                    ConversationTurn.tenant_id == tenant_id,
                    ConversationTurn.text_redacted.ilike(f"%{escaped}%", escape="\\"),
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    return set(rows)


async def has_human_reply(
    session: AsyncSession, *, tenant_id: uuid.UUID, ref_id: uuid.UUID
) -> bool:
    row = (
        await session.execute(
            select(ConversationTurn.id)
            .where(
                ConversationTurn.tenant_id == tenant_id,
                ConversationTurn.conversation_ref_id == ref_id,
                ConversationTurn.role == "agent",
                ConversationTurn.source == "agent",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def human_replied_refs(
    session: AsyncSession, *, tenant_id: uuid.UUID, refs: set[uuid.UUID]
) -> set[uuid.UUID]:
    if not refs:
        return set()
    rows = (
        (
            await session.execute(
                select(ConversationTurn.conversation_ref_id)
                .where(
                    ConversationTurn.tenant_id == tenant_id,
                    ConversationTurn.conversation_ref_id.in_(refs),
                    ConversationTurn.role == "agent",
                    ConversationTurn.source == "agent",
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    return set(rows)


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


async def append_system_turn(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    ref_id: uuid.UUID,
    text: str,
) -> ConversationTurn | None:
    """Persist one platform notice. Returns None when it is already there.

    The platform speaking in its own voice - "a colleague has this
    conversation" - rather than the customer or the assistant, which is why the
    role is `system` and why the customer surface renders it without a bubble.

    Two callers need this and they are the reason it exists rather than each
    writing its own insert. One is the out-of-hours notice on the abstain path.
    The other is `support_router`, which writes "someone has this conversation"
    when a customer asks something the AI is no longer allowed to answer - the
    state where the run path *cannot* speak, because the pre-send lease gate
    refuses on a conversation owned by a person or the queue. Without a path
    that can say so, the customer saw nothing at all.

    Not redacted, unlike `append_customer_turn`: this text is authored here, not
    supplied by the customer, so there is no PII to remove and running it
    through the redactor would only risk mangling a time like "09:00".

    Deduplicated on the text, like `append_customer_turn`, and for a reason that
    shows up immediately in use: the notice is written from the request that
    triggered it, so a customer sending three messages while a person owns the
    conversation would otherwise collect three copies of the same sentence.
    """
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
                ConversationTurn.role == "system",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return None

    turn = ConversationTurn(
        tenant_id=tenant_id,
        conversation_ref_id=ref_id,
        role="system",
        text_redacted=text,
        text_hash=digest,
        ts=int(time.time()),
        source="platform",
    )
    session.add(turn)
    await session.flush()
    return turn


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
    external_ref: str | None,
    trigger_message_ref: str,
    idem: str,
    mode: str,
    trace_id: str,
    audit_extra: dict[str, Any] | None = None,
    verified_account: str | None = None,
) -> dict[str, Any]:
    """Queue one agent run for a persisted turn, or raise `QueueRefused`.

    The payload carries the conversation **both** ways, and the worker prefers
    the resolved one:

    - `conversation_ref` - the platform id, already resolved by the caller.
      Always present, because this function receives it.
    - `conversation_id` - the external id this ref was derived from, or absent
      when the caller never had one. Only a caller holding a channel id (the
      visitor session's `visitor_id`, a webhook's conversation id) can supply
      it, and the worker derives from it only in that case.

    Why both: a caller that reached the conversation by its platform ref has no
    external id to give, and handing the worker the ref *as if* it were one
    makes it derive a second time - the run is then filed under a different
    conversation from the turn it answers, and the answer is produced and never
    found. Measured before the fix - the API wrote the turn under
    `6995d1b4-...` while the worker's run landed on `92c2a744-...`.

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
    # The routing fields the worker's consumer reads, written out rather than
    # handed to a generic extractor: this function knows exactly what it means,
    # and a payload it does not understand is a payload it must not guess at.
    minimized: dict[str, Any] = {
        "message_id": str(trigger_message_ref),
        "message_type": "incoming",
        # The resolved platform id. The worker uses it verbatim, which is what
        # lets an operator-initiated run name a conversation directly rather
        # than through a channel id it does not have.
        "conversation_ref": str(conversation_ref_id),
    }
    if external_ref is not None:
        # Only for callers that hold one. The worker derives from this when
        # `conversation_ref` is absent, and the channel delivery path reads it
        # as the address to answer on - neither applies to a run that arrived
        # by platform ref, so an absent key is the honest encoding and not a
        # missing field.
        minimized["conversation_id"] = str(external_ref)
    # Feature 2.5: the account this visitor proved ownership of, so the worker
    # can refuse to publish somebody else's order. Omitted entirely for operator
    # runs (verified_account is None) - the worker reads its *absence* as "no
    # gate", and must not confuse it with an anonymous visitor's empty string,
    # which would gate the operator out of reading orders. The visitor surface
    # always passes a string ("" when unverified), so only operators leave this
    # key absent.
    if verified_account is not None:
        minimized["verified_account"] = verified_account

    result = await inbox.persist_inbox_event(
        session,
        tenant_id=ctx.tenant_id,
        delivery_id=f"agent-run:{idem}",
        event_type="message_created",
        raw_body=b"",
        minimized_payload=minimized,
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


async def supersede_queued_runs(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_ref_id: uuid.UUID,
) -> int:
    """Close out the runs a conversation was still holding when ownership moved.

    Returns the number of runs closed, so the caller knows whether anyone needs
    telling.

    The runs already exist: a message accepted a moment ago wrote its turn and
    its `queued` run in the same transaction, and the tenant's monthly quota
    already counts it. When the conversation then leaves the AI - a
    clarification limit, an out-of-hours abstain, a safety refusal - nothing
    in the run path owns those rows any more. Each would otherwise sit in
    `queued` until a worker picked it up, discovered the conversation is no
    longer the AI's to answer, and recorded it as `handed_off`: a status that
    asserts a person is on it, which is exactly what has not happened.

    So the state is made explicit here, at the moment ownership moves, rather
    than inferred later by whichever worker gets there first. Two consequences,
    both the point:

    - the operator list can tell "waiting in the queue" from "a colleague has
      this", which the shared status made impossible;
    - the customer can be told, because the count is known at the point the
      lease changes instead of never.

    Only `queued` is touched. A run that already reached an outcome is a
    decision, and rewriting it would erase the record of what was answered.
    """
    result = await session.execute(
        sa_update(AgentRun)
        .where(
            AgentRun.tenant_id == tenant_id,
            AgentRun.conversation_ref_id == conversation_ref_id,
            AgentRun.status == RunStatus.QUEUED.value,
        )
        # No `started_at`: these runs never started, and a run that says
        # otherwise is indistinguishable from a slow one when the replay is
        # read.
        .values(status=RunStatus.SUPERSEDED.value)
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
