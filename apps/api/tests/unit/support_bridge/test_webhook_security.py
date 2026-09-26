"""Unit tests: webhook signature verification and payload minimization.

`verify_webhook` and `sign_payload` are shared by every signed inbound route —
the connector webhook and both channel adapters — so these assertions cover all
of them, not one provider's endpoint.
"""

import json
import time

import pytest

from platform_core.support_bridge.minimize import minimize_inbound_payload
from platform_core.support_bridge.webhook_security import (
    WebhookVerificationError,
    sign_payload,
    verify_webhook,
)

SECRET = b"test-webhook-secret"


def _signed_body(body: bytes, ts: str | None = None) -> tuple[str, str]:
    ts = ts or str(int(time.time()))
    return sign_payload(SECRET, ts, body), ts


def test_valid_signature_passes() -> None:
    body = b'{"event": "message_created"}'
    sig, ts = _signed_body(body)
    verify_webhook(SECRET, sig, ts, body, now=time.time())


def test_tampered_body_rejected() -> None:
    body = b'{"event": "message_created"}'
    sig, ts = _signed_body(body)
    with pytest.raises(WebhookVerificationError, match="signature"):
        verify_webhook(SECRET, sig, ts, b'{"event": "tampered"}', now=time.time())


def test_expired_timestamp_rejected() -> None:
    body = b"{}"
    old_ts = str(int(time.time()) - 3600)
    sig = sign_payload(SECRET, old_ts, body)
    with pytest.raises(WebhookVerificationError, match="replay"):
        verify_webhook(SECRET, sig, old_ts, body, now=time.time())


def test_missing_headers_rejected() -> None:
    with pytest.raises(WebhookVerificationError, match="missing"):
        verify_webhook(SECRET, None, None, b"{}")


def test_malformed_timestamp_rejected() -> None:
    sig, _ = _signed_body(b"{}")
    with pytest.raises(WebhookVerificationError, match="malformed"):
        verify_webhook(SECRET, sig, "not-a-number", b"{}")


def test_minimizer_never_copies_content() -> None:
    """The policy, stated as an assertion: the body must not survive."""
    payload = {
        "id": 42,
        "content": "SECRET CUSTOMER MESSAGE BODY",
        "message_type": "incoming",
        "conversation": {"id": 7, "inbox_id": 3, "status": "open"},
        "sender": {"type": "contact", "id": 99},
    }
    minimized = minimize_inbound_payload("message_created", payload)
    dumped = json.dumps(minimized)
    assert "SECRET CUSTOMER MESSAGE BODY" not in dumped
    assert minimized["message_id"] == "42"
    assert minimized["conversation_id"] == "7"
    assert minimized["message_type"] == "incoming"
    # Length only, so an operator can tell a long question from a short one
    # without the platform keeping either.
    assert minimized["content_length"] == len("SECRET CUSTOMER MESSAGE BODY")
