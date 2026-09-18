"""Integration tests: the SAML login flow against a real database.

The unit tests prove the validator refuses forged responses. These prove the
three things only a database can:

- **an assertion is accepted exactly once** - the replay guard is a UNIQUE
  constraint, and a check-then-insert would lose the race between two API
  replicas, which is precisely when a replayed bearer credential matters;
- **a verified identity with no membership is refused**, so a first login cannot
  mint itself access;
- **one tenant's connection cannot be used against another's**, since the
  connection id is in the URL and the tenant is derived from it.
"""

import asyncio
import base64
import os
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lxml import etree
from signxml import XMLSigner
from sqlalchemy import create_engine, text

from platform_core.config import get_settings
from platform_core.identity import saml as saml_proto

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)

TENANT = "0190d000-0000-7000-8000-000000000301"
TENANT_OTHER = "0190d000-0000-7000-8000-000000000302"
CONNECTION = "0190d000-0000-7000-8000-000000000303"

IDP_ENTITY = "https://idp.example.com/metadata"
SP_ENTITY = "https://api.example.com/saml/metadata"
NAME_ID = "ada@example.com"
# The id `_relay_state` mints. A signed response must be bound to a request the
# platform actually issued, so the fixture and the RelayState have to agree.
REQUEST_ID = "idabcdef"


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


def _run(coro):
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


@pytest.fixture(scope="module", autouse=True)
def configured_secret():
    """`_secret` refuses to sign a RelayState with no configured key, which is
    the correct behaviour and would make every ACS test a 401."""
    previous = os.environ.get("APP_SECRET_KEY")
    os.environ["APP_SECRET_KEY"] = "integration-secret-for-saml-relay-state"
    get_settings.cache_clear()
    yield
    if previous is None:
        os.environ.pop("APP_SECRET_KEY", None)
    else:
        os.environ["APP_SECRET_KEY"] = previous
    get_settings.cache_clear()


def _clean_rows(conn) -> None:
    """Per-test state only.

    `saml_connections` is **not** here: the module fixture seeds it once, and a
    per-test delete would remove it before every test and make every request a
    404 - which is exactly what the first version of this file did.
    """
    conn.execute(
        text("DELETE FROM saml_consumed_assertions WHERE tenant_id IN (:a, :b)"),
        {"a": TENANT, "b": TENANT_OTHER},
    )
    conn.execute(
        text("DELETE FROM external_identities WHERE tenant_id IN (:a, :b)"),
        {"a": TENANT, "b": TENANT_OTHER},
    )
    conn.execute(
        text("DELETE FROM memberships WHERE tenant_id IN (:a, :b)"),
        {"a": TENANT, "b": TENANT_OTHER},
    )
    conn.execute(
        text("DELETE FROM audit_events WHERE tenant_id IN (:a, :b)"),
        {"a": TENANT, "b": TENANT_OTHER},
    )
    conn.execute(
        text("DELETE FROM users WHERE primary_email LIKE 'ada@%' OR primary_email LIKE 'saml-%'")
    )


def _clean_all(conn) -> None:
    """Everything, including the connection and the tenants. Module teardown
    only - see `_clean_rows`."""
    _clean_rows(conn)
    conn.execute(
        text("DELETE FROM saml_connections WHERE tenant_id IN (:a, :b)"),
        {"a": TENANT, "b": TENANT_OTHER},
    )


@pytest.fixture(scope="module", autouse=True)
def seed():
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT, "saml-t1"), (TENANT_OTHER, "saml-t2")):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, :name, 'active') "
                    "ON CONFLICT (slug) DO UPDATE SET id = EXCLUDED.id, status = 'active'"
                ),
                {"id": tid, "slug": slug, "name": slug},
            )
        conn.execute(
            text(
                "INSERT INTO saml_connections (id, tenant_id, name, idp_entity_id, "
                "idp_sso_url, idp_certificate, sp_entity_id, status) VALUES "
                "(:id, :t, 'primary', :idp, 'https://idp.example.com/sso', :cert, :sp, 'active') "
                "ON CONFLICT (tenant_id, name) DO UPDATE "
                "SET idp_certificate = EXCLUDED.idp_certificate"
            ),
            {"id": CONNECTION, "t": TENANT, "idp": IDP_ENTITY, "cert": CERT, "sp": SP_ENTITY},
        )
    yield
    with admin.begin() as conn:
        _clean_all(conn)
        conn.execute(text("DELETE FROM tenants WHERE slug LIKE 'saml-t%'"))
    admin.dispose()


@pytest.fixture(autouse=True)
def clean_rows():
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        _clean_rows(conn)
    yield
    with admin.begin() as conn:
        _clean_rows(conn)
    admin.dispose()


def _client() -> TestClient:
    import importlib

    main_mod = importlib.import_module("platform_core.main")
    fresh = FastAPI()
    for route in main_mod.app.router.routes:
        fresh.router.routes.append(route)
    return TestClient(fresh, raise_server_exceptions=False)


def _secret() -> str:
    value = get_settings().secret_key
    return value.get_secret_value() if value else ""


def _response_xml(*, assertion_id: str, name_id: str = NAME_ID, audience: str = SP_ENTITY):
    now = int(time.time())

    def stamp(offset: int) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + offset))

    return etree.fromstring(
        f"""
    <samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"
                    xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion"
                    ID="response-1" Version="2.0" IssueInstant="{stamp(0)}"
                    InResponseTo="{REQUEST_ID}">
      <saml:Issuer>{IDP_ENTITY}</saml:Issuer>
      <samlp:Status>
        <samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/>
      </samlp:Status>
      <saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion"
                      ID="{assertion_id}" Version="2.0">
        <saml:Issuer>{IDP_ENTITY}</saml:Issuer>
        <saml:Subject>
          <saml:NameID>{name_id}</saml:NameID>
        </saml:Subject>
        <saml:Conditions NotBefore="{stamp(-300)}" NotOnOrAfter="{stamp(300)}">
          <saml:AudienceRestriction>
            <saml:Audience>{audience}</saml:Audience>
          </saml:AudienceRestriction>
        </saml:Conditions>
      </saml:Assertion>
    </samlp:Response>"""
    )


def _signed(*, assertion_id: str = "assertion-1", **kwargs: object) -> str:
    signed = XMLSigner().sign(
        _response_xml(assertion_id=assertion_id, **kwargs), key=KEY, cert=[CERT]
    )
    return base64.b64encode(etree.tostring(signed)).decode()


def _relay_state(connection_id: str = CONNECTION) -> str:
    return saml_proto.encode_relay_state(
        request_id=REQUEST_ID, connection_id=uuid.UUID(connection_id), secret=_secret()
    )


def _post_acs(
    *, saml_response: str, connection_id: str = CONNECTION, relay_state: str | None = None
):
    return _client().post(
        f"/v1/saml/{connection_id}/acs",
        data={
            "SAMLResponse": saml_response,
            "RelayState": relay_state if relay_state is not None else _relay_state(connection_id),
        },
    )


def _seed_membership(*, role: str = "support_agent", tenant: str = TENANT) -> str:
    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as conn:
            user_id = conn.execute(
                text(
                    "INSERT INTO users (id, primary_email, display_name, is_service_account) "
                    "VALUES (gen_random_uuid(), :email, 'Ada', false) RETURNING id"
                ),
                {"email": NAME_ID},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO memberships (id, tenant_id, user_id, role, status) VALUES "
                    "(gen_random_uuid(), :t, :u, :role, 'active')"
                ),
                {"t": tenant, "u": user_id, "role": role},
            )
    finally:
        admin.dispose()
    return str(user_id)


# --- the flow ---------------------------------------------------------------


def test_login_redirects_to_the_identity_provider() -> None:
    resp = _client().get(f"/v1/saml/{CONNECTION}/login", follow_redirects=False)

    assert resp.status_code == 307, resp.text
    assert resp.headers["location"].startswith("https://idp.example.com/sso?")
    assert "SAMLRequest=" in resp.headers["location"]
    assert "RelayState=" in resp.headers["location"]


def test_login_for_an_unknown_connection_is_a_404() -> None:
    resp = _client().get(f"/v1/saml/{uuid.uuid4()}/login", follow_redirects=False)
    assert resp.status_code == 404


def test_a_valid_assertion_resolves_the_membership() -> None:
    _seed_membership(role="support_agent")

    resp = _post_acs(saml_response=_signed())

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["role"] == "support_agent"
    assert payload["connection_id"] == CONNECTION
    # A user that did not exist was created, but with no role attached - the
    # membership below is the one the fixture granted.
    assert payload["created_user"] is False


def test_a_first_login_creates_the_user_but_never_a_role() -> None:
    """No membership means no access, however valid the assertion. Letting a
    first login create one would hand out a role derived from an IdP attribute,
    which is controlled by whoever administers the IdP."""
    resp = _post_acs(saml_response=_signed())

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "SAML_NO_MEMBERSHIP"

    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as conn:
            # The user exists so the next login can be recognised...
            assert (
                conn.execute(
                    text("SELECT count(*) FROM users WHERE primary_email = :e"),
                    {"e": NAME_ID},
                ).scalar_one()
                == 1
            )
            # ...and no membership was invented for them.
            assert (
                conn.execute(
                    text("SELECT count(*) FROM memberships WHERE tenant_id = :t"),
                    {"t": TENANT},
                ).scalar_one()
                == 0
            )
    finally:
        admin.dispose()


def test_a_second_presentation_of_the_same_assertion_is_refused() -> None:
    """The replay guard. `UNIQUE (connection_id, assertion_id)`, so this holds
    even when two API replicas process the same assertion concurrently."""
    _seed_membership()

    first = _post_acs(saml_response=_signed(assertion_id="assertion-replay"))
    second = _post_acs(saml_response=_signed(assertion_id="assertion-replay"))

    assert first.status_code == 200, first.text
    assert second.status_code == 401, second.text
    assert second.json()["error"]["code"] == "SAML_ASSERTION_REPLAYED"


def test_a_forged_assertion_is_refused_and_audited() -> None:
    _seed_membership()
    root = _response_xml(assertion_id="assertion-forged")
    unsigned = base64.b64encode(etree.tostring(root)).decode()

    resp = _post_acs(saml_response=unsigned)

    assert resp.status_code == 401, resp.text
    # One code for every validation failure: telling a caller which part of a
    # forged response failed is telling an attacker what to fix.
    assert resp.json()["error"]["code"] == "SAML_INVALID"

    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as conn:
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM audit_events WHERE tenant_id = :t "
                        "AND action = 'saml.login.refused'"
                    ),
                    {"t": TENANT},
                ).scalar_one()
                >= 1
            )
    finally:
        admin.dispose()


def test_a_relay_state_for_another_connection_is_refused() -> None:
    _seed_membership()
    resp = _post_acs(
        saml_response=_signed(assertion_id="assertion-other"),
        relay_state=_relay_state(TENANT_OTHER),
    )
    assert resp.status_code == 401, resp.text


def test_the_assertion_audience_must_match_this_service_provider() -> None:
    """A valid assertion issued for a different SP - the confused-deputy case."""
    _seed_membership()
    resp = _post_acs(
        saml_response=_signed(
            assertion_id="assertion-other-sp", audience="https://other.example.com"
        )
    )
    assert resp.status_code == 401, resp.text


def test_a_login_is_recorded_in_the_tenants_audit_trail() -> None:
    _seed_membership()
    assert _post_acs(saml_response=_signed(assertion_id="assertion-audit")).status_code == 200

    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as conn:
            row = conn.execute(
                text(
                    "SELECT action, decision, metadata::text FROM audit_events "
                    "WHERE tenant_id = :t AND action = 'saml.login'"
                ),
                {"t": TENANT},
            ).one()
    finally:
        admin.dispose()

    assert row[1] == "completed"
    # The subject is recorded as a hash: the trail proves a login happened for
    # a given identity without storing the identity in a second place.
    assert "subject_hash" in row[2]
    assert NAME_ID not in row[2]
