"""Unit tests: OIDC verifier + membership resolution (ticket 23).

Uses a locally generated RSA key to emulate a realm JWKS endpoint; the
live Keycloak path was verified manually during integration setup
(realm `platform`, client `platform-api`, user e2e-agent).
"""

import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from platform_core.identity.oidc import OidcError, OidcVerifier, ResolvedIdentity

ISSUER = "http://kc.test/realms/platform"
AUDIENCE = "platform-api"

# --- RSA test key + fake JWKS ---


def _make_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


KEY = _make_key()
KID = "test-key-1"


class FakeJWKClient:
    def __init__(self, key, kid: str) -> None:
        self._key = key
        self._kid = kid

    def get_signing_key_from_jwt(self, token: str):
        from types import SimpleNamespace

        return SimpleNamespace(key=self._key)


@pytest.fixture
def verifier(monkeypatch: pytest.MonkeyPatch) -> OidcVerifier:
    v = OidcVerifier(ISSUER, AUDIENCE)
    monkeypatch.setattr(v, "_jwks", FakeJWKClient(KEY.public_key(), KID))
    return v


def _token(
    *,
    sub: str = "kc-sub-1",
    azp: str = AUDIENCE,
    aud=None,
    iss: str = ISSUER,
    now: int | None = None,
    exp_delta: int = 300,
    extra: dict | None = None,
) -> str:
    ts = int(now if now is not None else time.time())
    claims = {
        "exp": ts + exp_delta,
        "iat": ts,
        "iss": iss,
        "sub": sub,
        "azp": azp,
        "preferred_username": "e2e-agent",
    }
    if aud is not None:
        claims["aud"] = aud
    if extra:
        claims.update(extra)
    return jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": KID})


def test_valid_token_verifies(verifier: OidcVerifier) -> None:
    claims = verifier.verify(_token(aud="account"), now=int(time.time()))
    assert claims["sub"] == "kc-sub-1"


def test_azp_accepted_as_audience(verifier: OidcVerifier) -> None:
    claims = verifier.verify(_token(), now=int(time.time()))
    assert claims["sub"] == "kc-sub-1"


def test_wrong_audience_and_azp_rejected(verifier: OidcVerifier) -> None:
    with pytest.raises(OidcError, match="audience"):
        verifier.verify(_token(aud="other-client", azp="other-client"))


def test_expired_token_rejected(verifier: OidcVerifier) -> None:
    past = int(time.time()) - 600
    with pytest.raises(OidcError, match="expired|invalid"):
        verifier.verify(_token(now=past, exp_delta=60), now=int(time.time()))


def test_wrong_issuer_rejected(verifier: OidcVerifier) -> None:
    with pytest.raises(OidcError):
        verifier.verify(_token(iss="http://evil.test/realms/platform"))


def test_tampered_token_rejected(verifier: OidcVerifier) -> None:
    good = _token()
    with pytest.raises(OidcError):
        verifier.verify(good[:-3] + "abc")


def test_missing_required_claims_rejected(verifier: OidcVerifier) -> None:
    ts = int(time.time())
    bad = jwt.encode({"iss": ISSUER, "sub": "s1"}, KEY, algorithm="RS256", headers={"kid": KID})
    with pytest.raises(OidcError):
        verifier.verify(bad, now=ts)


# --- ResolvedIdentity dataclass sanity ---


def test_resolved_identity_shape() -> None:
    r = ResolvedIdentity(
        user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role="support_agent", actor_kind="user"
    )
    assert r.role == "support_agent"
