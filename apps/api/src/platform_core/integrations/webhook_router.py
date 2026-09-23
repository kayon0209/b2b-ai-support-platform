"""Inbound connector webhook: verify, then persist before working on it.

    POST /v1/webhooks/connectors/{connector_id}

A provider notifies the platform that something happened on its side: a Jira
issue transitioned, an ERP order changed. The platform has to be able to
receive that at all.

How a connector resolves its tenant
-----------------------------------
A generic connector has no external-resource mapping - the only input is the
connector id in the path - and reading `connectors` to discover the tenant is
impossible before a tenant is bound, because the table is FORCE RLS'd on that
very binding. That is the bootstrap cycle migrations 0015, 0016, 0018, 0019 and
0026 each break with a narrow `SECURITY DEFINER` function;
`resolve_connector_for_webhook` is the one used here, and it returns only the
tenant, provider, inbound secret reference and status.

Security posture
----------------
- The signature **is** the authentication: there is no bearer token, which is
  why the path is in `EXEMPT_PATHS`. Verification is HMAC-SHA256 over
  `{timestamp}.{body}` with the connector's inbound secret, constant-time
  compared, inside the replay tolerance window - the one `verify_webhook`, not
  a second implementation per provider.
- A `disabled` connector's webhook is refused. Turning a connector off has to
  stop its inbound traffic, not only its outbound calls.
- Delivery ids are deduplicated by the existing `uq_inbox_delivery` index, so
  a provider retry cannot produce a second record.
- The payload is **minimized before storage**: the raw body is not kept
  (docs/security.md: no raw customer content at rest).

What happens to an accepted event, stated plainly
-------------------------------------------------
It is persisted in `inbox_events` and the worker marks it processed without
acting on it, because the existing consumer only runs the answer path for
`message_created`. That is deliberate rather than incomplete: deciding what a
provider event should change in the domain (which Case does a Jira issue
transition belong to?) needs a link that the model does not have, and
inventing one would be guessing. Persisting first is the documented rule and
it is what makes the decision reversible - the event is on record, so wiring
a consumer later does not lose anything that arrived meanwhile.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from platform_core.db import app_role_url, session_scope_with_url
from platform_core.integrations.inbound import (
    DELIVERY_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    resolve_connector,
    secret_bytes,
)
from platform_core.support_bridge import inbox
from platform_core.support_bridge.minimize import minimize_inbound_payload
from platform_core.support_bridge.webhook_security import (
    WebhookVerificationError,
    sign_payload,
    verify_webhook,
)

router = APIRouter(prefix="/v1/webhooks/connectors", tags=["integrations"])


def _error(code: str, status_code: int) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"code": code}})


@router.post("/{connector_id}")
async def connector_webhook(request: Request, connector_id: uuid.UUID) -> Response:
    """Verify a provider delivery and persist it before any work is queued."""
    body = await request.body()

    connector = await resolve_connector(connector_id)
    if connector is None:
        # Unknown connector id: 404 with no detail. Distinguishing "no such
        # connector" from "signature wrong" would let a caller enumerate ids.
        return _error("WEBHOOK_UNKNOWN_CONNECTOR", 404)

    if str(connector["status"]) != "active":
        return _error("WEBHOOK_CONNECTOR_INACTIVE", 409)

    secret = secret_bytes(connector["webhook_secret_ref"])
    if secret is None:
        # The connector exists but has no resolvable inbound secret. Accepting
        # anything here would be an unauthenticated write path, so refuse and
        # say so distinctly - this is a misconfiguration an operator fixes,
        # not a bad request.
        return _error("WEBHOOK_NOT_CONFIGURED", 503)

    try:
        verify_webhook(
            secret=secret,
            signature=request.headers.get(SIGNATURE_HEADER),
            timestamp=request.headers.get(TIMESTAMP_HEADER),
            body=body,
        )
    except WebhookVerificationError:
        return _error("WEBHOOK_SIGNATURE_INVALID", 401)

    delivery_id = request.headers.get(DELIVERY_HEADER) or ""
    if not delivery_id:
        # Without a delivery id there is no idempotency key, and provider
        # retries would each become a separate record.
        return _error("WEBHOOK_DELIVERY_ID_MISSING", 400)

    try:
        raw_payload: dict[str, Any] = json.loads(body) if body else {}
    except json.JSONDecodeError:
        return _error("WEBHOOK_PAYLOAD_MALFORMED", 400)
    if not isinstance(raw_payload, dict):
        return _error("WEBHOOK_PAYLOAD_MALFORMED", 400)

    event_type = str(raw_payload.get("event") or raw_payload.get("webhookEvent") or "unknown")

    async with session_scope_with_url(app_role_url()) as session:
        from sqlalchemy import text

        # The tenant comes from the resolver function above, never from the
        # payload: a provider-supplied tenant field is attacker-controlled.
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(connector["tenant_id"])},
        )
        result = await inbox.persist_inbox_event(
            session,
            tenant_id=connector["tenant_id"],
            delivery_id=delivery_id,
            event_type=event_type,
            raw_body=body,
            # A connector callback is not a customer channel: there is no
            # adapter to translate it, so the generic minimizer is the right
            # tool here and only here.
            minimized_payload=minimize_inbound_payload(event_type, raw_payload),
        )

    if result.duplicate:
        # Success for an already-processed delivery id: a provider retry is
        # not an error, and answering 4xx would make it retry forever.
        return JSONResponse(
            status_code=200,
            content={"status": "duplicate", "delivery_id": delivery_id},
        )

    return JSONResponse(
        status_code=202,
        content={"status": "received", "event_id": str(result.event_id)},
    )


__all__ = ["DELIVERY_HEADER", "SIGNATURE_HEADER", "TIMESTAMP_HEADER", "router", "sign_payload"]
