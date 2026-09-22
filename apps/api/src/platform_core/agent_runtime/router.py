"""Agent run API (docs/api-contracts.md agent run API).

POST /v1/conversations/{conversation_ref}/agent-runs

Queues an agent run for a conversation and returns immediately.

Contract notes:
- docs/api-contracts.md requires the webhook path to respond within 300 ms
  and to never call an LLM synchronously. The same rule governs this
  endpoint: it persists the work and returns `status: "queued"`. Generation
  happens in the worker, where the pre-send lease re-check lives.
- The endpoint therefore enqueues through the same transactional inbox the
  Chatwoot webhook uses. That reuses the proven claim/ack/idempotency path
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
from platform_core.agent_runtime.models import AgentRun
from platform_core.api import (
    IDEMPOTENCY_KEY_REQUIRED,
    NOT_FOUND,
    VALIDATION_FAILED,
    error_response,
    get_context,
    new_trace_id,
    ok_response,
    parse_uuid,
    require_idempotency_key,
    require_policy,
    tenant_session,
)
from platform_core.support_bridge.conversation_ref import (
    conversation_ref_for as conversation_ref_for,
)
from platform_policy import Action

router = APIRouter(prefix="/v1/conversations", tags=["agent-runtime"])

VALID_MODES = frozenset({"customer_reply", "internal_draft"})


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
        external_ref = parse_uuid(conversation_ref, field="conversation_ref")
    except ValueError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)
    # Derived, not the path value verbatim: the worker answers under the
    # derived id, so a run queued under the raw id is filed in a different
    # conversation from the one that produces the answer - which is exactly
    # why a listing of "runs for this conversation" used to show only the
    # queued placeholder and never the run that answered.
    conversation_ref_id = conversation_ref_for(ctx.tenant_id, str(external_ref))

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
                # `str(external_ref)`, not the raw path string: the ref above was
                # derived from the parsed form, and hashing a different spelling
                # of the same uuid ("{...}", an urn prefix, upper case) produces a
                # different conversation - which is the double-derivation defect
                # this module's docstring warns about, one step earlier.
                external_ref=str(external_ref),
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
        external_ref = parse_uuid(conversation_ref, field="conversation_ref")
    except ValueError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)
    # Same derivation as the create path and as the worker, so this listing
    # can actually see the runs that answered. Rows queued before the two
    # agreed were stored under the raw id and are no longer listed here;
    # they were unreachable placeholders anyway.
    conversation_ref_id = conversation_ref_for(ctx.tenant_id, str(external_ref))

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

    The same ref derivation as the two endpoints above, for the reason the
    create path documents - a replay looked up under the raw id would be filed
    against a different conversation from the one the runs answered in.
    """
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)
    denied = require_policy(ctx, Action.CASE_READ)
    if denied is not None:
        return denied

    try:
        external_ref = parse_uuid(conversation_ref, field="conversation_ref")
    except ValueError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)
    conversation_ref_id = conversation_ref_for(ctx.tenant_id, str(external_ref))

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
