"""Inbound connector webhook: verify, then persist before working on it.

    POST /v1/webhooks/connectors/{connector_id}

The last uncovered Phase 3 epic, "Webhook verification and replay protection":
only Chatwoot had a signed inbound endpoint, so no other provider could notify
the platform at all.

How this differs from the Chatwoot webhook
------------------------------------------
Chatwoot resolves the tenant through `external_resource_refs`, a mapping table
the platform maintains. A generic connector has no such mapping - the only
input is the connector id in the path - and reading `connectors` to discover
the tenant is impossible before a tenant is bound, because the table is FORCE
RLS'd on that very binding. That is the bootstrap cycle migrations 0015, 0016,
0018, 0019 and 0026 each break with a narrow `SECURITY DEFINER` function;
`resolve_connector_for_webhook` is the one used here, and it returns only the
tenant, provider, inbound secret reference and status.

Security posture
----------------
- The signature **is** the authentication: there is no bearer token, which is
  why the path is in `EXEMPT_PATHS`. Verification is HMAC-SHA256 over
  `{timestamp}.{body}` with the connector's inbound secret, constant-time
  compared, inside the replay tolerance window - the same `verify_webhook`
  the Chatwoot path uses, not a second implementation.
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

from platform_core.db import session_scope
from platform_core.integrations.credentials import resolve_credentials
from platform_core.support_bridge import inbox
from platform_core.support_bridge.webhook_security import (
    WebhookVerificationError,
    sign_payload,
    verify_webhook,
)

router = APIRouter(prefix="/v1/webhooks/connectors", tags=["integrations"])

# Generic provider headers. A provider that uses different names needs its own
# adapter-level translation, not a new verification implementation.
SIGNATURE_HEADER = "X-Webhook-Signature"
TIMESTAMP_HEADER = "X-Webhook-Timestamp"
DELIVERY_HEADER = "X-Webhook-Delivery"

# The credential key the inbound secret is read from. `resolve_credentials`
# maps a bare `env://VAR` value to `{"api_token": ...}` and a JSON object to
# its own keys, so a connector may name it either way.
SECRET_KEYS = ("webhook_secret", "api_token")


def _error(code: str, status_code: int) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"code": code}})


async def _resolve_connector(connector_id: uuid.UUID) -> dict[str, Any] | None:
    """Resolve tenant + inbound secret reference for a connector id.

    Runs through the SECURITY DEFINER function rather than a plain SELECT:
    an unbound app-role read of `connectors` returns zero rows, which would
    make every webhook look like an unknown connector.
    """
    from sqlalchemy import text

    async with session_scope() as session:
        row = (
            await session.execute(
                text(
                    "SELECT tenant_id, provider, webhook_secret_ref, status "
                    "FROM resolve_connector_for_webhook(CAST(:cid AS uuid))"
                ),
                {"cid": str(connector_id)},
            )
        ).first()
    if row is None:
        return None
    return {
        "tenant_id": row[0],
        "provider": row[1],
        "webhook_secret_ref": row[2],
        "status": row[3],
    }


def _secret_bytes(credential_ref: str | None) -> bytes | None:
    """Read the inbound signing secret, or None when it cannot be resolved.

    None is a refusal, not an empty secret: signing with `b""` would make a
    forged request verifiable by anyone who knows the algorithm.
    """
    if not credential_ref:
        return None
    credentials = resolve_credentials(credential_ref)
    for key in SECRET_KEYS:
        value = credentials.get(key)
        if value and value.strip():
            return value.encode()
    return None


@router.post("/{connector_id}")
async def connector_webhook(request: Request, connector_id: uuid.UUID) -> Response:
    """Verify a provider delivery and persist it before any work is queued."""
    body = await request.body()

    connector = await _resolve_connector(connector_id)
    if connector is None:
        # Unknown connector id: 404 with no detail. Distinguishing "no such
        # connector" from "signature wrong" would let a caller enumerate ids.
        return _error("WEBHOOK_UNKNOWN_CONNECTOR", 404)

    if str(connector["status"]) != "active":
        return _error("WEBHOOK_CONNECTOR_INACTIVE", 409)

    secret = _secret_bytes(connector["webhook_secret_ref"])
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

    async with session_scope() as session:
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
            raw_payload=raw_payload,
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
