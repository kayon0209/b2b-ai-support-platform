"""Payload minimization policy (docs/security.md).

Raw customer content never persists unminimized. An `InboxEvent` keeps the
identifiers, the event type and a content hash; the body itself is read back
from the platform's own `conversation_turns` when the runtime needs it.

`minimize_inbound_payload` is the fallback for a producer that has a raw
provider payload and no translator of its own. A channel adapter does **not**
use it — it translates into `InboundMessage` and hands `persist_inbox_event` an
explicit minimized dict, because running a generic extractor over a payload it
already understands is how a silently empty row gets stored.

The module was named after Chatwoot and carried a Chatwoot-shaped extractor.
The policy outlived the integration (ADR 0012); the vocabulary did not.
"""

import hashlib
from typing import Any

# Bounds on attachment metadata. Both are about keeping a hostile or merely
# noisy payload from turning one event row into a large document: the row is
# metadata for routing, not a store of what the customer sent.
_MAX_ATTACHMENTS = 5
_MAX_TYPE_CHARS = 63


def payload_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def minimize_inbound_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Extract only safe routing fields from a raw inbound payload.

    Payloads vary by provider; message-shaped ones carry content under
    `content` or in `conversation.messages`, which is NEVER copied — only the
    identifiers and metadata the worker needs to resolve a conversation.
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
        # Attachments (feature list 1.3): this trade runs on board photos,
        # Gerber archives and BOM spreadsheets, and a platform that only knows
        # about text cannot tell that evidence was already supplied.
        #
        # Only the content TYPES are kept - no URLs, no filenames, no bytes.
        # Two reasons, both load-bearing: the minimisation policy stores no
        # customer content at rest, and a Gerber or board drawing is customer
        # IP (the report's own red line). "The customer attached two images"
        # is what the run actually needs; anything more is risk.
        attachments = payload.get("attachments")
        if isinstance(attachments, list) and attachments:
            types: list[str] = []
            for item in attachments[:_MAX_ATTACHMENTS]:
                if not isinstance(item, dict):
                    continue
                file_type = item.get("file_type") or item.get("content_type")
                if isinstance(file_type, str) and file_type not in types:
                    types.append(file_type[:_MAX_TYPE_CHARS])
            if types:
                extracted["attachment_types"] = types

    conversation = payload.get("conversation")
    if isinstance(conversation, dict):
        extracted["conversation_id"] = str(conversation.get("id"))
        extracted["inbox_id"] = str(conversation.get("inbox_id"))
        extracted["status"] = conversation.get("status")
    elif "conversation_id" in payload:
        extracted["conversation_id"] = str(payload["conversation_id"])

    # Feature 2.5: the account a visitor *proved* ownership of (set by
    # `POST /v1/support/verify`, re-issued onto the visitor token). It is an
    # opaque authorisation label the worker's ownership gate compares against
    # the receipt's own account - not customer content, so it survives
    # minimisation where the message body does not. Empty string is meaningful
    # here ("anonymous visitor"): do not drop it, or the gate would see absence
    # and treat an anonymous run as an un-gated operator run.
    verified = payload.get("verified_account")
    if isinstance(verified, str):
        extracted["verified_account"] = verified

    sender = payload.get("sender")
    if isinstance(sender, dict):
        extracted["sender_type"] = sender.get("type")
        extracted["sender_id"] = str(sender.get("id")) if sender.get("id") else None

    # Contact id: the durable-facts key (plan 2.5) is per-contact, and the
    # contact id is the stable handle for "this customer" across messages.
    contact = payload.get("contact")
    if isinstance(contact, dict) and contact.get("id") is not None:
        extracted["contact_id"] = str(contact["id"])

    extracted["event"] = event
    return extracted
