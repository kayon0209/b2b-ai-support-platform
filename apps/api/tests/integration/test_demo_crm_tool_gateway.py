"""End-to-end local/test CRM opportunity simulation through Tool Gateway."""

from __future__ import annotations

import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

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

TOOL_NAME = "crm.create_opportunity"
USER_ID = uuid.uuid5(uuid.NAMESPACE_URL, "codex-r2r3-demo-crm-actor")


class _Resolver:
    def __init__(self, tenant_id: uuid.UUID, role: str) -> None:
        self.tenant_id = tenant_id
        self.role = role

    async def __call__(self, request: object) -> TenantContext:
        del request
        return TenantContext(
            tenant_id=self.tenant_id,
            actor_id=USER_ID,
            actor_kind="user",
            role=self.role,
        )


def _client(tenant_id: uuid.UUID, role: str) -> TestClient:
    from platform_core.main import app

    fresh = FastAPI()
    for route in app.router.routes:
        fresh.router.routes.append(route)
    fresh.add_middleware(
        TenantContextMiddleware,
        resolver=_Resolver(tenant_id, role),
    )
    return TestClient(fresh, raise_server_exceptions=False)


def _headers(idempotency_key: str | None = None) -> dict[str, str]:
    result = {"Authorization": "Bearer demo-crm-test"}
    if idempotency_key is not None:
        result["Idempotency-Key"] = idempotency_key
    return result


def _seed(tenant_id: uuid.UUID) -> None:
    schema = {
        "type": "object",
        "properties": {
            "account_ref": {"type": "string", "minLength": 1, "maxLength": 255},
            "product_ref": {"type": "string", "minLength": 1, "maxLength": 255},
        },
        "required": ["account_ref", "product_ref"],
        "additionalProperties": False,
    }
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) "
                "VALUES (:id, :slug, 'Synthetic CRM', 'active')"
            ),
            {"id": tenant_id, "slug": f"demo-crm-{tenant_id.hex[:12]}"},
        )
        conn.execute(
            text(
                "INSERT INTO tool_definitions "
                "(id, tenant_id, name, version, risk, input_schema, output_schema, "
                "required_permissions, timeout_ms, idempotent, requires_confirmation) "
                "VALUES (:id, :tenant, :name, 1, 'confirmed_write', CAST(:schema AS jsonb), "
                "'{}'::jsonb, '[\"tool.write.confirmed\"]'::jsonb, 10000, true, true)"
            ),
            {
                "id": uuid.uuid4(),
                "tenant": tenant_id,
                "name": TOOL_NAME,
                "schema": json.dumps(schema),
            },
        )
        conn.execute(
            text(
                "INSERT INTO connectors "
                "(id, tenant_id, provider, name, status, capabilities, configuration) "
                "VALUES (:id, :tenant, 'demo_crm', 'Synthetic CRM', 'active', "
                '\'["opportunity_create"]\'::jsonb, \'{"mode":"synthetic"}\'::jsonb)'
            ),
            {"id": uuid.uuid4(), "tenant": tenant_id},
        )
    admin.dispose()


def _cleanup(tenant_id: uuid.UUID) -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "DELETE FROM tool_executions WHERE tenant_id = :tenant AND proposal_id IN "
                "(SELECT id FROM tool_proposals WHERE tenant_id = :tenant)"
            ),
            {"tenant": tenant_id},
        )
        conn.execute(
            text(
                "DELETE FROM action_confirmations WHERE tenant_id = :tenant AND proposal_id IN "
                "(SELECT id FROM tool_proposals WHERE tenant_id = :tenant)"
            ),
            {"tenant": tenant_id},
        )
        conn.execute(
            text("DELETE FROM tool_proposals WHERE tenant_id = :tenant"), {"tenant": tenant_id}
        )
        conn.execute(
            text("DELETE FROM tool_definitions WHERE tenant_id = :tenant"), {"tenant": tenant_id}
        )
        conn.execute(
            text("DELETE FROM connectors WHERE tenant_id = :tenant"), {"tenant": tenant_id}
        )
        conn.execute(
            text("DELETE FROM audit_events WHERE tenant_id = :tenant"), {"tenant": tenant_id}
        )
        conn.execute(text("DELETE FROM tenants WHERE id = :tenant"), {"tenant": tenant_id})
    admin.dispose()


def test_demo_crm_opportunity_uses_gateway_confirmation_idempotency_and_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from platform_core.config import get_settings
    from platform_core.integrations.demo_crm import _reset_demo_crm_store_for_tests

    monkeypatch.setenv("APP_ENVIRONMENT", "test")
    monkeypatch.setenv("APP_BUSINESS_API_ADAPTER", "demo")
    monkeypatch.setenv("APP_ALLOW_BOOTSTRAP_TOKENS", "true")
    get_settings.cache_clear()
    tenant_id = uuid.uuid4()
    _seed(tenant_id)
    client = _client(tenant_id, "support_admin")
    viewer = _client(tenant_id, "support_viewer")
    arguments = {"account_ref": "acme", "product_ref": "PCB-DEMO-100"}
    try:
        denied = viewer.post(
            "/v1/tool-proposals",
            headers=_headers("demo-crm-viewer-denied"),
            json={"tool_name": TOOL_NAME, "arguments": arguments},
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "POLICY_DENIED"

        barrier = Barrier(2)

        def propose_same_action():
            barrier.wait(timeout=5)
            return client.post(
                "/v1/tool-proposals",
                headers=_headers("demo-crm-proposal-1"),
                json={"tool_name": TOOL_NAME, "arguments": arguments},
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            proposed_results = list(pool.map(lambda _: propose_same_action(), range(2)))
        assert all(response.status_code == 200 for response in proposed_results), [
            response.text for response in proposed_results
        ]
        proposal_ids = {response.json()["proposal"]["proposal_id"] for response in proposed_results}
        assert len(proposal_ids) == 1
        proposal_id = next(iter(proposal_ids))
        assert sorted(response.json()["replayed"] for response in proposed_results) == [False, True]
        assert proposed_results[0].json()["proposal"]["status"] == "authorized"
        assert proposed_results[0].json()["proposal"]["required_confirmation"] is True

        conflict = client.post(
            "/v1/tool-proposals",
            headers=_headers("demo-crm-proposal-1"),
            json={
                "tool_name": TOOL_NAME,
                "arguments": {"account_ref": "acme", "product_ref": "PCBA-DEMO-200"},
            },
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

        premature = client.post(
            f"/v1/tool-proposals/{proposal_id}/execute",
            headers=_headers("demo-crm-execute-early"),
            json={},
        )
        assert premature.status_code == 409
        assert premature.json()["error"]["code"] == "CONFIRMATION_REQUIRED"

        confirmed = client.post(
            f"/v1/tool-proposals/{proposal_id}/confirm",
            headers=_headers(),
            json={},
        )
        assert confirmed.status_code == 200, confirmed.text

        executed = client.post(
            f"/v1/tool-proposals/{proposal_id}/execute",
            headers=_headers("demo-crm-execute-1"),
            json={},
        )
        assert executed.status_code == 200, executed.text
        body = executed.json()
        assert body["execution"]["status"] == "executed"
        assert body["execution"]["verification_status"] == "verified"
        assert body["proposal"]["status"] == "verified"
        receipt = body["execution"]["output"]
        assert receipt["source"] == "demo"
        assert receipt["synthetic"] is True
        assert receipt["customer_contacted"] is False
        assert receipt["account_ref"] == "acme"
        assert receipt["product_ref"] == "PCB-DEMO-100"
        assert "amount" not in receipt

        replayed_execution = client.post(
            f"/v1/tool-proposals/{proposal_id}/execute",
            headers=_headers("demo-crm-execute-1"),
            json={},
        )
        assert replayed_execution.status_code == 200, replayed_execution.text
        assert (
            replayed_execution.json()["execution"]["execution_id"]
            == body["execution"]["execution_id"]
        )
        assert (
            replayed_execution.json()["execution"]["output"]["opportunity_ref"]
            == receipt["opportunity_ref"]
        )
    finally:
        client.close()
        viewer.close()
        _reset_demo_crm_store_for_tests()
        _cleanup(tenant_id)
        get_settings.cache_clear()
