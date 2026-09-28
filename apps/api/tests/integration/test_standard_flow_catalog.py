"""R2-02 tenant-specific readiness for the standard flow catalog endpoint."""

from __future__ import annotations

import json
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


class _RoleResolver:
    def __init__(self, tenant_id: uuid.UUID, role: str = "support_admin") -> None:
        self.tenant_id = tenant_id
        self.role = role
        self.actor_id = uuid.uuid5(uuid.NAMESPACE_URL, f"standard-flow:{tenant_id}")

    async def __call__(self, _request: object) -> TenantContext:
        return TenantContext(
            tenant_id=self.tenant_id,
            actor_id=self.actor_id,
            actor_kind="user",
            role=self.role,
        )


def _client(tenant_id: uuid.UUID, role: str = "support_admin") -> TestClient:
    import platform_core.main as main

    app = FastAPI()
    for route in main.app.router.routes:
        app.router.routes.append(route)
    app.add_middleware(TenantContextMiddleware, resolver=_RoleResolver(tenant_id, role))
    return TestClient(app, raise_server_exceptions=False)


def _seed_tool(
    conn,
    *,
    tenant_id: uuid.UUID,
    name: str,
    risk: str,
    permissions: list[str],
    schema: dict[str, object],
) -> None:
    conn.execute(
        text(
            "INSERT INTO tool_definitions "
            "(id, tenant_id, name, version, risk, input_schema, output_schema, "
            "required_permissions, timeout_ms, idempotent, requires_confirmation) "
            "VALUES (:id, :tenant, :name, 1, :risk, CAST(:input AS jsonb), '{}'::jsonb, "
            "CAST(:permissions AS jsonb), 10000, true, :confirm)"
        ),
        {
            "id": uuid.uuid4(),
            "tenant": tenant_id,
            "name": name,
            "risk": risk,
            "input": json.dumps(schema),
            "permissions": json.dumps(permissions),
            "confirm": risk == "confirmed_write",
        },
    )


def test_flow_catalog_uses_active_tenant_connectors_and_staffed_owner_groups() -> None:
    tenant_id = uuid.uuid4()
    other_tenant_id = uuid.uuid4()
    actor_id = uuid.uuid5(uuid.NAMESPACE_URL, f"standard-flow:{tenant_id}")
    department_id = uuid.uuid4()
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid in (tenant_id, other_tenant_id):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) "
                    "VALUES (:id, :slug, :slug, 'active')"
                ),
                {"id": tid, "slug": f"flow-catalog-{tid}"},
            )
        conn.execute(
            text(
                "INSERT INTO users (id, primary_email, display_name, is_service_account) "
                "VALUES (:id, :email, 'Quality Lead', false)"
            ),
            {"id": actor_id, "email": f"flow-{tenant_id}@example.test"},
        )
        conn.execute(
            text(
                "INSERT INTO departments (id, tenant_id, name, slug, created_at, updated_at) "
                "VALUES (:id, :tenant, 'Quality', 'quality', 1, 1)"
            ),
            {"id": department_id, "tenant": tenant_id},
        )
        conn.execute(
            text(
                "INSERT INTO memberships (id, tenant_id, user_id, role, status, department_id) "
                "VALUES (:id, :tenant, :user, 'support_admin', 'active', :department)"
            ),
            {
                "id": uuid.uuid4(),
                "tenant": tenant_id,
                "user": actor_id,
                "department": department_id,
            },
        )
        conn.execute(
            text(
                "INSERT INTO connectors "
                "(id, tenant_id, provider, name, status, capabilities, configuration) "
                "VALUES (:id, :tenant, 'business_api', 'Orders API', 'active', "
                "CAST(:capabilities AS jsonb), '{}'::jsonb)"
            ),
            {
                "id": uuid.uuid4(),
                "tenant": tenant_id,
                "capabilities": json.dumps(["orders_read"]),
            },
        )
        _seed_tool(
            conn,
            tenant_id=tenant_id,
            name="order.get_status",
            risk="read",
            permissions=["tool.read"],
            schema={
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
                "additionalProperties": False,
            },
        )
        _seed_tool(
            conn,
            tenant_id=tenant_id,
            name="case.create",
            risk="confirmed_write",
            permissions=["tool.write.confirmed"],
            schema={
                "type": "object",
                "properties": {
                    "enterprise_account_id": {"type": "string"},
                    "subject": {"type": "string"},
                },
                "required": ["enterprise_account_id", "subject"],
                "additionalProperties": False,
            },
        )

    try:
        response = _client(tenant_id).get(
            "/v1/workbench/standard-flows",
            headers={"Authorization": "Bearer pt_bootstrap_test"},
        )
        assert response.status_code == 200, response.text[:300]
        body = response.json()
        flows = {item["template"]["key"]: item for item in body["items"]}
        assert body["execution_requires_tool_gateway"] is True
        assert body["instances_enabled"] is False
        assert flows["order_status"]["availability"]["status"] == "available"
        assert flows["order_status"]["availability"]["optional_unavailable_tools"] == [
            "shipment.track"
        ]
        assert flows["repair_quality_intake"]["availability"]["status"] == "available"
        assert flows["invoice_application"]["availability"]["status"] == "needs_human"
        assert (
            flows["invoice_application"]["availability"]["reason_code"]
            == "FLOW_EXTERNAL_WRITE_UNAVAILABLE"
        )
        assert flows["technical_escalation"]["availability"]["reason_code"] == (
            "FLOW_OWNER_UNASSIGNED"
        )

        agent_response = _client(tenant_id, "support_agent").get(
            "/v1/workbench/standard-flows",
            headers={"Authorization": "Bearer pt_bootstrap_test"},
        )
        assert agent_response.status_code == 200, agent_response.text[:300]
        agent_flows = {item["template"]["key"]: item for item in agent_response.json()["items"]}
        assert agent_flows["order_status"]["availability"]["status"] == "available"
        assert agent_flows["repair_quality_intake"]["availability"]["reason_code"] == (
            "FLOW_CAPABILITY_MISSING"
        )

        other = _client(other_tenant_id).get(
            "/v1/workbench/standard-flows",
            headers={"Authorization": "Bearer pt_bootstrap_test"},
        )
        assert other.status_code == 200, other.text[:300]
        other_flows = {item["template"]["key"]: item for item in other.json()["items"]}
        assert other_flows["order_status"]["availability"]["status"] == "needs_human"
        assert other_flows["order_status"]["availability"]["unavailable_tools"] == [
            "order.get_status"
        ]
        assert other_flows["order_status"]["availability"][
            "unavailable_connector_capabilities"
        ] == ["orders_read"]
    finally:
        with admin.begin() as conn:
            conn.execute(text("DELETE FROM memberships WHERE tenant_id = :t"), {"t": tenant_id})
            conn.execute(text("DELETE FROM connectors WHERE tenant_id = :t"), {"t": tenant_id})
            conn.execute(
                text("DELETE FROM tool_definitions WHERE tenant_id = :t"), {"t": tenant_id}
            )
            conn.execute(text("DELETE FROM departments WHERE tenant_id = :t"), {"t": tenant_id})
            conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
            conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": other_tenant_id})
            conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": actor_id})
        admin.dispose()
