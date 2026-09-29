"""R2-01 workbench, authorization, idempotency, and tenant-bound advice."""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from platform_core.identity.middleware import TenantContextMiddleware
from platform_core.identity.tenant_context import TenantContext

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
FLAG = "agent.emotion_priority_advice"


class _RoleResolver:
    def __init__(self, tenant_id: uuid.UUID, role: str) -> None:
        self.tenant_id = tenant_id
        self.role = role
        self.actor_id = uuid.uuid5(uuid.NAMESPACE_URL, f"emotion-review:{tenant_id}:{role}")

    async def __call__(self, _request: object) -> TenantContext:
        return TenantContext(
            tenant_id=self.tenant_id,
            actor_id=self.actor_id,
            actor_kind="user",
            role=self.role,
        )


def _client(tenant_id: uuid.UUID, role: str) -> TestClient:
    import platform_core.main as main

    app = FastAPI()
    for route in main.app.router.routes:
        app.router.routes.append(route)
    app.add_middleware(
        TenantContextMiddleware,
        resolver=_RoleResolver(tenant_id, role),
    )
    return TestClient(app, raise_server_exceptions=False)


def _headers(key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": "Bearer pt_bootstrap_test"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


@pytest.fixture
def scenario():
    tenant_id = uuid.uuid4()
    other_tenant_id = uuid.uuid4()
    conversation_ref = uuid.uuid4()
    now = 1_797_000_000
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tenant in (tenant_id, other_tenant_id):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) "
                    "VALUES (:id, :slug, :slug, 'active')"
                ),
                {"id": tenant, "slug": f"emotion-{tenant}"},
            )
        conn.execute(
            text(
                "INSERT INTO conversation_control_leases "
                "(id, tenant_id, conversation_ref_id, owner_type, owner_ref, mode, "
                "lease_version, changed_reason, updated_at) VALUES "
                "(:id, :tenant, :conversation, 'queue', NULL, 'QUEUED_FOR_HUMAN', 1, 'test', :now)"
            ),
            {
                "id": uuid.uuid4(),
                "tenant": tenant_id,
                "conversation": conversation_ref,
                "now": now,
            },
        )
        conn.execute(
            text(
                "INSERT INTO conversation_turns "
                "(id, tenant_id, conversation_ref_id, role, text_redacted, text_hash, ts) "
                "VALUES (:id, :tenant, :conversation, 'customer', :body, 'emotion-seed', :now)"
            ),
            {
                "id": uuid.uuid4(),
                "tenant": tenant_id,
                "conversation": conversation_ref,
                "body": "订单迟迟没有进度，你们服务太差了",
                "now": now,
            },
        )
    try:
        yield {
            "tenant_id": tenant_id,
            "other_tenant_id": other_tenant_id,
            "conversation_ref": conversation_ref,
            "admin": admin,
        }
    finally:
        with admin.begin() as conn:
            for tenant in (tenant_id, other_tenant_id):
                conn.execute(text("DELETE FROM audit_events WHERE tenant_id = :t"), {"t": tenant})
                conn.execute(
                    text("DELETE FROM emotion_advice_reviews WHERE tenant_id = :t"), {"t": tenant}
                )
                conn.execute(text("DELETE FROM feature_flags WHERE tenant_id = :t"), {"t": tenant})
                conn.execute(
                    text("DELETE FROM conversation_control_leases WHERE tenant_id = :t"),
                    {"t": tenant},
                )
                conn.execute(
                    text("DELETE FROM conversation_turns WHERE tenant_id = :t"), {"t": tenant}
                )
                conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant})
        admin.dispose()


def _enable_advice(scenario) -> None:
    with scenario["admin"].begin() as conn:
        conn.execute(
            text(
                "INSERT INTO feature_flags "
                "(id, tenant_id, key, description, enabled, rollout_percent, created_at) "
                "VALUES (:id, :tenant, :key, 'R2-01 integration fixture', true, 100, 1)"
            ),
            {"id": uuid.uuid4(), "tenant": scenario["tenant_id"], "key": FLAG},
        )


def test_advice_is_off_by_default_and_tenant_scoped(scenario) -> None:
    ref = scenario["conversation_ref"]
    tenant = scenario["tenant_id"]
    owner = _client(tenant, "support_agent")

    detail = owner.get(f"/v1/workbench/conversations/{ref}", headers=_headers())
    assert detail.status_code == 200, detail.text[:300]
    assert detail.json()["emotion_advice"] is None

    sorted_queue = owner.get(
        "/v1/workbench/conversations?tab=queue&sort=emotion",
        headers=_headers(),
    )
    assert sorted_queue.status_code == 409
    assert sorted_queue.json()["error"]["code"] == "FEATURE_DISABLED"

    other = _client(scenario["other_tenant_id"], "support_agent")
    hidden = other.get(f"/v1/workbench/conversations/{ref}", headers=_headers())
    assert hidden.status_code == 404


def test_queue_advice_sort_explains_each_item_and_is_current_page_only(scenario) -> None:
    _enable_advice(scenario)
    with scenario["admin"].begin() as conn:
        conn.execute(
            text(
                "INSERT INTO conversation_control_leases "
                "(id, tenant_id, conversation_ref_id, owner_type, mode, lease_version, "
                "changed_reason, updated_at) VALUES (:id, :tenant, :conversation, 'queue', "
                "'QUEUED_FOR_HUMAN', 1, 'test', 2)"
            ),
            {
                "id": uuid.uuid4(),
                "tenant": scenario["tenant_id"],
                "conversation": uuid.uuid4(),
            },
        )
    response = _client(scenario["tenant_id"], "support_agent").get(
        "/v1/workbench/conversations?tab=queue&sort=emotion&limit=1&offset=0",
        headers=_headers(),
    )

    assert response.status_code == 200, response.text[:300]
    body = response.json()
    assert body["sort_mode"] == "emotion"
    assert body["sort_scope"] == "current_page"
    assert body["emotion_advice_enabled"] is True
    assert len(body["items"]) == 1
    assert body["items"][0]["emotion_advice"]["attention"] == "review"
    assert body["items"][0]["emotion_advice"]["reason_codes"]


def test_supervisor_correction_is_append_only_idempotent_and_stale_safe(scenario) -> None:
    _enable_advice(scenario)
    ref = scenario["conversation_ref"]
    tenant = scenario["tenant_id"]
    supervisor = _client(tenant, "support_admin")
    agent = _client(tenant, "support_agent")
    detail = supervisor.get(f"/v1/workbench/conversations/{ref}", headers=_headers())
    assert detail.status_code == 200, detail.text[:300]
    advice = detail.json()["emotion_advice"]
    assert advice["current_level"] == "angry"
    assert advice["evidence"]

    payload = {
        "advice_id": advice["advice_id"],
        "corrected_level": "frustrated",
        "reason_code": "overstated",
    }
    denied = agent.post(
        f"/v1/workbench/conversations/{ref}/emotion-advice/reviews",
        headers=_headers("agent-must-not-correct"),
        json=payload,
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "POLICY_DENIED"

    key = "supervisor-correction-1"
    first = supervisor.post(
        f"/v1/workbench/conversations/{ref}/emotion-advice/reviews",
        headers=_headers(key),
        json=payload,
    )
    assert first.status_code == 200, first.text[:300]
    assert first.json()["replayed"] is False

    # A lost-response retry remains idempotent even when a new message makes
    # the original advice revision stale.
    with scenario["admin"].begin() as conn:
        conn.execute(
            text(
                "INSERT INTO conversation_turns "
                "(id, tenant_id, conversation_ref_id, role, text_redacted, text_hash, ts) "
                "VALUES (:id, :tenant, :conversation, 'customer', '现在有新进度吗', 'later', 3)"
            ),
            {"id": uuid.uuid4(), "tenant": tenant, "conversation": ref},
        )
    replay = supervisor.post(
        f"/v1/workbench/conversations/{ref}/emotion-advice/reviews",
        headers=_headers(key),
        json=payload,
    )
    assert replay.status_code == 200, replay.text[:300]
    assert replay.json()["replayed"] is True

    changed_payload = {**payload, "corrected_level": "angry"}
    conflict = supervisor.post(
        f"/v1/workbench/conversations/{ref}/emotion-advice/reviews",
        headers=_headers(key),
        json=changed_payload,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    stale = supervisor.post(
        f"/v1/workbench/conversations/{ref}/emotion-advice/reviews",
        headers=_headers("supervisor-correction-stale"),
        json=payload,
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "EMOTION_ADVICE_STALE"

    with scenario["admin"].connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM emotion_advice_reviews WHERE tenant_id = :tenant"),
            {"tenant": tenant},
        ).scalar_one()
    assert count == 1
