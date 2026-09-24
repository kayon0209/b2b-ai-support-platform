"""Agent run API (docs/api-contracts.md agent run API).

POST /v1/conversations/{conversation_ref}/agent-runs

Queues an agent run for a conversation and returns immediately.

`{conversation_ref}` is the platform's own conversation id everywhere under
this prefix - the value `GET /v1/conversations` lists and the value
`POST /v1/support/sessions` returns - and it is used verbatim. It used to be
read as an external id and derived a second time, which is a silent failure:
the listing returned refs, `/replay` derived them, and the console showed a
different conversation's transcript. See
`support_bridge.conversation_ref` for the one rule.

Contract notes:
- The channel adapter persists the inbound event before queueing this work.
  This endpoint records the run request and returns `status: "queued"`;
  generation and delivery happen in the worker, where the pre-send lease
  re-check lives.
- The endpoint therefore enqueues through the same transactional inbox the
  signed channel adapters use. That reuses the claim/ack/idempotency path
  instead of introducing a second queue with its own semantics.
- `expected_control_version` is recorded for audit. The authoritative
  compare-and-set still happens in the orchestrator immediately before
  dispatch, because the lease can move between queueing and sending.
"""

from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from platform_core.agent_runtime import chat_service, replay
from platform_core.agent_runtime.models import VALID_MODES, AgentRun
from platform_core.api import (
    IDEMPOTENCY_KEY_REQUIRED,
    NOT_FOUND,
    VALIDATION_FAILED,
    error_response,
    get_context,
    new_trace_id,
    ok_response,
    require_idempotency_key,
    require_policy,
    tenant_session,
)
from platform_core.support_bridge.conversation_ref import parse_conversation_ref
from platform_policy import Action

router = APIRouter(prefix="/v1/conversations", tags=["agent-runtime"])

# Imported from the model module so the API cannot accept a mode the
# orchestrator does not implement.
VALID_MODES = VALID_MODES


class AgentRunIn(BaseModel):
    trigger_message_ref: str = Field(min_length=1, max_length=255)
    mode: str = Field(default="customer_reply")
    expected_control_version: int | None = Field(default=None, ge=1)


@router.post("/{conversation_ref}/agent-runs")
async def create_agent_run(request: Request, conversation_ref: str, body: AgentRunIn) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)

    # Queuing a run can lead to a customer-visible reply, so it is gated as
    # a case write rather than a read.
    denied = require_policy(ctx, Action.CASE_UPDATE)
    if denied is not None:
        return denied

    if body.mode not in VALID_MODES:
        return error_response(
            VALIDATION_FAILED, f"mode must be one of {sorted(VALID_MODES)}", status_code=400
        )

    idem = require_idempotency_key(request)
    if not idem:
        return error_response(
            IDEMPOTENCY_KEY_REQUIRED,
            "queuing an agent run requires an Idempotency-Key header",
            status_code=400,
        )

    try:
        conversation_ref_id = parse_conversation_ref(conversation_ref)
    except ValueError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        # One implementation of "queue a run", shared with the visitor chat
        # surface (`chat_service.queue_agent_run`). The quota gate, the
        # backpressure gate, the inbox row and the placeholder run are the same
        # four decisions in both, and a second copy is how two surfaces that are
        # supposed to agree quietly stop agreeing about what a refused run looks
        # like. `QueueRefused` carries the response to return, so the 429s keep
        # their codes and details.
        try:
            queued = await chat_service.queue_agent_run(
                session,
                ctx=ctx,
                conversation_ref_id=conversation_ref_id,
                # No external id: this caller reached the conversation by its
                # platform ref, so there is nothing left to derive from. The
                # payload carries the ref itself instead, and the worker uses
                # it verbatim - handing over an id for the worker to derive is
                # exactly how the run used to be filed under a second
                # conversation from the turn it was answering.
                external_ref=None,
                trigger_message_ref=body.trigger_message_ref,
                idem=idem,
                mode=body.mode,
                trace_id=trace_id,
                # Recorded for audit; the authoritative compare-and-set is in the
                # orchestrator, immediately before dispatch (module docstring).
                audit_extra={"expected_control_version": body.expected_control_version},
            )
        except chat_service.QueueRefused as refused:
            return refused.response

    return ok_response(queued, trace_id=trace_id)


@router.get("/{conversation_ref}/agent-runs")
async def list_agent_runs(
    request: Request,
    conversation_ref: str,
    limit: int = Query(default=20, ge=1, le=100),
) -> Any:
    """Recent runs for a conversation, newest first.

    Exposes status, route and the abstention reason so the admin UI can
    explain why a run abstained or handed off without reading the database.
    """
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)
    denied = require_policy(ctx, Action.CASE_READ)
    if denied is not None:
        return denied

    try:
        conversation_ref_id = parse_conversation_ref(conversation_ref)
    except ValueError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)

    async with tenant_session(ctx) as session:
        rows = (
            (
                await session.execute(
                    select(AgentRun)
                    .where(AgentRun.conversation_ref_id == conversation_ref_id)
                    # AgentRun carries no created_at column, so the UUIDv7
                    # primary key (time-ordered) is the ordering key.
                    .order_by(AgentRun.id.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        items = [
            {
                "run_id": str(r.id),
                "status": r.status,
                "route": r.route,
                "abstain_reason": r.abstain_reason,
                "latency_ms": r.latency_ms,
                "trace_id": r.trace_id,
            }
            for r in rows
        ]

    return ok_response({"items": items}, trace_id=new_trace_id())


@router.get("")
async def list_conversations(
    request: Request,
    limit: int = Query(default=25, ge=1, le=replay.MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> Any:
    """Conversations this tenant can replay, most recently active first.

    Feature list 8.3. Before this existed a conversation could only be reached
    by already holding its id - and no surface returns one - so a replay screen
    would have had nothing to open.

    Gated as a case read: the exchange is customer content, and reading it is
    the same permission as reading the case it belongs to.
    """
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)
    denied = require_policy(ctx, Action.CASE_READ)
    if denied is not None:
        return denied

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        listing = await replay.list_conversations(
            session, tenant_id=ctx.tenant_id, limit=limit, offset=offset
        )
    return ok_response(listing, trace_id=trace_id)


@router.get("/{conversation_ref}/replay")
async def conversation_replay(
    request: Request,
    conversation_ref: str,
    turn_limit: int = Query(default=replay.DEFAULT_TURN_LIMIT, ge=1, le=1000),
    run_limit: int = Query(default=replay.DEFAULT_RUN_LIMIT, ge=1, le=200),
) -> Any:
    """One conversation: what was said, and every decision taken in it.

    The path segment is the platform ref that `GET /v1/conversations` lists,
    used verbatim. It used to be treated as an external id and derived again,
    which is why opening a conversation from the console showed a second
    conversation's transcript - or nothing at all.
    """
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)
    denied = require_policy(ctx, Action.CASE_READ)
    if denied is not None:
        return denied

    try:
        conversation_ref_id = parse_conversation_ref(conversation_ref)
    except ValueError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        bundle = await replay.build_replay(
            session,
            tenant_id=ctx.tenant_id,
            conversation_ref_id=conversation_ref_id,
            turn_limit=turn_limit,
            run_limit=run_limit,
        )
    if bundle is None:
        return error_response(NOT_FOUND, "conversation not found", status_code=404)
    return ok_response(bundle, trace_id=trace_id)


@router.get("/{conversation_ref}/related")
async def get_related_conversations(
    request: Request,
    conversation_ref: str,
    limit: int = Query(default=10, ge=1, le=50),
) -> Any:
    """Other conversations the same person had, newest first.

    Feature 1.5's payoff, and the consumer `continuity.prior_conversations` never
    had. That function has existed since migration 0045 and was called **only
    from its own tests**, so the feature was built, tested and unreachable: a
    customer who asked on WeChat and then wrote an email was one person the
    platform could not connect.

    Returns an **empty list, not a 404**, when the conversation has no contact.
    An anonymous visitor and a conversation predating the table are both real
    states, and "we do not know who this is" is an answer rather than an error -
    a 404 would make a client treat a normal case as a failure.

    The path segment is the platform ref, like every other `conversation_ref`
    segment. It was the raw external string before, which is what made a
    channel conversation (`<root@acme.test>`) addressable here and not at
    `/replay`. Both are addressable now: the platform ref exists for a channel
    conversation too - the adapter derives it when it ingests - so the caller
    needs the ref rather than the channel's own key.
    """
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)
    denied = require_policy(ctx, Action.CASE_READ)
    if denied is not None:
        return denied

    from platform_core.support_bridge.continuity import (
        contact_for_conversation,
        prior_conversations,
    )

    try:
        conversation_ref_id = parse_conversation_ref(conversation_ref)
    except ValueError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)
    async with tenant_session(ctx) as session:
        contact = await contact_for_conversation(
            session, tenant_id=ctx.tenant_id, conversation_ref_id=conversation_ref_id
        )
        if not contact:
            return ok_response(
                {"contact_id": None, "count": 0, "items": []}, trace_id=new_trace_id()
            )
        prior = await prior_conversations(
            session,
            tenant_id=ctx.tenant_id,
            external_contact_id=contact,
            exclude_conversation_ref_id=conversation_ref_id,
            limit=limit,
        )
    return ok_response(
        {
            "contact_id": contact,
            "count": len(prior),
            "items": [
                {
                    "conversation_ref": str(p.conversation_ref_id),
                    "channel": p.channel,
                    "opened_at": p.opened_at,
                    "lease_version": p.lease_version,
                }
                for p in prior
            ],
        },
        trace_id=new_trace_id(),
    )
