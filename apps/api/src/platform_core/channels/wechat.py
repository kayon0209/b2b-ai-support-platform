"""WeChat Official Account (公众号) inbound adapter (ADR 0013).

Why this is not the email adapter with different field names
-----------------------------------------------------------
WeChat's contract differs in three ways that each change the code:

1. **Its own signature scheme.** `sha1` over the *sorted* concatenation of
   `token`, `timestamp` and `nonce`, compared against the `signature` query
   parameter. Not HMAC, and not over the body - so `verify_webhook` cannot be
   reused here. (sha1 is mandated by the protocol; it signs a shared secret
   rather than hashing content, and upgrading it would break every account.)
2. **XML, not JSON.**
3. **A synchronous reply window.** The protocol expects an XML reply to the POST
   within ~5 seconds or it retries and then shows the user nothing.

On (3): this platform's agent run is queued and answers asynchronously - measured
at 8-68s on this deployment - so the adapter cannot wait for the answer. The
router answers the passive "已收到" acknowledgement and the real answer is
delivered by the outbound leg (ADR 0014). **That is a consequence of the async
model, not a defect to patch here.**

Boundaries
----------
- Only `MsgType == "text"` becomes a message. Subscribes, unsubscribes, images,
  voice and location all arrive on the same endpoint and none of them is a
  question; `translate` returns None for them, so no run is queued with nothing
  to answer. (Sending "the customer attached a photo" is a real gap, recorded in
  ADR 0013 rather than half-built here.)
- The body is parsed with entity declarations refused. The payload is
  signature-verified before it is parsed, so this is defence in depth rather
  than the only guard - but "verified" and "safe to hand to an XML parser" are
  different properties, and a DTD in a message body is never legitimate.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import xml.etree.ElementTree as ET

from platform_core.channels.base import (
    ChannelRequest,
    ChannelVerificationError,
    InboundMessage,
)

# The XML reply's own envelope. WeChat expects the same shape back.
_REPLY_TEMPLATE = (
    "<xml>"
    "<ToUserName><![CDATA[{to_user}]]></ToUserName>"
    "<FromUserName><![CDATA[{from_user}]]></FromUserName>"
    "<CreateTime>{created}</CreateTime>"
    "<MsgType><![CDATA[text]]></MsgType>"
    "<Content><![CDATA[{content}]]></Content>"
    "</xml>"
)


def _signature(token: str, timestamp: str, nonce: str) -> str:
    """WeChat's signature: sha1 of the sorted token/timestamp/nonce triple."""
    joined = "".join(sorted((token, timestamp, nonce)))
    return hashlib.sha1(joined.encode()).hexdigest()


class WeChatAdapter:
    """Translation and verification for a WeChat Official Account connector."""

    system = "wechat"

    def verify(self, *, secret: bytes, request: ChannelRequest) -> None:
        provided = request.query.get("signature") or ""
        timestamp = request.query.get("timestamp") or ""
        nonce = request.query.get("nonce") or ""
        if not provided or not timestamp or not nonce:
            raise ChannelVerificationError("wechat signature parameters missing")
        expected = _signature(secret.decode("utf-8", "replace"), timestamp, nonce)
        # Constant-time: a timing difference here would let a caller recover the
        # signature one character at a time.
        if not hmac.compare_digest(expected, provided):
            raise ChannelVerificationError("wechat signature rejected")

    def challenge(self, *, secret: bytes, request: ChannelRequest) -> str | None:
        """Echo `echostr` back, which is how WeChat proves the URL is ours.

        Reaching here means `verify` already accepted the signature: answering
        the challenge without it would let anyone confirm which server URLs
        belong to this deployment.
        """
        if request.method.upper() != "GET":
            return None
        echostr = request.query.get("echostr")
        return echostr if echostr else None

    def translate(self, *, request: ChannelRequest) -> InboundMessage | None:
        root = _parse(request.body)
        if root is None:
            return None

        if _field(root, "MsgType").lower() != "text":
            return None

        text = _field(root, "Content")
        sender = _field(root, "FromUserName")
        message_id = _field(root, "MsgId")
        if not text or not sender or not message_id:
            return None

        return InboundMessage(
            # One conversation per customer: the official account protocol has
            # no thread concept, so the openid *is* the conversation key. Using
            # the message id instead would make every message its own
            # conversation and lose all context.
            conversation_key=sender,
            message_id=message_id,
            contact_id=sender,
            text=text,
        )

    def acknowledgement(self, *, request: ChannelRequest, content: str) -> tuple[bytes, str] | None:
        """The XML reply, built from the inbound envelope.

        The addresses swap: what arrived as `ToUserName` (our official account)
        goes back as `FromUserName`, and the customer's openid becomes
        `ToUserName`. Getting that backwards fails silently - WeChat accepts the
        reply and delivers it to nobody.
        """
        root = _parse(request.body)
        if root is None:
            return None
        customer = _field(root, "FromUserName")
        account = _field(root, "ToUserName")
        if not customer or not account:
            return None
        # `]]>` inside CDATA would close the section early and produce XML that
        # is well-formed enough to send and wrong enough to be rejected.
        safe = content.replace("]]>", "]]&gt;")
        body = _REPLY_TEMPLATE.format(
            to_user=customer,
            from_user=account,
            created=int(time.time()),
            content=safe,
        ).encode()
        return body, "application/xml"


def _parse(body: bytes) -> ET.Element | None:
    if not body.strip():
        return None
    # A DTD in a chat message is never legitimate, and entity expansion is the
    # classic way to turn a small body into a large one.
    lowered = body.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        return None
    try:
        return ET.fromstring(body)
    except ET.ParseError:
        return None


def _field(root: ET.Element, name: str) -> str:
    """Read one top-level field, trimmed; empty string when absent.

    Only direct children are considered. `findtext(".//" + name)` would also
    match a nested element, which lets a payload choose which `Content` the
    adapter reads.
    """
    for child in root:
        if child.tag == name and child.text:
            return child.text.strip()
    return ""
