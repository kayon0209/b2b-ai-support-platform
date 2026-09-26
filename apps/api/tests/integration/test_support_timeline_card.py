"""The visitor timeline, and the card that rides on a `tool` turn.

Why this file exists
--------------------
`/v1/support/timeline` had no test at all, and the card field is new. The two
things worth pinning are both "silently wrong" cases:

1. A receipt is stored as JSON in the turn's `text`. Handing that to a chat
   surface unchanged is what put raw JSON in a customer's bubble, and nothing
   in the suite would have noticed because the endpoint still answered 200.
2. The timeline is scoped by RLS, not by the conversation ref in the token. A
   test that only checks "the visitor sees their own conversation" passes even
   if the tenant binding is ignored, so the negative case here deliberately
   pairs a token for one tenant with another tenant's conversation ref.
"""

from __future__ import annotations

import json
import os
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from platform_core.support_bridge.conversation_ref import conversation_ref_for
from platform_core.support_bridge.visitor_token import issue

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)

TENANT_A = uuid.UUID("01900000-0000-7000-8000-0000000000e1")
TENANT_B = uuid.UUID("01900000-0000-7000-8000-0000000000e2")
SLUG_A = "support-card-a"
SLUG_B = "support-card-b"

EXTERNAL_A = "conv-card-a"
EXTERNAL_B = "conv-card-b"

RECEIPT = {
    "order_id": "SO-9001",
    "status": "in_production",
    "nodes": [
        {"label": "下单", "status": "done", "at": "2026-09-14T10:00:00Z"},
        {"label": "工程确认", "status": "done", "at": "2026-09-15T09:20:00Z"},
        {"label": "生产", "status": "active", "at": "2026-09-17T08:00:00Z"},
        {"label": "出货", "status": "pending", "at": None},
    ],
    "eta": "2026-09-26T00:00:00Z",
    "quantity": 500,
    "found": True,
    "resource": "orders",
    "source": "demo",
    "fetched_at": "2026-09-21T00:00:00Z",
    "tool": "order.get_status",
}

# A published receipt with no card shape. It must still be readable as `text`
# (the operator console reads the receipt itself) and must not produce a card.
OPAQUE_RECEIPT = {"tool": "crm.update_account", "found": True, "ok": True}


def _admin():
    return create_engine(ADMIN_URL)


def _cleanup() -> None:
    admin = _admin()
    with admin.begin() as conn:
        for table in ("conversation_turns",):
            conn.execute(
                text(
                    f"DELETE FROM {table} WHERE tenant_id IN "  # noqa: S608 - fixed literal names
                    "(SELECT id FROM tenants WHERE slug = ANY(:s))"
                ),
                {"s": [SLUG_A, SLUG_B]},
            )
        conn.execute(text("DELETE FROM tenants WHERE slug = ANY(:s)"), {"s": [SLUG_A, SLUG_B]})
    admin.dispose()


@pytest.fixture(scope="module", autouse=True)
def seed() -> None:
    _cleanup()
    now = int(time.time())
    ref_a = conversation_ref_for(TENANT_A, EXTERNAL_A)
    ref_b = conversation_ref_for(TENANT_B, EXTERNAL_B)

    admin = _admin()
    with admin.begin() as conn:
        for tid, slug in ((TENANT_A, SLUG_A), (TENANT_B, SLUG_B)):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, 'Support Card', 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug},
            )
        rows = [
            (TENANT_A, ref_a, "customer", "SO-9001 现在到哪一步了？", now - 300),
            (TENANT_A, ref_a, "tool", json.dumps(RECEIPT, ensure_ascii=False), now - 290),
            (TENANT_A, ref_a, "agent", "订单在生产中，预计 9 月 26 日出货。", now - 280),
            (TENANT_A, ref_a, "tool", json.dumps(OPAQUE_RECEIPT), now - 270),
            (TENANT_B, ref_b, "customer", "另一个租户的提问", now - 300),
        ]
        for tid, ref, role, body, ts in rows:
            conn.execute(
                text(
                    "INSERT INTO conversation_turns "
                    "(id, tenant_id, conversation_ref_id, role, text_redacted, text_hash, ts, "
                    " ref, source, created_at) VALUES "
                    "(gen_random_uuid(), :t, :c, :r, :b, :h, :ts, '', :s, :ts)"
                ),
                {
                    "t": tid,
                    "c": ref,
                    "r": role,
                    "b": body,
                    "h": str(uuid.uuid4()),
                    "ts": ts,
                    "s": "tool" if role == "tool" else "platform",
                },
            )
    admin.dispose()
    yield
    _cleanup()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("APP_ALLOW_BOOTSTRAP_TOKENS", "true")
    monkeypatch.delenv("APP_OIDC_ISSUER", raising=False)

    from platform_core.config import get_settings

    get_settings.cache_clear()

    from fastapi import FastAPI

    from platform_core.identity.middleware import TenantContextMiddleware, build_resolver
    from platform_core.main import app as main_app

    fresh = FastAPI()
    for route in main_app.router.routes:
        fresh.router.routes.append(route)
    fresh.add_middleware(TenantContextMiddleware, resolver=build_resolver())
    try:
        yield TestClient(fresh, raise_server_exceptions=False)
    finally:
        get_settings.cache_clear()


def _visitor(tenant_id: uuid.UUID, external: str, *, ttl: int = 3600) -> dict[str, str]:
    token, _expires = issue(
        tenant_id, conversation_ref_for(tenant_id, external), external, ttl_seconds=ttl
    )
    return {"Authorization": f"Bearer {token}"}


def _timeline(client: TestClient, headers: dict[str, str]) -> list[dict]:
    resp = client.get("/v1/support/timeline", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


# --- The card ---------------------------------------------------------------


def test_a_tool_turn_carries_a_card(client: TestClient) -> None:
    items = _timeline(client, _visitor(TENANT_A, EXTERNAL_A))
    assert [turn["role"] for turn in items] == ["customer", "tool", "agent", "tool"]

    card = items[1]["card"]
    assert card is not None
    assert card["kind"] == "order_status"
    assert card["title"] == "SO-9001"
    assert card["status"] == "in_production"
    assert [node["label"] for node in card["nodes"]] == ["下单", "工程确认", "生产", "出货"]
    assert card["nodes"][2]["state"] == "active"
    assert card["fetched_at"] is not None
    assert card["provenance"] == "demo"


def test_the_receipt_text_is_still_there(client: TestClient) -> None:
    """Additive, not a replacement: the operator console reads the receipt
    itself, and a surface that needs a field the card drops must still get it."""
    items = _timeline(client, _visitor(TENANT_A, EXTERNAL_A))
    assert json.loads(items[1]["text"])["order_id"] == "SO-9001"


def test_only_tool_turns_carry_a_card(client: TestClient) -> None:
    items = _timeline(client, _visitor(TENANT_A, EXTERNAL_A))
    for turn in items:
        if turn["role"] != "tool":
            assert turn["card"] is None, turn


def test_a_receipt_without_a_card_shape_has_no_card(client: TestClient) -> None:
    """And its text is still text - the surface falls back, it does not break."""
    items = _timeline(client, _visitor(TENANT_A, EXTERNAL_A))
    assert items[3]["card"] is None
    assert json.loads(items[3]["text"])["ok"] is True


# --- Authorization ----------------------------------------------------------


def test_a_visitor_sees_only_their_own_conversation(client: TestClient) -> None:
    items = _timeline(client, _visitor(TENANT_B, EXTERNAL_B))
    assert [turn["role"] for turn in items] == ["customer"]
    assert items[0]["text"] == "另一个租户的提问"


def test_a_token_cannot_reach_another_tenants_conversation(client: TestClient) -> None:
    """The negative case that a same-tenant test cannot express.

    The token is validly signed and names tenant B, but carries tenant A's
    conversation ref. If the read were scoped by the ref rather than by RLS,
    the visitor would get A's exchange. It must return nothing.
    """
    token, _expires = issue(
        TENANT_B, conversation_ref_for(TENANT_A, EXTERNAL_A), EXTERNAL_A, ttl_seconds=3600
    )
    resp = client.get("/v1/support/timeline", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []


@pytest.mark.parametrize(
    "token",
    [
        "",
        "vs_not-a-token",
        "not-a-prefix",
        "vs_AAAA.BBBB",
    ],
)
def test_a_token_that_does_not_verify_is_rejected(client: TestClient, token: str) -> None:
    resp = client.get("/v1/support/timeline", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401, resp.text


def test_an_expired_token_is_rejected(client: TestClient) -> None:
    expired, _expires = issue(
        TENANT_A,
        conversation_ref_for(TENANT_A, EXTERNAL_A),
        EXTERNAL_A,
        ttl_seconds=1,
        now=int(time.time()) - 3600,
    )
    resp = client.get("/v1/support/timeline", headers={"Authorization": f"Bearer {expired}"})
    assert resp.status_code == 401, resp.text
