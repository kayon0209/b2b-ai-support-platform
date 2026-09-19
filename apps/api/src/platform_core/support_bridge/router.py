"""Signed Chatwoot webhook endpoint (tickets 5 & 6).

Contract (docs/api-contracts.md):
1. Verify signature and timestamp (fail closed).
2. Resolve connector and tenant from trusted configuration.
3. Store delivery and payload hash (minimized, never raw content).
4. Return success for already-processed delivery IDs.
5. Persist InboxEvent before enqueueing work.
6. Respond within 300 ms; never call an LLM synchronously.
"""

import json
import uuid
from typing import Any

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

from platform_core.config import get_settings
from platform_core.db import app_role_url, session_scope_with_url
from platform_core.support_bridge import inbox, mapping
from platform_core.support_bridge.webhook_security import (
    DELIVERY_HEADER,
    LEGACY_DELIVERY_HEADER,
    LEGACY_SIGNATURE_HEADER,
    LEGACY_TIMESTAMP_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    WebhookVerificationError,
    verify_webhook,
)


def _header(request: Request, *names: str) -> str | None:
    for name in names:
        value = request.headers.get(name)
        if value:
            return value
    return None


router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


def _error_body(code: str) -> dict[str, Any]:
    return {"error": {"code": code, "retryable": False}}


@router.post("/chatwoot")
async def chatwoot_webhook(request: Request) -> Response:
    body = await request.body()
    settings = get_settings()

    secret: bytes | None = None
    if settings.chatwoot_webhook_secret is not None:
        secret = settings.chatwoot_webhook_secret.get_secret_value().encode()
    if secret is None:
        # Fail closed: an unconfigured connector cannot accept deliveries.
        return Response(
            content=json.dumps(_error_body("WEBHOOK_NOT_CONFIGURED")),
            status_code=503,
            media_type="application/json",
        )

    try:
        verify_webhook(
            secret=secret,
            signature=_header(request, SIGNATURE_HEADER, LEGACY_SIGNATURE_HEADER),
            timestamp=_header(request, TIMESTAMP_HEADER, LEGACY_TIMESTAMP_HEADER),
            body=body,
        )
    except WebhookVerificationError:
        return Response(
            content=json.dumps(_error_body("WEBHOOK_SIGNATURE_INVALID")),
            status_code=401,
            media_type="application/json",
        )

    delivery_id = _header(request, DELIVERY_HEADER, LEGACY_DELIVERY_HEADER) or ""
    if not delivery_id:
        return Response(
            content=json.dumps(_error_body("WEBHOOK_DELIVERY_ID_MISSING")),
            status_code=400,
            media_type="application/json",
        )

    try:
        raw_payload: dict[str, Any] = json.loads(body) if body else {}
    except json.JSONDecodeError:
        return Response(
            content=json.dumps(_error_body("WEBHOOK_PAYLOAD_MALFORMED")),
            status_code=400,
            media_type="application/json",
        )

    event_type = str(raw_payload.get("event") or "unknown")
    account = raw_payload.get("account") or raw_payload.get("current_account") or {}
    chatwoot_account_id = str(account.get("id") or "")

    async with session_scope_with_url(app_role_url()) as session:
        # Tenant from trusted mapping, never from the payload's tenant field.
        # Resolved through the SECURITY DEFINER function (migration 0032):
        # the mapping row is FORCE-RLS'd, so an unbound app-role SELECT finds
        # nothing - the same bootstrap chicken-and-egg 0016 and 0026 solved.
        # The account id arrives from a signature-verified payload, so the
        # definer read is not an enumeration surface.
        tenant_id = (
            await session.execute(
                text("SELECT resolve_chatwoot_tenant(:account_id)"),
                {"account_id": chatwoot_account_id},
            )
        ).scalar_one_or_none()
        if tenant_id is None:
            return Response(
                content=json.dumps(_error_body("WEBHOOK_TENANT_UNRESOLVED")),
                status_code=202,  # accepted but not actionable; Chatwoot stops retrying
                media_type="application/json",
            )

        # Bind the tenant for the writes: from here on the session is the
        # non-bypass app role and RLS holds on the INSERT, as on every
        # interactive request path.
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )

        result = await inbox.persist_inbox_event(
            session,
            tenant_id=tenant_id,
            delivery_id=delivery_id,
            event_type=event_type,
            raw_body=body,
            raw_payload=raw_payload,
        )

        if not result.duplicate:
            conv_id = result.event_id  # keep type-checkers honest; ref resolved by worker
            del conv_id
            # Register/update the conversation mapping if present (ticket 4)
            conv = raw_payload.get("conversation")
            if isinstance(conv, dict) and conv.get("id") is not None:
                await mapping.upsert_conversation_mapping(
                    session,
                    tenant_id=tenant_id,
                    chatwoot_conversation_id=str(conv["id"]),
                    source_version=str(conv.get("display_id") or ""),
                )

    if result.duplicate:
        # Rule 4: success for already-processed delivery IDs.
        return Response(
            content=json.dumps({"status": "duplicate", "delivery_id": delivery_id}),
            status_code=200,
            media_type="application/json",
        )

    return Response(
        content=json.dumps(
            {"status": "received", "event_id": str(result.event_id), "trace_id": str(uuid.uuid4())}
        ),
        status_code=202,
        media_type="application/json",
    )
