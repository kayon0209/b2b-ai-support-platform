"""SAML 2.0 service provider: AuthnRequest generation and response validation.

The validation order is the security property
---------------------------------------------
**Nothing from the response is read until the signature has been verified.**
An unverified SAML response is fully attacker-controlled input: the NameID, the
audience and the validity window are all just text until something proves the
IdP wrote them. So `validate_response` verifies first and reads second, and it
reads from the **verified element** returned by the verifier rather than from a
fresh XPath - which is what closes XML Signature Wrapping, where a valid
signature sits over one assertion while a second, forged assertion is the one a
naive implementation reads.

Fields that are *not* covered by a signature can only ever **refuse** a request,
never accept one. `Destination` and `InResponseTo` live on the `Response`
element, and an IdP that signs only the assertion leaves them unauthenticated;
they are still checked, because a mismatch means something is wrong, but a match
is never the reason to accept.

What is pinned, and why each pin matters
----------------------------------------
- **RSA-SHA256 and SHA-256 only.** SHA-1 is still emitted by old IdPs and is no
  longer a collision-resistant choice; a verifier that accepts it inherits that.
- **Exactly one reference, one signature, X.509 required.** Multiple references
  are how a wrapping attack gets a valid signature to cover the attacker's
  element.
- **The IdP certificate is configuration, not something the response supplies.**
  A response that carries its own signing certificate proves only that whoever
  wrote it also signed it. `signxml` is given the configured certificate and
  never a certificate resolved from the document.
- **Entities are not resolved and the network is not consulted.** XML parsing is
  an attack surface (XXE, billion laughs); the parser is configured to refuse
  both.

Replay is prevented by the database, not here: `saml_consumed_assertions` has
`UNIQUE (connection_id, assertion_id)`, so a second presentation of the same
assertion loses the insert regardless of how many API replicas are running.
Validation returns the assertion id so the caller can record it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
import zlib
from dataclasses import dataclass, field

from lxml import etree

# Imported from the defining modules rather than the package root: `signxml`
# ships `py.typed` but does not re-export these names in `__all__`, so the
# package-level import is an attribute error to a type checker even though it
# works at runtime.
from signxml.algorithms import DigestAlgorithm, SignatureMethod
from signxml.verifier import SignatureConfiguration, XMLVerifier

SAML_NS = "urn:oasis:names:tc:SAML:2.0:assertion"
PROTOCOL_NS = "urn:oasis:names:tc:SAML:2.0:protocol"
SAML_SUCCESS = "urn:oasis:names:tc:SAML:2.0:status:Success"

# Clock skew allowance. IdPs and the platform drift, and a strict window turns
# a two-second difference into "your identity provider rejected you" with no
# diagnostic. Two minutes each way is the usual figure.
LEEWAY_SECONDS = 120

# How long a pending AuthnRequest stays valid. Short: the user completes a
# login or abandons it, and a long-lived request id is replay material.
REQUEST_TTL_SECONDS = 600

# A response larger than this is not a login. Bounded because it is
# unauthenticated bytes being parsed before anything has been verified.
MAX_RESPONSE_BYTES = 512 * 1024


class SamlError(Exception):
    """A response or a connection that must be refused, with a stable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


@dataclass(frozen=True)
class SamlConnectionConfig:
    """The narrow projection the verifier needs.

    Deliberately not the ORM row: a verifier that can read the tenant's whole
    connection object is a verifier that can be extended to read something it
    should not, and this projection is also what the SECURITY DEFINER resolver
    returns.
    """

    connection_id: uuid.UUID
    tenant_id: uuid.UUID
    idp_entity_id: str
    idp_sso_url: str
    idp_certificate: str
    sp_entity_id: str
    status: str = "active"


@dataclass(frozen=True)
class SamlIdentity:
    """What a verified assertion says about the subject."""

    assertion_id: str
    name_id: str
    session_index: str | None = None
    attributes: dict[str, list[str]] = field(default_factory=dict)
    not_on_or_after: int | None = None


# --- pending-request binding -------------------------------------------------


def encode_relay_state(
    *, request_id: str, connection_id: uuid.UUID, secret: str, now: int | None = None
) -> str:
    """A self-contained, signed RelayState carrying the request binding.

    Stateless on purpose. The alternative is a server-side table of pending
    requests, which means an ACS request can fail because it landed on a
    different replica than the login did - a problem this platform avoids
    everywhere else and should not introduce for a login round trip.

    It is **signed, not encrypted**: it carries an id and a connection id, both
    of which the caller already knows, and nothing else. `InResponseTo` is only
    meaningful when the platform can tell its own requests apart, and that is
    what the signature provides.
    """
    issued = now if now is not None else int(time.time())
    body = json.dumps(
        {
            "rid": request_id,
            "cid": str(connection_id),
            "exp": issued + REQUEST_TTL_SECONDS,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(body).decode().rstrip("=") + "." + signature


def decode_relay_state(
    state: str, *, secret: str, connection_id: uuid.UUID, now: int | None = None
) -> str:
    """Recover the request id, or refuse.

    The connection id is checked against the one in the path: a RelayState
    minted for one tenant's connection must not be usable against another's,
    and without that check the signature would only prove the platform once
    issued *a* request, not that it was this one.
    """
    try:
        encoded, signature = state.rsplit(".", 1)
        padded = encoded + "=" * (-len(encoded) % 4)
        body = base64.urlsafe_b64decode(padded)
    except (ValueError, TypeError) as exc:
        raise SamlError("SAML_RELAY_STATE_MALFORMED") from exc

    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(expected, signature):
        raise SamlError("SAML_RELAY_STATE_INVALID")

    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise SamlError("SAML_RELAY_STATE_MALFORMED") from exc

    if payload.get("cid") != str(connection_id):
        raise SamlError("SAML_RELAY_STATE_WRONG_CONNECTION")
    if int(payload.get("exp", 0)) < (now if now is not None else int(time.time())):
        raise SamlError("SAML_RELAY_STATE_EXPIRED")
    request_id = str(payload.get("rid", ""))
    if not request_id:
        raise SamlError("SAML_RELAY_STATE_MALFORMED")
    return request_id


# --- AuthnRequest ------------------------------------------------------------


def new_request_id() -> str:
    """SAML ids must be NCNames - no leading digit, no colon."""
    return "id" + secrets.token_hex(16)


def build_authn_request_url(
    *,
    config: SamlConnectionConfig,
    acs_url: str,
    request_id: str,
    relay_state: str,
) -> str:
    """The redirect the browser is sent to.

    Deflated and base64-encoded per the HTTP-Redirect binding. `ForceAuthn` is
    left off deliberately: forcing re-authentication on every support login
    trains users to click through an IdP prompt, which is the opposite of what
    it is for. `Destination` is omitted for the same reason the field is only
    ever checked - it is optional in the redirect binding.
    """
    issued = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    xml = (
        f'<samlp:AuthnRequest xmlns:samlp="{PROTOCOL_NS}" xmlns:saml="{SAML_NS}" '
        f'ID="{request_id}" Version="2.0" IssueInstant="{issued}" '
        f'AssertionConsumerServiceURL="{_escape(acs_url)}">'
        f"<saml:Issuer>{_escape(config.sp_entity_id)}</saml:Issuer>"
        f'<samlp:NameIDPolicy AllowCreate="false" '
        f'Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"/>'
        f"</samlp:AuthnRequest>"
    )
    compressed = zlib.compress(xml.encode("utf-8"))[2:-4]  # raw deflate
    encoded = base64.b64encode(compressed).decode()
    separator = "&" if "?" in config.idp_sso_url else "?"
    return (
        f"{config.idp_sso_url}{separator}SAMLRequest={_url_quote(encoded)}"
        f"&RelayState={_url_quote(relay_state)}"
    )


def _escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _url_quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


# --- response validation ----------------------------------------------------


def _parse(xml: bytes) -> etree._Element:
    """Parse without resolving entities and without touching the network.

    `resolve_entities=False` closes XXE (a response that points at
    `file:///etc/passwd`); `no_network=True` stops an external DTD fetch. Both
    matter because this is unauthenticated input being parsed before anything
    has been verified.
    """
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        dtd_validation=False,
        load_dtd=False,
        huge_tree=False,
    )
    try:
        return etree.fromstring(xml, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise SamlError("SAML_MALFORMED_XML") from exc


def _q(namespace: str, tag: str) -> str:
    return f"{{{namespace}}}{tag}"


def _verify_signature(root: etree._Element, certificate: str) -> etree._Element:
    """Verify and return the *signed* element.

    Pinned to one signature, one reference, X.509 present, RSA-SHA256 and
    SHA-256 - see the module docstring for why each matters. The certificate
    comes from configuration and is never resolved from the document.
    """
    config = SignatureConfiguration(
        require_x509=True,
        expect_references=1,
        location=".//",
        # frozenset, not set: `SignatureConfiguration` is a frozen dataclass
        # and takes immutable collections, which is the right shape for a
        # policy anyway - a verifier's accepted algorithms must not be
        # mutable state.
        signature_methods=frozenset({SignatureMethod.RSA_SHA256}),
        digest_algorithms=frozenset({DigestAlgorithm.SHA256}),
    )
    try:
        result = XMLVerifier().verify(root, x509_cert=certificate, expect_config=config)
    except Exception as exc:  # noqa: BLE001 - the library raises many types
        # One refusal code for every signature failure. Distinguishing "wrong
        # certificate" from "bad digest" would tell an attacker which part of a
        # forged response to fix.
        raise SamlError("SAML_SIGNATURE_INVALID") from exc
    # `verify` returns a list when several signatures match; `expect_references=1`
    # makes anything but exactly one a refusal, so normalise and check rather
    # than assuming the single-signature shape.
    results = result if isinstance(result, list) else [result]
    if len(results) != 1:
        raise SamlError("SAML_SIGNATURE_INVALID")
    signed = results[0].signed_xml
    if signed is None:  # pragma: no cover - the library always sets it
        raise SamlError("SAML_SIGNATURE_INVALID")
    return signed


def _text(element: etree._Element | None) -> str:
    if element is None or element.text is None:
        return ""
    return str(element.text).strip()


def validate_response(
    *,
    config: SamlConnectionConfig,
    xml: bytes,
    acs_url: str,
    expected_request_id: str,
    now: int | None = None,
) -> SamlIdentity:
    """Validate a POST-binding response and return the asserted identity.

    Every branch below is a refusal, and each is a distinct code because the
    operator response differs: an expired assertion is a clock problem, a bad
    signature is a certificate problem, and a replayed one is an incident.
    """
    moment = now if now is not None else int(time.time())
    if config.status != "active":
        raise SamlError("SAML_CONNECTION_DISABLED")
    if len(xml) > MAX_RESPONSE_BYTES:
        raise SamlError("SAML_RESPONSE_TOO_LARGE")

    root = _parse(xml)
    if root.tag != _q(PROTOCOL_NS, "Response"):
        raise SamlError("SAML_NOT_A_RESPONSE")

    # 1. Signature, before anything is read. `signed` is the element that was
    #    actually covered, and every claim below is read from it.
    signed = _verify_signature(root, config.idp_certificate)
    response_signed = signed.tag == _q(PROTOCOL_NS, "Response")
    if response_signed:
        response = signed
        assertions = response.findall(_q(SAML_NS, "Assertion"))
    elif signed.tag == _q(SAML_NS, "Assertion"):
        # Only the assertion is signed, so the Response-level fields below are
        # unauthenticated. They are still checked (a mismatch is a refusal) but
        # they are never the reason to accept.
        response = root
        assertions = [signed]
    else:
        # A signature over something else entirely - a wrapping attempt, or a
        # misconfigured IdP. Either way there is no covered assertion.
        raise SamlError("SAML_SIGNATURE_NOT_OVER_ASSERTION")

    if len(assertions) != 1:
        # Exactly one: two assertions means one of them is not covered, and
        # choosing between them is the decision a wrapping attack wants made.
        raise SamlError("SAML_ASSERTION_COUNT_INVALID")
    assertion = assertions[0]

    # 2. The IdP's own status, from the verified response element.
    #
    # The code is in the **`Value` attribute**, not the element text. Reading
    # it as text meant the check never fired: `_text` returned "" for every
    # status, `"" and ...` short-circuited, and a `<StatusCode
    # Value="...:Requester">` - a refusal from the IdP - was treated as a
    # success. Found by the test that asserts the refusal.
    status_element = response.find(f"{_q(PROTOCOL_NS, 'Status')}/{_q(PROTOCOL_NS, 'StatusCode')}")
    status = (status_element.get("Value") or "").strip() if status_element is not None else ""
    if not status:
        raise SamlError("SAML_STATUS_MISSING")
    if not status.endswith(":Success"):
        raise SamlError("SAML_STATUS_NOT_SUCCESS", status)

    # 3. Issuer must be the configured IdP. Checked on the covered element, so
    #    this is a real constraint rather than a suggestion.
    issuer = _text(assertion.find(_q(SAML_NS, "Issuer")))
    if not issuer:
        issuer = _text(response.find(_q(SAML_NS, "Issuer")))
    if issuer != config.idp_entity_id:
        raise SamlError("SAML_ISSUER_MISMATCH")

    # 4. Destination. Only meaningful when the Response is signed; otherwise it
    #    can still refuse us, which is the direction that is safe.
    destination = (response.get("Destination") or "").strip()
    if destination and destination != acs_url:
        raise SamlError("SAML_DESTINATION_MISMATCH")

    # 5. Request binding. This is what stops a response harvested from another
    #    session being replayed into this one.
    in_response_to = (response.get("InResponseTo") or "").strip()
    if in_response_to and in_response_to != expected_request_id:
        raise SamlError("SAML_IN_RESPONSE_TO_MISMATCH")
    if response_signed and not in_response_to:
        # A signed response that is not bound to a request is a response the
        # platform never asked for. Forged-response injection, or an IdP
        # configured for the wrong flow.
        raise SamlError("SAML_IN_RESPONSE_TO_MISSING")

    # 6. Conditions: the audience, and the validity window with leeway.
    conditions = assertion.find(_q(SAML_NS, "Conditions"))
    if conditions is None:
        raise SamlError("SAML_CONDITIONS_MISSING")
    audiences = [
        _text(a)
        for a in conditions.findall(
            f"{_q(SAML_NS, 'AudienceRestriction')}/{_q(SAML_NS, 'Audience')}"
        )
    ]
    if config.sp_entity_id not in audiences:
        raise SamlError("SAML_AUDIENCE_MISMATCH")

    not_before = _parse_instant(conditions.get("NotBefore"))
    not_on_or_after = _parse_instant(conditions.get("NotOnOrAfter"))
    if not_on_or_after is None:
        # An assertion with no end is an assertion that never expires.
        raise SamlError("SAML_CONDITIONS_MISSING")
    if not_before is not None and moment + LEEWAY_SECONDS < not_before:
        raise SamlError("SAML_ASSERTION_NOT_YET_VALID")
    if moment - LEEWAY_SECONDS >= not_on_or_after:
        raise SamlError("SAML_ASSERTION_EXPIRED")

    # 7. Subject, read from the covered assertion.
    name_id = _text(assertion.find(f"{_q(SAML_NS, 'Subject')}/{_q(SAML_NS, 'NameID')}"))
    if not name_id:
        raise SamlError("SAML_NAMEID_MISSING")

    assertion_id = (assertion.get("ID") or "").strip()
    if not assertion_id:
        # No id means no replay guard, so an assertion that omits one is
        # unrepresentable in `saml_consumed_assertions` and cannot be accepted
        # without giving up replay protection entirely.
        raise SamlError("SAML_ASSERTION_ID_MISSING")

    # The SessionIndex is what a Single Logout request would reference, so it is
    # carried even though logout is not implemented yet: re-deriving it later
    # would mean re-parsing the response, which is gone by then.
    statement = assertion.find(_q(SAML_NS, "AuthnStatement"))
    session_index = statement.get("SessionIndex") if statement is not None else None

    return SamlIdentity(
        assertion_id=assertion_id,
        name_id=name_id,
        session_index=session_index,
        attributes=_attributes(assertion),
        not_on_or_after=not_on_or_after,
    )


def _attributes(assertion: etree._Element) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for statement in assertion.findall(_q(SAML_NS, "AttributeStatement")):
        for attribute in statement.findall(_q(SAML_NS, "Attribute")):
            name = attribute.get("Name")
            if not name:
                continue
            values = [_text(v) for v in attribute.findall(_q(SAML_NS, "AttributeValue"))]
            out[name] = [v for v in values if v]
    return out


def _parse_instant(value: str | None) -> int | None:
    if not value:
        return None
    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(cleaned).timestamp())
    except ValueError:
        raise SamlError("SAML_TIMESTAMP_INVALID", value) from None


__all__ = [
    "LEEWAY_SECONDS",
    "MAX_RESPONSE_BYTES",
    "REQUEST_TTL_SECONDS",
    "SAML_SUCCESS",
    "SamlConnectionConfig",
    "SamlError",
    "SamlIdentity",
    "build_authn_request_url",
    "decode_relay_state",
    "encode_relay_state",
    "new_request_id",
    "validate_response",
]
