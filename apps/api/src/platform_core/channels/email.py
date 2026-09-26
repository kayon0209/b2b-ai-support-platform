"""Email inbound adapter (ADR 0013).

Contract
--------
A provider (or a gateway in front of one) POSTs a JSON body signed with the
connector's inbound secret, using the same scheme as every other inbound
delivery in this repository - HMAC-SHA256 over `{timestamp}.{body}`, verified by
`webhook_security.verify_webhook`. **Reused, not reimplemented**: a second
implementation of the same check is how two paths end up disagreeing about what
"authentic" means.

Expected body (provider-agnostic, and deliberately small):

    {
      "message_id":  "<Message-Id>",
      "references":  ["<root Message-Id>", ...],   # optional
      "thread_id":   "<root Message-Id>",          # optional
      "from":        "customer@example.com",
      "to":          "support@acme.com",
      "subject":     "...",
      "text":        "...",
      "attachments": [{"content_type": "image/png"}]
    }

Threading, and the one way to get it wrong
------------------------------------------
`conversation_key` decides which conversation a message belongs to, so a key
that changes between two messages of one thread splits the thread in two. The
root entry of `references` is the thread root by RFC 5322 and is therefore
preferred; `thread_id` is a provider's shortcut for the same thing. **A provider
that sets `thread_id` to the *parent* rather than the *root* will split every
thread** - the standard wins when both are present, and that is why.

Boundaries, stated rather than discovered
-----------------------------------------
- A delivery with no `text` is **not answered** (translate returns None). An
  HTML-only email is therefore dropped: extracting text from HTML is a
  content-processing decision, and guessing at it would silently feed the model
  markup. The provider is expected to supply a plain-text part.
- Attachment **content types only** are carried. No URLs, filenames or bytes:
  `docs/security.md` keeps no customer content at rest, and a Gerber or board
  drawing is customer IP.
"""

from __future__ import annotations

import json
from typing import Any

from platform_core.channels.base import (
    ChannelRequest,
    ChannelVerificationError,
    InboundMessage,
    header,
)
from platform_core.integrations.inbound import SIGNATURE_HEADER, TIMESTAMP_HEADER
from platform_core.support_bridge.webhook_security import (
    WebhookVerificationError,
    verify_webhook,
)

# Bounds on attachment metadata, mirroring `support_bridge.minimize`: the point
# is that a noisy payload cannot turn one row into a large document.
_MAX_ATTACHMENTS = 5
_MAX_TYPE_CHARS = 63


class EmailAdapter:
    """Translation and verification for an inbound email connector."""

    system = "email"

    def verify(self, *, secret: bytes, request: ChannelRequest) -> None:
        try:
            verify_webhook(
                secret=secret,
                # `header()`, not `request.headers.get(...)`: header names are
                # case-insensitive and httpx lowercases them, so a plain dict
                # lookup by the canonical name passes in a hand-built test and
                # 401s against every real client. Mutation-tested.
                signature=header(request, SIGNATURE_HEADER),
                timestamp=header(request, TIMESTAMP_HEADER),
                body=request.body,
            )
        except WebhookVerificationError as exc:
            # Re-raised as the channel-level error so the router has one thing to
            # catch. The reason is not carried outward: whether the signature or
            # the timestamp was wrong is not a caller's business.
            raise ChannelVerificationError("email signature rejected") from exc

    def challenge(self, *, secret: bytes, request: ChannelRequest) -> str | None:
        """Email has no endpoint-verification handshake."""
        return None

    def acknowledgement(self, *, request: ChannelRequest, content: str) -> tuple[bytes, str] | None:
        """A JSON provider is answered with a status code, not a body."""
        return None

    def translate(self, *, request: ChannelRequest) -> InboundMessage | None:
        try:
            payload: Any = json.loads(request.body) if request.body else {}
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None

        message_id = _text(payload.get("message_id"))
        if not message_id:
            # No stable id means no dedup key: a provider retry would become a
            # second question, so refuse rather than invent one.
            return None

        body = _text(payload.get("text"))
        if not body:
            return None

        sender = _text(payload.get("from"))
        return InboundMessage(
            conversation_key=_conversation_key(payload, message_id),
            message_id=message_id,
            contact_id=sender,
            text=body,
            attachment_types=_attachment_types(payload.get("attachments")),
        )


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _conversation_key(payload: dict[str, Any], message_id: str) -> str:
    """The thread root, or this message if it starts one.

    See the module docstring: the *root* of `references` is the thread root by
    RFC 5322, so it is preferred over `thread_id`, which some providers set to
    the parent instead.
    """
    references = payload.get("references")
    if isinstance(references, list):
        for item in references:
            candidate = _text(item)
            if candidate:
                return candidate
    thread_id = _text(payload.get("thread_id"))
    return thread_id or message_id


def _attachment_types(raw: object) -> list[str]:
    if not isinstance(raw, list) or not raw:
        return []
    types: list[str] = []
    for item in raw[:_MAX_ATTACHMENTS]:
        if not isinstance(item, dict):
            continue
        content_type = _text(item.get("content_type"))
        if content_type and content_type not in types:
            types.append(content_type[:_MAX_TYPE_CHARS])
    return types
