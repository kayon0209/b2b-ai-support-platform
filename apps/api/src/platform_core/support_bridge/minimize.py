"""Payload minimization and canonical event envelope construction.

Per docs/security.md logging policy: raw customer message content never
persists unminimized. We keep IDs, event type, timestamps and a content
hash; the message body itself stays in Chatwoot and is fetched later via
its API when the runtime needs it.
"""

import hashlib
import time
import uuid
from typing import Any

EVENT_VERSION = 1


def payload_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def minimize_chatwoot_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Extract only safe routing fields from a Chatwoot webhook payload.

    Chatwoot payloads vary by event; message events carry content under
    `content` or in `conversation.messages`, which we NEVER copy — only
    identifiers and metadata needed for tenant/conversation resolution.
    """
    extracted: dict[str, Any] = {}
    event = payload.get("event") or event_type

    # Message-shaped payloads
    if "id" in payload and ("content" in payload or "message_type" in payload):
        extracted["message_id"] = str(payload["id"])
        extracted["message_type"] = payload.get("message_type")
        # content is deliberately excluded; store length for diagnostics only
        content = payload.get("content")
        extracted["content_length"] = len(content) if isinstance(content, str) else None

    conversation = payload.get("conversation")
    if isinstance(conversation, dict):
        extracted["conversation_id"] = str(conversation.get("id"))
        extracted["inbox_id"] = str(conversation.get("inbox_id"))
        extracted["status"] = conversation.get("status")
    elif "conversation_id" in payload:
        extracted["conversation_id"] = str(payload["conversation_id"])

    account = payload.get("account") or payload.get("current_account")
    if isinstance(account, dict) and account.get("id") is not None:
        extracted["chatwoot_account_id"] = str(account["id"])

    sender = payload.get("sender")
    if isinstance(sender, dict):
        extracted["sender_type"] = sender.get("type")
        extracted["sender_id"] = str(sender.get("id")) if sender.get("id") else None

    # Contact id: the durable-facts key (plan 2.5) is per-contact, and the
    # contact id is the stable handle for "this customer" across messages.
    contact = payload.get("contact")
    if isinstance(contact, dict) and contact.get("id") is not None:
        extracted["contact_id"] = str(contact["id"])

    extracted["chatwoot_event"] = event
    return extracted


def build_envelope(
    *,
    event_type: str,
    tenant_id: str,
    delivery_id: str,
    minimized: dict[str, Any],
    trace_id: str | None = None,
    occurred_at: int | None = None,
) -> dict[str, Any]:
    """Canonical event envelope per docs/api-contracts.md."""
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "event_version": EVENT_VERSION,
        "tenant_id": tenant_id,
        "source": "chatwoot",
        "occurred_at": occurred_at or int(time.time()),
        "resource": {
            "type": "message" if "message" in event_type else "conversation",
            "external_id": minimized.get("message_id") or minimized.get("conversation_id", ""),
        },
        "data": minimized,
        "trace_id": trace_id or str(uuid.uuid4()),
    }
