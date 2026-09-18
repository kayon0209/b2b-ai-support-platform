"""Integration tests: provider-signed connector webhooks.

The last uncovered Phase 3 epic, "Webhook verification and replay protection":
only Chatwoot had a signed inbound endpoint, so no other provider could notify
the platform at all.

Two things here are load-bearing and neither is about the happy path:

- `test_the_tenant_comes_from_the_connector_not_the_payload` - a
  provider-supplied tenant field is attacker-controlled, and a webhook is the
  one endpoint reachable without a bearer token.
- `test_a_stale_delivery_is_rejected` / `test_a_duplicate_delivery_is_recorded_once`
  - without the replay window and the delivery-id dedupe, anyone who captures
  one signed request can replay it.

The tenant resolution is the hard part: `connectors` is FORCE-RLS'd, so an
unbound app-role read finds nothing. That is what migration 0026's
`resolve_connector_for_webhook` exists for, and
`test_the_resolver_function_is_not_publicly_executable` pins its grant.
"""

import asyncio
import json
import os
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from platform_core.support_bridge.webhook_security import sign_payload

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"

TENANT = "0190d000-0000-7000-8000-0000000000c1"
TENANT_OTHER = "0190d000-0000-7000-8000-0000000000c2"

SECRET_ENV = "TEST_CONNECTOR_WEBHOOK_SECRET"
SECRET_VALUE = "s3cret-signing-key"

_CLEAN: tuple[str, ...] = (
    "DELETE FROM inbox_events WHERE tenant_id IN (:a, :b)",
    "DELETE FROM connectors WHERE tenant_id IN (:a, :b)",
    "DELETE FROM audit_events WHERE tenant_id IN (:a, :b)",
)


def _clean(conn) -> None:
    for stmt in _CLEAN:
        conn.execute(text(stmt), {"a": TENANT, "b": TENANT_OTHER})


def _run(coro):
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


@pytest.fixture(scope="module", autouse=True)
def seed_tenants():
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT, "wh-t1"), (TENANT_OTHER, "wh-t2")):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, :name, 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug, "name": slug},
            )
    yield
    with admin.begin() as conn:
        _clean(conn)
        conn.execute(text("DELETE FROM tenants WHERE slug LIKE 'wh-t%'"))
    admin.dispose()


@pytest.fixture(autouse=True)
def clean_rows(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(SECRET_ENV, SECRET_VALUE)
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        _clean(conn)
    yield
    with admin.begin() as conn:
        _clean(conn)
    admin.dispose()


def _client() -> TestClient:
    """The real app, so the exempt-path wiring is under test.

    `TestClient(fresh_app)` with only the router mounted would pass even if the
    path were missing from the middleware's exempt list - and a webhook has no
    bearer token, so that omission would make it unreachable in production
    while every test still passed.
    """
    import importlib

    main_mod = importlib.import_module("platform_core.main")
    return TestClient(main_mod.app, raise_server_exceptions=False)


def _insert_connector(
    *,
    tenant: str = TENANT,
    status: str = "active",
    webhook_secret_ref: str | None = f"env://{SECRET_ENV}",
) -> str:
    cid = str(uuid.uuid4())
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO connectors (id, tenant_id, provider, name, status, "
                "capabilities, configuration, credential_ref, webhook_secret_ref) VALUES "
                "(:id, :tid, 'jira', 'jira-primary', :status, CAST('[]' AS jsonb), "
                "CAST('{}' AS jsonb), NULL, :ref)"
            ),
            {"id": cid, "tid": tenant, "status": status, "ref": webhook_secret_ref},
        )
    admin.dispose()
    return cid


def _deliver(
    connector_id: str,
    *,
    payload: dict,
    secret: str = SECRET_VALUE,
    timestamp: str | None = None,
    delivery_id: str | None = None,
    raw_body: bytes | None = None,
    omit_delivery: bool = False,
):
    body = raw_body if raw_body is not None else json.dumps(payload).encode()
    stamp = timestamp if timestamp is not None else str(int(time.time()))
    headers = {
        "X-Webhook-Signature": f"sha256={sign_payload(secret.encode(), stamp, body)}",
        "X-Webhook-Timestamp": stamp,
        "Content-Type": "application/json",
    }
    if not omit_delivery:
        headers["X-Webhook-Delivery"] = delivery_id or str(uuid.uuid4())
    return _client().post(f"/v1/webhooks/connectors/{connector_id}", content=body, headers=headers)


def _inbox_rows(tenant: str = TENANT) -> list[dict]:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT delivery_id, event_type, status, minimized_payload, payload_hash "
                    "FROM inbox_events WHERE tenant_id = :t ORDER BY received_at"
                ),
                {"t": tenant},
            )
            .mappings()
            .all()
        )
    admin.dispose()
    return [dict(r) for r in rows]


# --- reachability ----------------------------------------------------------


def test_the_webhook_is_reachable_without_a_bearer_token() -> None:
    """A webhook has no token; the signature is the authentication. If the
    path were missing from the middleware's exempt list the endpoint would
    401 before the handler ran, while a router-only test still passed."""
    cid = _insert_connector()
    resp = _deliver(cid, payload={"event": "jira:issue_updated", "issue": {"id": "1"}})
    assert resp.status_code == 202, resp.text


# --- refusals --------------------------------------------------------------


def test_an_unknown_connector_is_not_found() -> None:
    resp = _deliver(str(uuid.uuid4()), payload={"event": "x"})
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "WEBHOOK_UNKNOWN_CONNECTOR"


def test_an_inactive_connector_is_refused() -> None:
    """Turning a connector off has to stop its inbound traffic, not only its
    outbound calls."""
    cid = _insert_connector(status="disabled")
    resp = _deliver(cid, payload={"event": "x"})
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "WEBHOOK_CONNECTOR_INACTIVE"
    assert _inbox_rows() == []


def test_a_connector_without_a_resolvable_secret_is_refused() -> None:
    """Accepting here would be an unauthenticated write path. Distinct from a
    bad signature: this is a misconfiguration an operator fixes."""
    cid = _insert_connector(webhook_secret_ref=None)
    resp = _deliver(cid, payload={"event": "x"})
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "WEBHOOK_NOT_CONFIGURED"
    assert _inbox_rows() == []


def test_a_reference_that_does_not_resolve_is_refused() -> None:
    cid = _insert_connector(webhook_secret_ref="env://NO_SUCH_VARIABLE_ANYWHERE")
    resp = _deliver(cid, payload={"event": "x"})
    assert resp.status_code == 503, resp.text


def test_a_forged_signature_is_rejected() -> None:
    cid = _insert_connector()
    resp = _deliver(cid, payload={"event": "x"}, secret="not-the-right-key")
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "WEBHOOK_SIGNATURE_INVALID"
    assert _inbox_rows() == []


def test_a_stale_delivery_is_rejected() -> None:
    """Without the replay window, anyone who captures one signed request can
    replay it forever."""
    cid = _insert_connector()
    old = str(int(time.time()) - 10_000)
    resp = _deliver(cid, payload={"event": "x"}, timestamp=old)
    assert resp.status_code == 401, resp.text
    assert _inbox_rows() == []


def test_a_delivery_without_an_id_is_rejected() -> None:
    """No delivery id means no idempotency key, and provider retries would
    each become a separate record."""
    cid = _insert_connector()
    resp = _deliver(cid, payload={"event": "x"}, omit_delivery=True)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "WEBHOOK_DELIVERY_ID_MISSING"


def test_a_malformed_payload_is_rejected() -> None:
    cid = _insert_connector()
    resp = _deliver(cid, payload={}, raw_body=b"{not json")
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "WEBHOOK_PAYLOAD_MALFORMED"


# --- acceptance ------------------------------------------------------------


def test_a_valid_delivery_is_persisted_before_any_work() -> None:
    cid = _insert_connector()
    delivery = str(uuid.uuid4())

    resp = _deliver(
        cid,
        payload={"event": "jira:issue_updated", "issue": {"id": "SUP-1"}},
        delivery_id=delivery,
    )

    assert resp.status_code == 202, resp.text
    rows = _inbox_rows()
    assert len(rows) == 1
    assert rows[0]["delivery_id"] == delivery
    assert rows[0]["event_type"] == "jira:issue_updated"
    assert rows[0]["status"] == "received"
    # The raw body is not stored: docs/security.md forbids raw customer
    # content at rest, and a hash plus minimized fields is what's kept.
    assert "payload_hash" in rows[0] and rows[0]["payload_hash"]


def test_the_tenant_comes_from_the_connector_not_the_payload() -> None:
    """A provider-supplied tenant field is attacker-controlled, and a webhook
    is the one endpoint reachable without a bearer token."""
    cid = _insert_connector()
    resp = _deliver(
        cid,
        payload={
            "event": "jira:issue_updated",
            # A malicious or merely buggy provider could claim any tenant.
            "tenant_id": TENANT_OTHER,
            "account_id": TENANT_OTHER,
        },
    )

    assert resp.status_code == 202, resp.text
    assert len(_inbox_rows(TENANT)) == 1
    assert _inbox_rows(TENANT_OTHER) == []


def test_a_duplicate_delivery_is_recorded_once() -> None:
    """A provider retry is not an error, and answering 4xx would make it
    retry forever."""
    cid = _insert_connector()
    delivery = str(uuid.uuid4())
    payload = {"event": "jira:issue_updated", "issue": {"id": "SUP-1"}}

    first = _deliver(cid, payload=payload, delivery_id=delivery)
    second = _deliver(cid, payload=payload, delivery_id=delivery)

    assert first.status_code == 202, first.text
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "duplicate"
    assert len(_inbox_rows()) == 1


def test_two_tenants_may_use_the_same_delivery_id() -> None:
    """Deduplication is per tenant (`uq_inbox_delivery` is on
    `(tenant_id, delivery_id)`), so one tenant's provider cannot suppress
    another tenant's delivery."""
    first = _insert_connector(tenant=TENANT)
    second = _insert_connector(tenant=TENANT_OTHER)
    delivery = "shared-delivery-id"

    assert _deliver(first, payload={"event": "x"}, delivery_id=delivery).status_code == 202
    assert _deliver(second, payload={"event": "x"}, delivery_id=delivery).status_code == 202

    assert len(_inbox_rows(TENANT)) == 1
    assert len(_inbox_rows(TENANT_OTHER)) == 1


def test_the_event_type_falls_back_to_a_known_value() -> None:
    """A payload with no recognisable event name must not produce an empty
    event_type: the column is NOT NULL and an empty string would make the row
    unclassifiable."""
    cid = _insert_connector()
    resp = _deliver(cid, payload={"unexpected": "shape"})

    assert resp.status_code == 202, resp.text
    assert _inbox_rows()[0]["event_type"] == "unknown"


# --- the bootstrap function ------------------------------------------------


def test_the_resolver_function_is_not_publicly_executable() -> None:
    """The tenant bootstrap is the one place RLS is deliberately stepped
    around, so the grant is the security boundary. PUBLIC must not hold it."""
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        proacl = conn.execute(
            text("SELECT proacl FROM pg_proc WHERE proname = 'resolve_connector_for_webhook'")
        ).scalar_one()
    admin.dispose()

    grants = " ".join(proacl or [])
    assert "platform_app=X" in grants
    # `=X/` with an empty grantee is PUBLIC; its presence would mean any role
    # could resolve a connector id to a tenant.
    assert "=X/" not in grants.replace("platform=X/", "").replace("platform_app=X/", "")


def test_the_unbound_app_role_cannot_read_connectors_directly() -> None:
    """Why the function is necessary rather than convenient: without it the
    webhook path could not discover a tenant at all. An unbound read must
    return nothing - if RLS stopped enforcing that, this fails."""
    _insert_connector()

    async def _count_unbound() -> int:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from platform_core.db import create_engine as app_engine

        engine = app_engine(APP_URL)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                return int(
                    (
                        await session.execute(
                            text("SELECT count(*) FROM connectors WHERE id IS NOT NULL")
                        )
                    ).scalar_one()
                )
        finally:
            await engine.dispose()

    assert _run(_count_unbound()) == 0
