"""Inbound channel webhooks: email and WeChat (ADR 0013).

    POST /v1/webhooks/channels/{connector_id}
    GET  /v1/webhooks/channels/{connector_id}    # WeChat server-URL verification

A channel **is** a `connectors` row (`provider = "email" | "wechat"`). That is
what gives this route a tenant without reading one from a payload, a signing
secret without a new column, and the `SECURITY DEFINER` resolver that breaks the
RLS bootstrap cycle - see `integrations/inbound.py` for why a plain SELECT
cannot work here.

What this route does, in order
------------------------------
1. Resolve the connector (tenant, provider, secret reference, status).
2. Pick the adapter by provider and verify the delivery with the channel's own
   scheme. **Verification is the authentication** - there is no bearer token,
   which is why this path is exempt.
3. Translate into an `InboundMessage`.
4. Persist the customer turn, then the `InboxEvent`.

Step 4 is why the adapter interface is so small: an email or WeChat message ends
up indistinguishable from a question typed into `/support`, so the worker's
consumer, the ownership gate and the delivery path need no changes at all.

Response contract
-----------------
- The channel's own acknowledgement, if it has one (WeChat needs XML inside five
  seconds - `channels/wechat.py` says why the real answer cannot be in it).
- `202` when accepted, `200` for a duplicate delivery id (a provider retry is not
  an error, and answering 4xx would make it retry forever).
- `202`/`200` and *not* an error when a delivery is not a question.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

from platform_core.agent_runtime import chat_service
from platform_core.channels.base import (
    ChannelAdapter,
    ChannelRequest,
    ChannelVerificationError,
)
from platform_core.channels.email import EmailAdapter
from platform_core.channels.wechat import WeChatAdapter
from platform_core.db import app_role_url, session_scope_with_url
from platform_core.integrations.inbound import resolve_connector, secret_bytes
from platform_core.support_bridge import inbox
from platform_core.support_bridge.continuity import link_conversation
from platform_core.support_bridge.conversation_ref import conversation_ref_for

router = APIRouter(prefix="/v1/webhooks/channels", tags=["channels"])

# Keyed by `connectors.provider`: the row selects its own adapter, so adding a
# channel is a row plus an adapter, never a branch in this file.
ADAPTERS: dict[str, ChannelAdapter] = {
    EmailAdapter.system: EmailAdapter(),
    WeChatAdapter.system: WeChatAdapter(),
}

# Sent inside WeChat's reply window. The real answer is delivered later by the
# outbound leg (ADR 0014); this only tells the customer the message arrived, so
# it must not promise anything the run has not decided yet.
ACK_TEXT = "已收到，正在为您查询…"


def _error(code: str, status_code: int) -> Response:
    return Response(
        content=json.dumps({"error": {"code": code, "retryable": False}}),
        status_code=status_code,
        media_type="application/json",
    )


def _channel_request(request: Request, body: bytes) -> ChannelRequest:
    return ChannelRequest(
        method=request.method,
        headers=dict(request.headers),
        query=dict(request.query_params),
        body=body,
    )


def _respond(
    adapter: ChannelAdapter, delivery: ChannelRequest, *, status_code: int, duplicate: bool = False
) -> Response:
    """Answer in the channel's own shape when it has one, JSON otherwise."""
    own = adapter.acknowledgement(request=delivery, content=ACK_TEXT)
    if own is not None:
        body, media_type = own
        # WeChat retries a delivery it thinks failed; a duplicate still needs a
        # valid reply or it retries again.
        return Response(content=body, media_type=media_type)
    payload = {"status": "duplicate" if duplicate else "received"}
    return Response(
        content=json.dumps(payload),
        # 200 for a duplicate, matching the connector webhook: a provider retry
        # is not an error, and answering 4xx would make it retry forever. 202
        # would also be wrong in the other direction - it claims new work.
        status_code=200 if duplicate else status_code,
        media_type="application/json",
    )


@router.api_route("/{connector_id}", methods=["GET", "POST"])
async def channel_webhook(request: Request, connector_id: uuid.UUID) -> Response:
    """Verify a channel delivery, persist the turn, then persist the event."""
    body = await request.body()

    connector = await resolve_connector(connector_id)
    if connector is None:
        # Unknown connector id: 404 with no detail. Distinguishing "no such
        # connector" from "signature wrong" would let a caller enumerate ids.
        return _error("WEBHOOK_UNKNOWN_CONNECTOR", 404)

    if str(connector["status"]) != "active":
        # Turning a channel off has to stop its inbound traffic, not only its
        # outbound calls.
        return _error("WEBHOOK_CONNECTOR_INACTIVE", 409)

    adapter = ADAPTERS.get(str(connector["provider"]))
    if adapter is None:
        # A connector that is not a customer channel belongs to the generic
        # connector webhook, which persists without answering.
        return _error("WEBHOOK_NOT_A_CHANNEL", 404)

    secret = secret_bytes(connector["webhook_secret_ref"])
    if secret is None:
        # Accepting anything here would be an unauthenticated write path. This is
        # a misconfiguration an operator fixes, not a bad request.
        return _error("WEBHOOK_NOT_CONFIGURED", 503)

    delivery = _channel_request(request, body)
    try:
        adapter.verify(secret=secret, request=delivery)
    except ChannelVerificationError:
        return _error("WEBHOOK_SIGNATURE_INVALID", 401)

    # WeChat proves ownership of the server URL with a signed GET. Reaching here
    # means the signature already passed, so an unauthenticated caller cannot
    # use this to discover which server URLs belong to this deployment.
    challenge = adapter.challenge(secret=secret, request=delivery)
    if challenge is not None:
        return Response(content=challenge, media_type="text/plain")

    message = adapter.translate(request=delivery)
    if message is None:
        # Not a question - a subscribe, an image, an HTML-only email. Acknowledge
        # so the provider stops retrying, but do not queue a run with nothing in
        # it to answer.
        return _respond(adapter, delivery, status_code=200)

    tenant_id = connector["tenant_id"]
    ref_id = conversation_ref_for(tenant_id, message.conversation_key)

    async with session_scope_with_url(app_role_url()) as session:
        # Bind the tenant for the writes: from here on the session is the
        # non-bypass app role and RLS holds on both INSERTs, exactly as on every
        # interactive request path.
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        turn, _duplicate_turn = await chat_service.append_customer_turn(
            session, tenant_id=tenant_id, ref_id=ref_id, text=message.text
        )

        # Who this conversation belongs to, and which of the channel's threads
        # it is. Both are needed later: the first is continuity (feature 1.5),
        # the second is what lets an outbound reply join this thread instead of
        # starting a new one. Guarded because a channel that cannot name the
        # contact has nothing to link, and inventing one would attribute the
        # conversation to the wrong person.
        if message.contact_id:
            await link_conversation(
                session,
                tenant_id=tenant_id,
                conversation_ref_id=ref_id,
                external_contact_id=message.contact_id,
                channel=adapter.system,
                external_conversation_key=message.conversation_key,
            )

        minimized: dict[str, Any] = {
            "conversation_id": message.conversation_key,
            # The turn id, not the channel's message id: the consumer resolves
            # the body from it through the same path `/support` uses, so no
            # customer content is stored on this row.
            "message_id": str(turn.id),
            "message_type": "incoming",
            "contact_id": message.contact_id,
            # Which channel to answer on (ADR 0014). Without it the orchestrator
            # cannot tell an email from a WeChat message, and `_dispatch` would
            # treat the answer as delivered by the platform surface - which is
            # what made channel answers persist and go nowhere.
            "channel_system": adapter.system,
            # EMPTY STRING, and this is load-bearing. The ownership gate is
            # three-state: None means "operator run, no gate", "" means
            # "anonymous, refuse before any connector call". A channel sender
            # has proved ownership of nothing, so leaving this unset would make
            # every email or WeChat message an un-gated run able to read any
            # order the tenant can. Omitting it is not a default; it is a hole.
            # Mutation-tested: removing this line fails
            # `test_a_channel_sender_is_stored_as_anonymous`.
            "verified_account": "",
        }
        if message.attachment_types:
            minimized["attachment_types"] = message.attachment_types

        result = await inbox.persist_inbox_event(
            session,
            tenant_id=tenant_id,
            # The channel's own message id, so a provider retry collapses onto
            # this row instead of becoming a second question.
            delivery_id=message.message_id,
            event_type="message_created",
            raw_body=body,
            minimized_payload=minimized,
        )

    return _respond(adapter, delivery, status_code=202, duplicate=result.duplicate)
