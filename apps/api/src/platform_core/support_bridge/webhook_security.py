"""Webhook signature verification (docs/api-contracts.md, docs/security.md).

Chatwoot sends X-Signature / X-Timestamp headers. We verify:
1. HMAC-SHA256 over `{timestamp}.{body}` with the connector webhook secret.
2. Timestamp within the replay tolerance window.
3. Constant-time comparison.

The secret comes from connector configuration, never from the request.
"""

import hashlib
import hmac
import time

from platform_core.config import get_settings

SIGNATURE_HEADER = "X-Chatwoot-Signature"
TIMESTAMP_HEADER = "X-Chatwoot-Timestamp"
DELIVERY_HEADER = "X-Chatwoot-Delivery"
LEGACY_SIGNATURE_HEADER = "X-Signature"
LEGACY_TIMESTAMP_HEADER = "X-Timestamp"
LEGACY_DELIVERY_HEADER = "X-Delivery-Id"


class WebhookVerificationError(Exception):
    """Fail-closed error for any signature/timestamp problem."""


def sign_payload(secret: bytes, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret, digestmod=hashlib.sha256)
    mac.update(timestamp.encode())
    mac.update(b".")
    mac.update(body)
    return mac.hexdigest()


def verify_webhook(
    secret: bytes,
    signature: str | None,
    timestamp: str | None,
    body: bytes,
    *,
    now: float | None = None,
) -> None:
    """Raise WebhookVerificationError unless everything checks out.

    Fail closed on: missing headers, malformed timestamp, expired replay
    window, or signature mismatch. Comparison is constant-time.
    """
    settings = get_settings()
    tolerance = settings.webhook_timestamp_tolerance_seconds

    if not signature or not timestamp:
        raise WebhookVerificationError("missing signature or timestamp headers")

    # Chatwoot sends "sha256=<hex>"; accept both that and bare hex.
    if signature.lower().startswith("sha256="):
        signature = signature[7:]

    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise WebhookVerificationError("malformed timestamp") from exc

    current = now if now is not None else time.time()
    if abs(current - ts) > tolerance:
        raise WebhookVerificationError("timestamp outside replay window")

    expected = sign_payload(secret, timestamp, body)
    if not hmac.compare_digest(expected, signature):
        raise WebhookVerificationError("signature mismatch")
