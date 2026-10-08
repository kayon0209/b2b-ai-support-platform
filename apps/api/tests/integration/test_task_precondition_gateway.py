"""A conditional task cannot reach its write executor on a stale condition."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from types import SimpleNamespace

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
TENANT = uuid.UUID("0190f000-0000-7000-8000-000000000901")
CONVERSATION = uuid.UUID("0190f000-0000-7000-8000-000000000902")
TASK = uuid.UUID("0190f000-0000-7000-8000-000000000903")
PARENT = uuid.UUID("0190f000-0000-7000-8000-000000000904")
PROPOSAL = uuid.UUID("0190f000-0000-7000-8000-000000000905")
TOOL_DEFINITION = uuid.UUID("0190f000-0000-7000-8000-000000000906")
CONFIRMATION = uuid.UUID("0190f000-0000-7000-8000-000000000907")
PARENT_EXECUTION = uuid.UUID("0190f000-0000-7000-8000-000000000908")
LIVE_EXECUTION = uuid.UUID("0190f000-0000-7000-8000-000000000909")
LIVE_READ_PROPOSAL = uuid.UUID("0190f000-0000-7000-8000-000000000910")
ACTOR = uuid.uuid5(uuid.NAMESPACE_URL, "phase1-task-precondition-agent")
TOOL_NAME = "jira.create_issue"


class _Resolver:
    async def __call__(self, request: object) -> TenantContext:
        return TenantContext(
            tenant_id=TENANT,
            actor_id=ACTOR,
            actor_kind="user",
            role="support_admin",
        )


def _client() -> TestClient:
    import importlib

    main_mod = importlib.import_module("platform_core.main")
    fresh = FastAPI()
    for route in main_mod.app.router.routes:
        fresh.router.routes.append(route)
    fresh.add_middleware(TenantContextMiddleware, resolver=_Resolver())
    return TestClient(fresh, raise_server_exceptions=False)


def _seed() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) "
                "VALUES (:id, 'phase1-precondition', 'Precondition test', 'active') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": TENANT},
        )
        conn.execute(
            text(
                "INSERT INTO users (id, primary_email, display_name, is_service_account) "
                "VALUES (:id, 'phase1-task-precondition@test.invalid', "
                "'Precondition actor', false) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": ACTOR},
        )
        conn.execute(
            text(
                "INSERT INTO memberships (id, tenant_id, user_id, role, status) "
                "VALUES (gen_random_uuid(), :tenant, :actor, 'support_admin', 'active') "
                "ON CONFLICT (tenant_id, user_id) DO UPDATE "
                "SET role = 'support_admin', status = 'active'"
            ),
            {"tenant": TENANT, "actor": ACTOR},
        )
        conn.execute(
            text(
                "INSERT INTO conversation_control_leases "
                "(id, tenant_id, conversation_ref_id, owner_type, owner_ref, mode, "
                "lease_version, changed_reason, updated_at) "
                "VALUES (:id, :tenant, :conversation, 'human', :actor, 'HUMAN_ACTIVE', "
                "1, 'test', 0) ON CONFLICT (tenant_id, conversation_ref_id) DO NOTHING"
            ),
            {
                "id": uuid.uuid4(),
                "tenant": TENANT,
                "conversation": CONVERSATION,
                "actor": str(ACTOR),
            },
        )
        schema = {
            "type": "object",
            "properties": {"project": {"type": "string"}, "summary": {"type": "string"}},
            "required": ["project", "summary"],
            "additionalProperties": False,
        }
        conn.execute(
            text(
                "INSERT INTO tool_definitions (id, tenant_id, name, version, risk, input_schema, "
                "output_schema, required_permissions, timeout_ms, idempotent, "
                "requires_confirmation) "
                "VALUES (:id, :tenant, :name, 1, 'confirmed_write', CAST(:schema AS jsonb), "
                "'{}'::jsonb, CAST(:permissions AS jsonb), 5000, true, true) "
                "ON CONFLICT (tenant_id, name, version) DO NOTHING"
            ),
            {
                "id": TOOL_DEFINITION,
                "tenant": TENANT,
                "name": TOOL_NAME,
                "schema": json.dumps(schema),
                "permissions": json.dumps(["tool.write.confirmed"]),
            },
        )
        conn.execute(
            text(
                "INSERT INTO conversation_tasks (id, tenant_id, conversation_ref_id, "
                "source_turn_id, task_local_key, sequence, kind, status, version, action_revision, "
                "content_hash, depends_on, condition, slots, missing_slots, proposal_id, "
                "proposal_lease_version, execution_id, "
                "completion_evidence, created_at, updated_at) "
                "VALUES (:id, :tenant, :conversation, 't-guard', 'read-0', 0, 'read', "
                "'succeeded', 2, 1, :hash, '[]', NULL, CAST(:slots AS jsonb), '[]', NULL, "
                "NULL, :parent_execution, :parent_evidence, 0, 0), "
                "(:child, :tenant, :conversation, 't-guard', 'write-1', 1, 'write', "
                "'awaiting_confirmation', 2, 2, :hash, '[\"read-0\"]', "
                "CAST(:condition AS jsonb), "
                "CAST(:child_slots AS jsonb), '[]', :proposal, 1, NULL, NULL, 0, 0)"
            ),
            {
                "id": PARENT,
                "child": TASK,
                "tenant": TENANT,
                "conversation": CONVERSATION,
                "hash": "0" * 64,
                "parent_execution": PARENT_EXECUTION,
                "parent_evidence": f"tool_receipt:{PARENT_EXECUTION}",
                "condition": json.dumps(
                    {"field": "order.status", "operator": "ne", "value": "shipped"}
                ),
                "slots": '[{"name":"order_id","origin":"customer_stated",'
                '"confirmed":true,"value":"SO-12345"}]',
                "child_slots": (
                    '[{"name":"tool","origin":"server_capability",'
                    '"selection_source":"allowlisted_candidate_schema_match",'
                    f'"confirmed":false,"value":"{TOOL_NAME}"}}]'
                ),
                "proposal": PROPOSAL,
            },
        )
        conn.execute(
            text(
                "INSERT INTO tool_proposals (id, tenant_id, tool_definition_id, action_hash, "
                "actor_id, "
                "sanitized_input, status, permission_decision, permission_reason, "
                "required_confirmation, expires_at, idempotency_key) "
                "VALUES (:id, :tenant, :tool, 'action-hash', :actor, CAST(:input AS jsonb), "
                "'confirmed', 'allowed', 'OK', true, :expires, 'phase1-precondition-write')"
            ),
            {
                "id": PROPOSAL,
                "tenant": TENANT,
                "tool": TOOL_DEFINITION,
                "actor": ACTOR,
                "input": json.dumps({"project": "PCB", "summary": "short"}),
                "expires": int(time.time()) + 600,
            },
        )
        conn.execute(
            text(
                "INSERT INTO action_confirmations (id, tenant_id, proposal_id, actor_id, "
                "action_hash, "
                "scope, expires_at, confirmed_at) VALUES (:id, :tenant, :proposal, :actor, "
                "'action-hash', 'single_execution', :expires, :now)"
            ),
            {
                "id": CONFIRMATION,
                "tenant": TENANT,
                "proposal": PROPOSAL,
                "actor": ACTOR,
                "expires": int(time.time()) + 600,
                "now": int(time.time()),
            },
        )
    admin.dispose()


def _clear() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(text("DELETE FROM memberships WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM users WHERE id = :actor"), {"actor": ACTOR})
        conn.execute(text("DELETE FROM action_confirmations WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM tool_executions WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM tool_proposals WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(
            text("DELETE FROM conversation_task_events WHERE tenant_id = :t"), {"t": TENANT}
        )
        conn.execute(text("DELETE FROM conversation_tasks WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM audit_events WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(
            text("DELETE FROM conversation_control_leases WHERE tenant_id = :t"), {"t": TENANT}
        )
        conn.execute(
            text("DELETE FROM tool_definitions WHERE tenant_id = :t AND name = :name"),
            {"t": TENANT, "name": TOOL_NAME},
        )
        conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})
    admin.dispose()


@pytest.fixture(autouse=True)
def _clean():
    _clear()
    _seed()
    yield
    _clear()


def test_gateway_runs_the_condition_guard_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A confirmed proposal still cannot bypass a failed live precondition."""
    from platform_core.agent_runtime.tasks import dependencies
    from platform_core.tool_gateway import router as gateway_router

    calls = {"guard": 0, "write": 0}

    async def block_write(*_args: object, **_kwargs: object) -> str:
        calls["guard"] += 1
        return "TASK_CONDITION_UNMET"

    class _Executor:
        async def execute(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            calls["write"] += 1
            return {"created": True}

        async def verify_postcondition(self, *_args: object, **_kwargs: object) -> bool:
            return True

    monkeypatch.setattr(dependencies, "check_live_dependent_write_precondition", block_write)
    monkeypatch.setattr(
        gateway_router,
        "resolve_executors",
        lambda *_args, **_kwargs: _async_executors({TOOL_NAME: _Executor()}),
    )

    response = _client().post(
        f"/v1/tool-proposals/{PROPOSAL}/execute",
        headers={"Authorization": "Bearer pt_bootstrap_test", "Idempotency-Key": "write-check-1"},
        json={"reason": "confirmed"},
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "TASK_DEPENDENCY_BLOCKED"
    assert calls == {"guard": 1, "write": 0}
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        executions = conn.execute(
            text("SELECT count(*) FROM tool_executions WHERE tenant_id = :t AND proposal_id = :p"),
            {"t": TENANT, "p": PROPOSAL},
        ).scalar_one()
    admin.dispose()
    assert executions == 0


async def _async_executors(executors: dict[str, object]) -> dict[str, object]:
    return executors


@pytest.mark.parametrize(
    ("live_status", "expected_blocker"),
    [("shipped", "TASK_CONDITION_UNMET"), ("in_production", None)],
)
def test_precondition_uses_a_fresh_verified_order_read(
    monkeypatch: pytest.MonkeyPatch,
    live_status: str,
    expected_blocker: str | None,
) -> None:
    from platform_core.agent_runtime.tasks import dependencies
    from platform_core.identity.tenant_context import tenant_session

    async def historical_proof(*_args: object, **_kwargs: object) -> None:
        return None

    async def account_for_conversation(*_args: object, **_kwargs: object) -> uuid.UUID:
        return uuid.uuid5(uuid.NAMESPACE_URL, "phase1-precondition-account")

    async def account_system_ref(*_args: object, **_kwargs: object) -> str:
        return "account-demo-1"

    async def connector_capabilities(*_args: object, **_kwargs: object) -> set[str]:
        return {"orders_read"}

    async def resolve_read_executor(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"order.get_status": object()}

    async def load_read(*_args: object, execution_id: uuid.UUID, **_kwargs: object):
        status = "submitted" if execution_id == PARENT_EXECUTION else live_status
        return SimpleNamespace(
            tool_name="order.get_status",
            sanitized_input={"order_id": "SO-12345"},
            sanitized_output={
                "found": True,
                "resource": "orders",
                "order_id": "SO-12345",
                "account": "account-demo-1",
                "status": status,
            },
        )

    class _ReadGateway:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def proposal_id_for_idempotency_key(self, **_kwargs: object) -> None:
            return None

        async def propose_id(self, **_kwargs: object) -> uuid.UUID:
            return LIVE_READ_PROPOSAL

        async def execute_receipt(self, **_kwargs: object):
            return SimpleNamespace(
                id=LIVE_EXECUTION,
                status="executed",
                verification_status="verified",
            )

        async def withdraw(self, **_kwargs: object) -> None:
            return None

    monkeypatch.setattr(dependencies, "dependency_block_reason", historical_proof)
    monkeypatch.setattr(
        "platform_core.cases.service.verified_account_for_conversation",
        account_for_conversation,
    )
    monkeypatch.setattr(
        "platform_core.identity.profile.business_system_ref_for_account", account_system_ref
    )
    monkeypatch.setattr(
        "platform_core.integrations.readiness.active_connector_capabilities",
        connector_capabilities,
    )
    monkeypatch.setattr(
        "platform_core.tool_gateway.registry.resolve_executors", resolve_read_executor
    )
    monkeypatch.setattr(
        "platform_core.tool_gateway.gateway.load_verified_read_execution", load_read
    )
    monkeypatch.setattr("platform_core.tool_gateway.gateway.ToolGateway", _ReadGateway)

    async def _run_check() -> str | None:
        from platform_core.identity.tenant_context import TenantContext

        ctx = TenantContext(
            tenant_id=TENANT,
            actor_id=ACTOR,
            actor_kind="user",
            role="support_admin",
        )
        async with tenant_session(ctx) as session:
            return await dependencies.check_live_dependent_write_precondition(
                session,
                tenant_context=ctx,
                write_proposal_id=PROPOSAL,
                write_tool_name=TOOL_NAME,
                request_idempotency_key="outer-write-attempt",
                trace_id="precondition-test",
            )

    blocker = asyncio.run(_run_check(), loop_factory=asyncio.SelectorEventLoop)
    assert blocker == expected_blocker

    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        reason = conn.execute(
            text(
                "SELECT reason_code FROM audit_events WHERE tenant_id = :tenant "
                "AND action = 'conversation.task_precondition_rechecked' "
                "AND resource_id = :task ORDER BY occurred_at DESC LIMIT 1"
            ),
            {"tenant": TENANT, "task": TASK},
        ).scalar_one()
        task_state = conn.execute(
            text("SELECT status, blocked_reason FROM conversation_tasks WHERE id = :task"),
            {"task": TASK},
        ).one()
    admin.dispose()
    assert reason == (expected_blocker or "TASK_PRECONDITION_SATISFIED")
    if expected_blocker == "TASK_CONDITION_UNMET":
        assert task_state == ("cancelled", "TASK_CONDITION_UNMET")
