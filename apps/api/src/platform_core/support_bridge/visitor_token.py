"""Per-conversation tokens for the visitor chat surface.

ADR 0010 decided the platform would not grow a customer identity scheme, on the
grounds that Chatwoot owns the customer surface. ADR 0011 revisits that: a chat
window a customer can actually open needs a credential a customer can hold, and
the alternative -- leaving the operator bearer token as the only way in -- is
what made `/chat` unusable for anyone but an operator.

This is the narrowest credential that works. A token names exactly one
(tenant, conversation) pair, is signed with the deployment secret, and expires.

**It carries no role, and that is the point.** A visitor has no membership, so
every policy gate in the service would deny them; rather than invent a role for
them, the authorization here is the *binding*. The token states which
conversation the bearer may read and append to, and the handler compares the
request against that instead of trusting a path value. A visitor therefore
cannot name another conversation, another tenant, or any operator endpoint --
there is nothing in the token that could express one.

Format: ``vs_<payload>.<signature>``, base64url, unpadded. The signature covers
the encoded payload, so there is no JSON canonicalisation to get wrong. The
payload is compact JSON, which keeps the token short enough to put in a URL if a
signed link is ever wanted.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass

PREFIX = "vs_"

# Long enough that a customer can leave a tab open through a support
# conversation; short enough that a leaked token is not a permanent key. The
# visitor page re-issues silently rather than making the customer notice.
DEFAULT_TTL_SECONDS = 12 * 60 * 60


class VisitorTokenError(Exception):
    """Absent, malformed, forged or expired. Every case maps to 401."""


@dataclass(frozen=True)
class VisitorClaim:
    tenant_id: uuid.UUID
    conversation_ref: uuid.UUID
    # The raw external id the conversation ref was derived from. Carried so a
    # later step can hand the *external* id to the worker, which derives the ref
    # itself. Passing the already-derived ref instead makes it derive twice and
    # files the run under a different conversation than the turn it answers -
    # the answer is produced and then never found.
    external_ref: str
    # The account this visitor has **proven ownership of**, or None.
    #
    # Proven, not claimed: it is set only by `POST /v1/support/verify`, after
    # the caller demonstrated knowledge of an order's phone tail. Feature list
    # 2.2/2.5 - without it, an anonymous visitor asking "where is my order" is
    # served *somebody's* order data, which is exactly the leak the list marks
    # as a red line. It rides on the token rather than in a database session
    # because the token is the identity carrier of this surface (ADR 0011):
    # the worker never sees the token, so the account travels into the run's
    # payload at queue time, and the expiry of the proof is the expiry of the
    # token - one lifetime to reason about, not two.
    account: str | None = None


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _secret() -> bytes:
    from platform_core.config import get_settings

    return str(get_settings().secret_key).encode("utf-8")


def _sign(payload: str) -> str:
    return _b64e(hmac.new(_secret(), payload.encode("ascii"), hashlib.sha256).digest())


def issue(
    tenant_id: uuid.UUID,
    conversation_ref: uuid.UUID,
    external_ref: str,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: int | None = None,
    account: str | None = None,
) -> tuple[str, int]:
    """Mint a token. Returns (token, expires_at).

    `account` is the proven-ownership claim (see `VisitorClaim`); it is omitted
    for a fresh session and set only when a verification step succeeded, which
    re-issues the token.
    """
    issued = int(time.time() if now is None else now)
    expires_at = issued + int(ttl_seconds)
    payload = _b64e(
        json.dumps(
            {
                "t": str(tenant_id),
                "c": str(conversation_ref),
                "x": external_ref,
                "e": expires_at,
                # Omitted entirely when absent, so an unverified token stays
                # short and a verified one differs visibly in the payload.
                **({"a": account} if account else {}),
            },
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return f"{PREFIX}{payload}.{_sign(payload)}", expires_at


def verify(token: str, *, now: int | None = None) -> VisitorClaim:
    """Check signature and expiry, and return what the token is bound to.

    Signature is compared with `compare_digest` so a forgery attempt cannot be
    narrowed by timing. Expiry is checked *after* the signature: an attacker who
    cannot sign should not be able to learn anything from the error, and a
    correctly signed but stale token must still be refused.
    """
    if not token.startswith(PREFIX):
        raise VisitorTokenError("not a visitor token")
    payload, separator, signature = token[len(PREFIX) :].partition(".")
    if not separator or not payload or not signature:
        raise VisitorTokenError("malformed token")
    if not hmac.compare_digest(signature, _sign(payload)):
        raise VisitorTokenError("signature does not verify")

    try:
        claims = json.loads(_b64d(payload))
        tenant_id = uuid.UUID(str(claims["t"]))
        conversation_ref = uuid.UUID(str(claims["c"]))
        external_ref = str(claims["x"])
        expires_at = int(claims["e"])
        account = claims.get("a")
    except Exception as exc:  # noqa: BLE001 - any parse failure is a bad token
        raise VisitorTokenError("payload is unreadable") from exc

    if int(time.time() if now is None else now) >= expires_at:
        raise VisitorTokenError("token has expired")

    return VisitorClaim(
        tenant_id=tenant_id,
        conversation_ref=conversation_ref,
        external_ref=external_ref,
        account=str(account) if account else None,
    )
