"""Unit tests: webhook signature verification and payload minimization."""

import json
import time

import pytest

from platform_core.support_bridge.minimize import (
    build_envelope,
    minimize_chatwoot_payload,
    payload_hash,
)
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
    payload = {
        "id": 42,
        "content": "SECRET CUSTOMER MESSAGE BODY",
        "message_type": "incoming",
        "conversation": {"id": 7, "inbox_id": 3, "status": "open"},
        "account": {"id": 1},
        "sender": {"type": "contact", "id": 99},
    }
    minimized = minimize_chatwoot_payload("message_created", payload)
    dumped = json.dumps(minimized)
    assert "SECRET CUSTOMER MESSAGE BODY" not in dumped
    assert minimized["message_id"] == "42"
    assert minimized["conversation_id"] == "7"
    assert minimized["chatwoot_account_id"] == "1"
    assert minimized["content_length"] == len("SECRET CUSTOMER MESSAGE BODY")


def test_envelope_shape() -> None:
    envelope = build_envelope(
        event_type="message_created",
        tenant_id="01900000-0000-7000-8000-000000000001",
        delivery_id="d1",
        minimized={"message_id": "42", "conversation_id": "7"},
    )
    assert envelope["source"] == "chatwoot"
    assert envelope["event_version"] == 1
    assert envelope["resource"] == {"type": "message", "external_id": "42"}
    assert envelope["event_type"] == "message_created"
    assert payload_hash(b"x") == payload_hash(b"x")
