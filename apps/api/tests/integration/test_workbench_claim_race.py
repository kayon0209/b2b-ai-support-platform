"""Two seats racing to claim one queue item must have exactly one winner."""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, text

from platform_core.identity.middleware import TenantContextMiddleware
from platform_core.identity.tenant_context import TenantContext

pytestmark = pytest.mark.integration

ADMIN_URL = "postgresql+psycopg://platform:platform@localhost:5435/platform"


class _AgentResolver:
    def __init__(self, tenant_id: uuid.UUID, actor_id: uuid.UUID) -> None:
        self.tenant_id = tenant_id
        self.actor_id = actor_id

    async def __call__(self, _request: object) -> TenantContext:
        return TenantContext(
            tenant_id=self.tenant_id,
            actor_id=self.actor_id,
            actor_kind="user",
            role="support_agent",
        )


def _app(tenant_id: uuid.UUID, actor_id: uuid.UUID) -> FastAPI:
    import platform_core.main as main

    app = FastAPI()
    for route in main.app.router.routes:
        app.router.routes.append(route)
    app.add_middleware(
        TenantContextMiddleware,
        resolver=_AgentResolver(tenant_id=tenant_id, actor_id=actor_id),
    )
    return app


def test_two_agents_cannot_claim_the_same_queue_version() -> None:
    tenant_id = uuid.uuid4()
    conversation_ref = uuid.uuid4()
    agent_ids = (uuid.uuid4(), uuid.uuid4())
    now = 1_797_000_000
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) "
                "VALUES (:id, :slug, 'Workbench Race', 'active')"
            ),
            {"id": tenant_id, "slug": f"workbench-race-{tenant_id}"},
        )
        for index, actor_id in enumerate(agent_ids, start=1):
            conn.execute(
                text(
                    "INSERT INTO agent_profiles "
                    "(id, tenant_id, user_ref, display_name, skills, max_concurrent, "
                    "status, created_at, updated_at) "
                    "VALUES (:id, :tenant, :user, :name, '[]'::jsonb, 5, 'active', :now, :now)"
                ),
                {
                    "id": uuid.uuid4(),
                    "tenant": tenant_id,
                    "user": str(actor_id),
                    "name": f"Race Agent {index}",
                    "now": now,
                },
            )
        conn.execute(
            text(
                "INSERT INTO conversation_control_leases "
                "(id, tenant_id, conversation_ref_id, owner_type, owner_ref, mode, "
                "lease_version, changed_reason, updated_at) "
                "VALUES (:id, :tenant, :conversation, 'queue', NULL, 'QUEUED_FOR_HUMAN', "
                "1, 'acceptance-race', :now)"
            ),
            {
                "id": uuid.uuid4(),
                "tenant": tenant_id,
                "conversation": conversation_ref,
                "now": now,
            },
        )

    async def race() -> list[tuple[int, dict[str, object]]]:
        ready = 0
        barrier = asyncio.Event()

        async def claim(client: httpx.AsyncClient) -> tuple[int, dict[str, object]]:
            nonlocal ready
            ready += 1
            if ready == 2:
                barrier.set()
            await barrier.wait()
            response = await client.post(
                f"/v1/workbench/conversations/{conversation_ref}/actions",
                headers={"Idempotency-Key": str(uuid.uuid4())},
                json={"operation": "claim", "expected_version": 1},
            )
            return response.status_code, response.json()

        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=_app(tenant_id, agent_ids[0])),
                base_url="http://agent-a.test",
            ) as agent_a,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=_app(tenant_id, agent_ids[1])),
                base_url="http://agent-b.test",
            ) as agent_b,
        ):
            return await asyncio.gather(claim(agent_a), claim(agent_b))

    try:
        outcomes = asyncio.run(race())
        assert sorted(status for status, _ in outcomes) == [200, 409]
        denied = next(payload for status, payload in outcomes if status == 409)
        assert denied["error"]["code"] == "LEASE_CONFLICT"

        with admin.connect() as conn:
            owner_type, owner_ref, version = conn.execute(
                text(
                    "SELECT owner_type, owner_ref, lease_version "
                    "FROM conversation_control_leases "
                    "WHERE tenant_id = :tenant AND conversation_ref_id = :conversation"
                ),
                {"tenant": tenant_id, "conversation": conversation_ref},
            ).one()
        assert owner_type == "human"
        assert owner_ref in {str(actor_id) for actor_id in agent_ids}
        assert version == 2
    finally:
        with admin.begin() as conn:
            conn.execute(
                text("DELETE FROM audit_events WHERE tenant_id = :tenant"),
                {"tenant": tenant_id},
            )
            conn.execute(
                text("DELETE FROM conversation_control_leases WHERE tenant_id = :tenant"),
                {"tenant": tenant_id},
            )
            conn.execute(
                text("DELETE FROM agent_profiles WHERE tenant_id = :tenant"),
                {"tenant": tenant_id},
            )
            conn.execute(text("DELETE FROM tenants WHERE id = :tenant"), {"tenant": tenant_id})
        admin.dispose()
