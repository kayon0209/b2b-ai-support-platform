"""Integration tests: signed webhook endpoint + InboxEvent dedup (tickets 5-6).

Covers E2E scenarios from docs/testing-and-evaluation.md:
- Duplicate webhook -> single InboxEvent row
- Bad signature -> 401, nothing persisted
- Unregistered account -> 202, nothing persisted
- Happy path -> InboxEvent row with minimized payload (no message content)

Uses the platform owner role for seeding/assertions and drives the endpoint
through TestClient; the endpoint itself runs with the app role's privileges
via the normal engine.
"""

import hashlib
import hmac
import json
import os
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
TENANT_A = "01900000-0000-7000-8000-000000000001"
CHATWOOT_ACCOUNT_ID = "9001"
SECRET = os.environ.get("WEBHOOK_TEST_SECRET", "test-webhook-secret")


@pytest.fixture(scope="module", autouse=True)
def seed_mapping() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, 'webhook-test', 'Webhook Tenant', 'active') "
                "ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT_A},
        )
        conn.execute(
            text(
                "DELETE FROM external_resource_refs WHERE system='chatwoot' "
                "AND resource_type='account' AND external_id = :aid"
            ),
            {"aid": CHATWOOT_ACCOUNT_ID},
        )
        conn.execute(
            text(
                "INSERT INTO external_resource_refs "
                "(id, tenant_id, system, resource_type, external_id) VALUES "
                "(:id, :tid, 'chatwoot', 'account', :aid)"
            ),
            {"id": uuid.uuid4(), "tid": TENANT_A, "aid": CHATWOOT_ACCOUNT_ID},
        )
    yield
    with admin.begin() as conn:
        conn.execute(
            text("DELETE FROM inbox_events WHERE tenant_id = :tid"),
            {"tid": TENANT_A},
        )
        conn.execute(
            text(
                "DELETE FROM external_resource_refs WHERE system='chatwoot' "
                "AND resource_type='account' AND external_id = :aid"
            ),
            {"aid": CHATWOOT_ACCOUNT_ID},
        )
        conn.execute(text("DELETE FROM tenants WHERE slug = 'webhook-test'"))
    admin.dispose()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from platform_core.config import Settings
    from platform_core.main import app

    fake = Settings(environment="local", chatwoot_webhook_secret=SECRET)  # type: ignore[call-arg]
    monkeypatch.setattr("platform_core.support_bridge.router.get_settings", lambda: fake)
    return TestClient(app, raise_server_exceptions=False)


def _signed_headers(body: bytes, delivery_id: str | None = None) -> dict[str, str]:
    ts = str(int(time.time()))
    sig = hmac.new(SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return {
        "X-Signature": sig,
        "X-Timestamp": ts,
        "X-Delivery-Id": delivery_id or str(uuid.uuid4()),
        "Content-Type": "application/json",
    }


def _msg_body(content: str, account_id: str = CHATWOOT_ACCOUNT_ID) -> bytes:
    return json.dumps(
        {
            "event": "message_created",
            "id": 42,
            "content": content,
            "message_type": "incoming",
            "conversation": {"id": 7, "inbox_id": 3, "status": "open"},
            "account": {"id": int(account_id)},
        }
    ).encode()


def _inbox_count(delivery_id: str) -> int:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM inbox_events WHERE delivery_id = :d"),
            {"d": delivery_id},
        ).scalar()
    admin.dispose()
    return int(n or 0)


def test_happy_path_persists_minimized_event(client: TestClient) -> None:
    body = _msg_body("CUSTOMER SECRET CONTENT SHOULD NOT PERSIST")
    delivery = str(uuid.uuid4())
    resp = client.post(
        "/v1/webhooks/chatwoot", content=body, headers=_signed_headers(body, delivery)
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "received"
    assert _inbox_count(delivery) == 1

    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        row = conn.execute(
            text("SELECT minimized_payload, payload_hash FROM inbox_events WHERE delivery_id = :d"),
            {"d": delivery},
        ).one()
    admin.dispose()
    payload_str = json.dumps(row.minimized_payload)
    assert "CUSTOMER SECRET CONTENT" not in payload_str  # content never persisted
    assert row.minimized_payload["conversation_id"] == "7"
    assert row.payload_hash == hashlib.sha256(body).hexdigest()


def test_duplicate_delivery_returns_success_without_new_row(client: TestClient) -> None:
    body = _msg_body("duplicate check")
    delivery = str(uuid.uuid4())
    headers = _signed_headers(body, delivery)

    first = client.post("/v1/webhooks/chatwoot", content=body, headers=headers)
    assert first.status_code == 202
    second = client.post("/v1/webhooks/chatwoot", content=body, headers=headers)
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert _inbox_count(delivery) == 1  # exactly one row


def test_bad_signature_rejected_and_not_persisted(client: TestClient) -> None:
    body = _msg_body("evil")
    delivery = str(uuid.uuid4())
    headers = _signed_headers(body, delivery)
    headers["X-Signature"] = "0" * 64

    resp = client.post("/v1/webhooks/chatwoot", content=body, headers=headers)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "WEBHOOK_SIGNATURE_INVALID"
    assert _inbox_count(delivery) == 0


def test_expired_timestamp_rejected(client: TestClient) -> None:
    body = _msg_body("replay")
    delivery = str(uuid.uuid4())
    old_ts = str(int(time.time()) - 3600)
    sig = hmac.new(SECRET.encode(), f"{old_ts}.".encode() + body, hashlib.sha256).hexdigest()

    resp = client.post(
        "/v1/webhooks/chatwoot",
        content=body,
        headers={
            "X-Signature": sig,
            "X-Timestamp": old_ts,
            "X-Delivery-Id": delivery,
        },
    )
    assert resp.status_code == 401
    assert _inbox_count(delivery) == 0


def test_unregistered_account_not_persisted(client: TestClient) -> None:
    body = _msg_body("who is this?", account_id="999999")
    delivery = str(uuid.uuid4())
    resp = client.post(
        "/v1/webhooks/chatwoot", content=body, headers=_signed_headers(body, delivery)
    )
    assert resp.status_code == 202
    assert resp.json()["error"]["code"] == "WEBHOOK_TENANT_UNRESOLVED"
    assert _inbox_count(delivery) == 0


def test_missing_delivery_id_rejected(client: TestClient) -> None:
    body = _msg_body("no delivery id")
    headers = _signed_headers(body)
    del headers["X-Delivery-Id"]
    resp = client.post("/v1/webhooks/chatwoot", content=body, headers=headers)
    assert resp.status_code == 400
