"""Unit tests: SAML response validation.

Every response here is **really signed** with `signxml` and a freshly generated
key, then tampered with in one specific way. Mocking the verifier would test the
mock; the questions that matter are what the *library* accepts and what this
code does with the answer.

The refusals are the feature. A SAML verifier that accepts everything passes a
happy-path test, so the tests below are mostly negative: wrong audience,
expired, not yet valid, wrong issuer, unbound to a request, destination
mismatch, unsigned, tampered, two assertions, signature over the wrong element.
Each is a distinct code because the operator response differs - an expired
assertion is a clock problem, a bad signature is a certificate problem.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from lxml import etree
from signxml import XMLSigner

from platform_core.identity import saml

TENANT = uuid.UUID("0190d000-0000-7000-8000-000000000201")
CONNECTION = uuid.UUID("0190d000-0000-7000-8000-000000000202")
ACS_URL = "https://api.example.com/v1/saml/0190d000-0000-7000-8000-000000000202/acs"
IDP_ENTITY = "https://idp.example.com/metadata"
SP_ENTITY = "https://api.example.com/saml/metadata"
REQUEST_ID = "id0123456789abcdef"
SECRET = "test-secret"

NOW = 1_800_000_000

_EMAIL_NAMEID_FORMAT = "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"
_PASSWORD_CONTEXT = "urn:oasis:names:tc:SAML:2.0:ac:classes:Password"


def _key_and_cert() -> tuple[object, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "idp.example.com")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    return key, cert.public_bytes(serialization.Encoding.PEM).decode()


KEY, CERT = _key_and_cert()
OTHER_KEY, _ = _key_and_cert()


def _config(**overrides: object) -> saml.SamlConnectionConfig:
    values: dict[str, object] = {
        "connection_id": CONNECTION,
        "tenant_id": TENANT,
        "idp_entity_id": IDP_ENTITY,
        "idp_sso_url": "https://idp.example.com/sso",
        "idp_certificate": CERT,
        "sp_entity_id": SP_ENTITY,
        "status": "active",
    }
    values.update(overrides)
    return saml.SamlConnectionConfig(**values)  # type: ignore[arg-type]


def _instant(offset_seconds: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + offset_seconds))


def _response_xml(
    *,
    audience: str = SP_ENTITY,
    issuer: str = IDP_ENTITY,
    name_id: str = "ada@example.com",
    assertion_id: str = "assertion-1",
    in_response_to: str | None = REQUEST_ID,
    destination: str = ACS_URL,
    not_before: int = -300,
    not_on_or_after: int = 300,
    status: str = "urn:oasis:names:tc:SAML:2.0:status:Success",
    extra_assertion: bool = False,
) -> etree._Element:
    """Build an unsigned SAML Response. Signing happens in the test, because
    *what* is signed is one of the things under test."""
    in_response_attr = f' InResponseTo="{in_response_to}"' if in_response_to else ""
    assertion = f"""
    <saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion"
                    ID="{assertion_id}" Version="2.0">
      <saml:Issuer>{issuer}</saml:Issuer>
      <saml:Subject>
        <saml:NameID Format="{_EMAIL_NAMEID_FORMAT}">{name_id}</saml:NameID>
      </saml:Subject>
      <saml:Conditions NotBefore="{_instant(not_before)}"
                      NotOnOrAfter="{_instant(not_on_or_after)}">
        <saml:AudienceRestriction>
          <saml:Audience>{audience}</saml:Audience>
        </saml:AudienceRestriction>
      </saml:Conditions>
      <saml:AuthnStatement SessionIndex="session-1">
        <saml:AuthnContext>
          <saml:AuthnContextClassRef>{_PASSWORD_CONTEXT}</saml:AuthnContextClassRef>
        </saml:AuthnContext>
      </saml:AuthnStatement>
      <saml:AttributeStatement>
        <saml:Attribute Name="department">
          <saml:AttributeValue>support</saml:AttributeValue>
        </saml:Attribute>
      </saml:AttributeStatement>
    </saml:Assertion>"""
    duplicate = assertion if extra_assertion else ""
    return etree.fromstring(
        f"""
    <samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"
                    xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion"
                    ID="response-1" Version="2.0" IssueInstant="{_instant(0)}"
                    Destination="{destination}"{in_response_attr}>
      <saml:Issuer>{issuer}</saml:Issuer>
      <samlp:Status><samlp:StatusCode Value="{status}"/></samlp:Status>
      {assertion}
      {duplicate}
    </samlp:Response>"""
    )


def _sign(element: etree._Element, *, key: object = KEY) -> bytes:
    signed = XMLSigner().sign(element, key=key, cert=[CERT])
    return etree.tostring(signed)


def _sign_response(*, key: object = KEY, **kwargs: object) -> bytes:
    """Sign the whole Response - what most IdPs do."""
    return _sign(_response_xml(**kwargs), key=key)


def _sign_assertion(*, key: object = KEY, **kwargs: object) -> bytes:
    """Sign only the Assertion, in place inside the Response. The Response is
    then unsigned, which is the case where response-level fields are advisory.

    Signed *in place* rather than extracted and re-appended: canonicalisation
    depends on the inherited namespace context, so moving a signed element
    between parents invalidates its digest. That is a fact about XML DSig, not
    about this test, and an IdP that signs the assertion has the same
    constraint.
    """
    response = _response_xml(**kwargs)
    assertion = response.find("{urn:oasis:names:tc:SAML:2.0:assertion}Assertion")
    assert assertion is not None
    signed = XMLSigner().sign(assertion, key=key, cert=[CERT])
    response.replace(assertion, signed)
    return etree.tostring(response)


def _validate(xml: bytes, *, config: saml.SamlConnectionConfig | None = None, **kwargs: object):
    return saml.validate_response(
        config=config or _config(),
        xml=xml,
        acs_url=ACS_URL,
        expected_request_id=REQUEST_ID,
        now=NOW,
        **kwargs,  # type: ignore[arg-type]
    )


def _refusal(xml: bytes, **kwargs: object) -> str:
    with pytest.raises(saml.SamlError) as exc:
        _validate(xml, **kwargs)
    return exc.value.code


# --- the happy paths --------------------------------------------------------


def test_a_signed_response_is_accepted() -> None:
    identity = _validate(_sign_response())

    assert identity.name_id == "ada@example.com"
    assert identity.assertion_id == "assertion-1"
    assert identity.session_index == "session-1"
    assert identity.attributes == {"department": ["support"]}


def test_an_assertion_signed_inside_an_unsigned_response_is_accepted() -> None:
    """IdPs differ on what they sign. Both are valid SAML, and the assertion
    signature covers everything this code reads."""
    identity = _validate(_sign_assertion())

    assert identity.name_id == "ada@example.com"


def test_attributes_are_carried_through() -> None:
    """SCIM maps groups to departments; SAML carries the same information as
    attributes, so it has to survive validation."""
    identity = _validate(_sign_response())
    assert identity.attributes["department"] == ["support"]


# --- signature --------------------------------------------------------------


def test_an_unsigned_response_is_refused() -> None:
    assert _refusal(etree.tostring(_response_xml())) == "SAML_SIGNATURE_INVALID"


def test_a_response_signed_by_the_wrong_key_is_refused() -> None:
    assert _refusal(_sign_response(key=OTHER_KEY)) == "SAML_SIGNATURE_INVALID"


def test_a_tampered_assertion_is_refused() -> None:
    """The signature is over the assertion, so changing the NameID invalidates
    it. This is the whole reason nothing is read before verification."""
    root = etree.fromstring(_sign_response())
    name_id = root.find(
        "{urn:oasis:names:tc:SAML:2.0:assertion}Assertion/"
        "{urn:oasis:names:tc:SAML:2.0:assertion}Subject/"
        "{urn:oasis:names:tc:SAML:2.0:assertion}NameID"
    )
    assert name_id is not None
    name_id.text = "attacker@example.com"

    assert _refusal(etree.tostring(root)) == "SAML_SIGNATURE_INVALID"


def test_a_signature_over_your_own_element_is_not_accepted() -> None:
    """An assertion signed by an attacker's key with the attacker's certificate
    is refused because the verifier is given the *configured* certificate. It
    never trusts a certificate the document supplies."""
    assert _refusal(_sign_assertion(key=OTHER_KEY)) == "SAML_SIGNATURE_INVALID"


def test_two_assertions_are_refused() -> None:
    """One of them is not covered by the signature, and choosing between them
    is exactly the decision a wrapping attack wants the verifier to make."""
    assert _refusal(_sign_response(extra_assertion=True)) == "SAML_ASSERTION_COUNT_INVALID"


# --- claims ------------------------------------------------------------------


def test_a_wrong_audience_is_refused() -> None:
    """An assertion issued for a different service provider is valid SAML and
    must not be accepted here - that is the confused-deputy case."""
    assert _refusal(_sign_response(audience="https://other.example.com")) == (
        "SAML_AUDIENCE_MISMATCH"
    )


def test_a_wrong_issuer_is_refused() -> None:
    assert _refusal(_sign_response(issuer="https://evil.example.com/metadata")) == (
        "SAML_ISSUER_MISMATCH"
    )


def test_an_expired_assertion_is_refused() -> None:
    assert _refusal(_sign_response(not_before=-3600, not_on_or_after=-300)) == (
        "SAML_ASSERTION_EXPIRED"
    )


def test_an_assertion_from_the_future_is_refused() -> None:
    assert _refusal(_sign_response(not_before=3600, not_on_or_after=7200)) == (
        "SAML_ASSERTION_NOT_YET_VALID"
    )


def test_leeway_is_applied_in_both_directions() -> None:
    """A strict window turns a two-second clock difference into "your identity
    provider rejected you" with no diagnostic."""
    just_expired = _sign_response(not_before=-3600, not_on_or_after=-60)
    just_started = _sign_response(not_before=60, not_on_or_after=3600)

    # Inside the leeway on both sides, so both are accepted.
    assert _validate(just_expired).name_id == "ada@example.com"
    assert _validate(just_started).name_id == "ada@example.com"


def test_a_destination_mismatch_is_refused() -> None:
    """Destination is not covered when only the assertion is signed, which is
    why it is only ever allowed to *refuse*. A mismatch still means something
    is wrong."""
    assert _refusal(_sign_response(destination="https://elsewhere.example.com/acs")) == (
        "SAML_DESTINATION_MISMATCH"
    )


def test_a_failed_status_is_refused() -> None:
    code = _refusal(_sign_response(status="urn:oasis:names:tc:SAML:2.0:status:Requester"))
    assert code == "SAML_STATUS_NOT_SUCCESS"


# --- request binding --------------------------------------------------------


def test_a_response_bound_to_another_request_is_refused() -> None:
    """The assertion is perfectly valid and was issued for somebody else's
    login. Without this check it would be replayable into any session."""
    assert _refusal(_sign_response(in_response_to="id-someone-else")) == (
        "SAML_IN_RESPONSE_TO_MISMATCH"
    )


def test_a_signed_response_with_no_request_binding_is_refused() -> None:
    """A signed response the platform never asked for: forged-response
    injection, or an IdP configured for the wrong flow."""
    assert _refusal(_sign_response(in_response_to=None)) == "SAML_IN_RESPONSE_TO_MISSING"


def test_a_missing_name_id_is_refused() -> None:
    assert _refusal(_sign_response(name_id="")) == "SAML_NAMEID_MISSING"


def test_an_assertion_without_an_id_is_refused() -> None:
    """No id means no replay guard: the assertion could be presented again for
    its whole validity window."""
    assert _refusal(_sign_response(assertion_id="")) == "SAML_ASSERTION_ID_MISSING"


# --- connection state -------------------------------------------------------


def test_a_disabled_connection_is_refused() -> None:
    assert _refusal(_sign_response(), config=_config(status="disabled")) == (
        "SAML_CONNECTION_DISABLED"
    )


def test_a_malformed_document_is_refused() -> None:
    assert _refusal(b"<not-xml") == "SAML_MALFORMED_XML"


def test_a_non_response_root_is_refused() -> None:
    assert (
        _refusal(
            etree.tostring(_response_xml().find("{urn:oasis:names:tc:SAML:2.0:assertion}Assertion"))
        )
        == "SAML_NOT_A_RESPONSE"
    )


def test_an_oversized_document_is_refused_before_parsing() -> None:
    """Unverified bytes are not worth the parsing cost, and a bound here is
    cheaper than a bound on the parser."""
    assert _refusal(b"x" * (saml.MAX_RESPONSE_BYTES + 1)) == "SAML_RESPONSE_TOO_LARGE"


def test_an_entity_expansion_attempt_is_refused() -> None:
    """XXE: a response that defines an entity pointing at a local file. The
    parser resolves no entities and touches no network."""
    xml = b"""<?xml version="1.0"?>
<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol">&xxe;</samlp:Response>"""

    assert _refusal(xml) in ("SAML_MALFORMED_XML", "SAML_SIGNATURE_INVALID")


# --- request binding: RelayState --------------------------------------------


def test_relay_state_round_trips() -> None:
    state = saml.encode_relay_state(
        request_id=REQUEST_ID, connection_id=CONNECTION, secret=SECRET, now=NOW
    )
    assert (
        saml.decode_relay_state(state, secret=SECRET, connection_id=CONNECTION, now=NOW + 10)
        == REQUEST_ID
    )


def test_a_tampered_relay_state_is_refused() -> None:
    state = saml.encode_relay_state(
        request_id=REQUEST_ID, connection_id=CONNECTION, secret=SECRET, now=NOW
    )
    body, signature = state.rsplit(".", 1)
    forged = saml.encode_relay_state(
        request_id="id-attacker", connection_id=CONNECTION, secret=SECRET, now=NOW
    ).rsplit(".", 1)[0]

    with pytest.raises(saml.SamlError) as exc:
        saml.decode_relay_state(
            f"{forged}.{signature}", secret=SECRET, connection_id=CONNECTION, now=NOW
        )
    assert exc.value.code == "SAML_RELAY_STATE_INVALID"


def test_a_relay_state_signed_with_another_secret_is_refused() -> None:
    state = saml.encode_relay_state(
        request_id=REQUEST_ID, connection_id=CONNECTION, secret="other", now=NOW
    )
    with pytest.raises(saml.SamlError) as exc:
        saml.decode_relay_state(state, secret=SECRET, connection_id=CONNECTION, now=NOW)
    assert exc.value.code == "SAML_RELAY_STATE_INVALID"


def test_a_relay_state_for_another_connection_is_refused() -> None:
    """Otherwise a RelayState minted for one tenant's connection would be
    accepted at another's - the signature would prove only that the platform
    once issued *a* request, not that it was this one."""
    other = uuid.UUID("0190d000-0000-7000-8000-000000000203")
    state = saml.encode_relay_state(
        request_id=REQUEST_ID, connection_id=CONNECTION, secret=SECRET, now=NOW
    )
    with pytest.raises(saml.SamlError) as exc:
        saml.decode_relay_state(state, secret=SECRET, connection_id=other, now=NOW)
    assert exc.value.code == "SAML_RELAY_STATE_WRONG_CONNECTION"


def test_an_expired_relay_state_is_refused() -> None:
    state = saml.encode_relay_state(
        request_id=REQUEST_ID, connection_id=CONNECTION, secret=SECRET, now=NOW
    )
    with pytest.raises(saml.SamlError) as exc:
        saml.decode_relay_state(
            state,
            secret=SECRET,
            connection_id=CONNECTION,
            now=NOW + saml.REQUEST_TTL_SECONDS + 1,
        )
    assert exc.value.code == "SAML_RELAY_STATE_EXPIRED"


def test_an_authn_request_url_carries_the_request_and_the_state() -> None:
    state = saml.encode_relay_state(
        request_id=REQUEST_ID, connection_id=CONNECTION, secret=SECRET, now=NOW
    )
    url = saml.build_authn_request_url(
        config=_config(), acs_url=ACS_URL, request_id=REQUEST_ID, relay_state=state
    )

    assert url.startswith("https://idp.example.com/sso?")
    assert "SAMLRequest=" in url and "RelayState=" in url


def test_a_new_request_id_is_an_xml_ncname() -> None:
    """SAML ids must be NCNames: no leading digit and no colon, or a strict IdP
    rejects the request before the user sees anything."""
    for _ in range(20):
        rid = saml.new_request_id()
        assert rid[0].isalpha()
        assert ":" not in rid
