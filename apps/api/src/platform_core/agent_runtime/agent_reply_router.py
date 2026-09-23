"""Agent reply API: let a human answer.

    POST /v1/conversations/{conversation_ref}/replies

`CASE_UPDATE` and an `Idempotency-Key`, like every other write command. The
reply is the most consequential write in the product - it is the one that
reaches a customer's inbox - so it carries the strictest requirements rather
than the loosest.

`{conversation_ref}` is the platform's own conversation id, used verbatim
like every other segment under this prefix. It used to be read as an external
id and derived again, which filed the reply under a conversation the customer
could not address: the reply was delivered, the audit event was written, the
response echoed the id back, and the customer's own timeline never showed it.
No caller exercised that path yet, so it never reached anyone - see
`support_bridge.conversation_ref`.

**The author is the authenticated actor, not a field in the body.** `agent_ref`
is derived from the resolved tenant context, because a client-supplied author is
an attribution the platform cannot verify: anyone able to send a reply could
sign it with a colleague's name, and the audit trail would record the lie as
fact. The cost is that `agent_profiles.user_ref` has to be the same string as
the actor id for the directory and the reply to line up; that linkage is stated
here because it is currently an expectation rather than a foreign key, and a
deployment that names agents by email will find the two do not join.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from platform_core.agent_runtime.agent_reply import (
    MAX_REPLY_CHARS,
    AgentReplyError,
    send_agent_reply,
)
from platform_core.api import (
    AUTH_UNRESOLVED,
    VALIDATION_FAILED,
    error_response,
    get_context,
    require_policy,
    require_write_idempotency,
    tenant_session,
)
from platform_core.audit import service as audit_service
from platform_core.support_bridge.conversation_ref import parse_conversation_ref
from platform_policy import Action

router = APIRouter(prefix="/v1/conversations", tags=["conversations"])


class AgentReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_REPLY_CHARS)
    # How the text was composed. Client-supplied and deliberately so: only the
    # client knows whether the agent typed it, inserted a template, or sent the
    # model's suggestion. It is telemetry, not authorization - it gates nothing.
    origin: str = Field(default="", max_length=31)
    canned_reply_id: uuid.UUID | None = None


@router.post("/{conversation_ref}/replies")
async def post_agent_reply(request: Request, conversation_ref: str, body: AgentReplyIn) -> Any:
    """Send a human's reply to the customer, on the channel they wrote in on.

    The lease is transferred to the caller as part of this, so an AI run
    mid-generation on the same conversation cannot also send. See
    `agent_reply.send_agent_reply`.
    """
    ctx = get_context(request)
    if ctx is None:
        return error_response(AUTH_UNRESOLVED, "tenant context not resolved", status_code=401)
    denied = require_policy(ctx, Action.CASE_UPDATE)
    if denied is not None:
        return denied
    missing_idem = require_write_idempotency(request, Action.CASE_UPDATE)
    if missing_idem is not None:
        return missing_idem

    # Server-derived attribution - see the module docstring. An actor-less
    # context is a service token, and a service token has no business signing a
    # customer-visible message as if a person wrote it.
    if ctx.actor_id is None:
        return error_response(
            VALIDATION_FAILED,
            "a reply must be sent by an identified actor",
            status_code=400,
        )
    agent_ref = str(ctx.actor_id)

    try:
        ref_id = parse_conversation_ref(conversation_ref)
    except ValueError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)

    try:
        async with tenant_session(ctx) as session:
            result = await send_agent_reply(
                session,
                tenant_id=ctx.tenant_id,
                conversation_ref_id=ref_id,
                text=body.text,
                agent_ref=agent_ref,
                trace_id=getattr(request.state, "trace_id", None),
                origin=body.origin,
                canned_reply_id=body.canned_reply_id,
            )
            await audit_service.record(
                session,
                ctx=ctx,
                action="conversation.agent_replied",
                resource_type="conversation",
                resource_id=ref_id,
                metadata={
                    "turn_id": str(result.turn_id),
                    "delivery": result.delivery,
                    "channel": result.channel or "",
                    "length": len(body.text),
                    "origin": body.origin or "unknown",
                },
            )
    except AgentReplyError as exc:
        return error_response(VALIDATION_FAILED, str(exc), status_code=400)

    return {
        # The canonical form, not the path string: a ref is a UUID and a
        # client that sent an upper-case or braced spelling should get back
        # the one value every other endpoint will accept.
        "conversation_ref": str(ref_id),
        "turn_id": str(result.turn_id),
        "lease_version": result.lease_version,
        "delivery": result.delivery,
        "channel": result.channel,
        "queued_event_id": str(result.event_id) if result.event_id else None,
    }


__all__ = ["router"]
