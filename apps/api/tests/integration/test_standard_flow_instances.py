"""HTTP and PostgreSQL guarantees for operator-started standard flows."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
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
APP_URL = os.environ.get(
    "APP_TEST_DATABASE_URL",
    "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform",
)

TENANT = "0190f000-0000-7000-8000-000000000001"
OTHER_TENANT = "0190f000-0000-7000-8000-000000000002"
CONVERSATION = "0190f000-0000-7000-8000-000000000010"
OTHER_CONVERSATION = "0190f000-0000-7000-8000-000000000020"
CUSTOMER_TURN = "0190f000-0000-7000-8000-000000000030"
SLUG = "r2-standard-flow-instance"
OTHER_SLUG = "r2-standard-flow-instance-other"
LEASE_VERSION = 3
AGENT_REF = "r2-standard-flow-agent"
AGENT_ID = uuid.uuid5(uuid.NAMESPACE_URL, AGENT_REF)


class _Resolver:
    def __init__(self, tenant: str = TENANT, agent: str = AGENT_REF, role: str = "support_agent"):
        self.tenant = tenant
        self.agent = agent
        self.role = role

    async def __call__(self, request: object) -> TenantContext:
        return TenantContext(
            tenant_id=uuid.UUID(self.tenant),
            actor_id=uuid.uuid5(uuid.NAMESPACE_URL, self.agent),
            actor_kind="user",
            role=self.role,
        )


def _seed_actor(tenant: str, agent: str, role: str) -> uuid.UUID:
    """Persist the user and live membership used by a synthetic API actor."""
    actor_id = uuid.uuid5(uuid.NAMESPACE_URL, agent)
    email = f"r2flow-{actor_id.hex}@example.test"
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, primary_email, display_name, is_service_account) "
                "VALUES (:id, :email, :agent, false) ON CONFLICT (id) DO NOTHING"
            ),
            {"id": actor_id, "email": email, "agent": agent},
        )
        conn.execute(
            text(
                "INSERT INTO memberships (id, tenant_id, user_id, role, status) "
                "VALUES (gen_random_uuid(), :tenant, :actor, :role, 'active') "
                "ON CONFLICT (tenant_id, user_id) DO UPDATE "
                "SET role = EXCLUDED.role, status = 'active'"
            ),
            {"tenant": tenant, "actor": actor_id, "role": role},
        )
    admin.dispose()
    return actor_id


def _client(
    tenant: str = TENANT,
    *,
    agent: str = AGENT_REF,
    role: str = "support_agent",
) -> TestClient:
    import importlib

    _seed_actor(tenant, agent, role)
    main_mod = importlib.import_module("platform_core.main")
    fresh = FastAPI()
    for route in main_mod.app.router.routes:
        fresh.router.routes.append(route)
    fresh.add_middleware(
        TenantContextMiddleware,
        resolver=_Resolver(tenant=tenant, agent=agent, role=role),
    )
    return TestClient(fresh, raise_server_exceptions=False)


_RESTART_API_BOOTSTRAP = """
import sys
import uuid
import uvicorn
from fastapi import FastAPI
from platform_core.identity.middleware import TenantContextMiddleware
from platform_core.identity.tenant_context import TenantContext
from platform_core.main import app as source_app
import os

tenant_id, actor_id, role, port = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])

class Resolver:
    async def __call__(self, request):
        return TenantContext(
            tenant_id=uuid.UUID(tenant_id),
            actor_id=uuid.UUID(actor_id),
            actor_kind="user",
            role=role,
        )

app = FastAPI()
for route in source_app.router.routes:
    app.router.routes.append(route)
app.add_middleware(TenantContextMiddleware, resolver=Resolver())

provider_url = os.environ.get("PHASE2_TEST_FAKE_PROVIDER_URL")
if provider_url:
    import httpx
    from platform_core.tool_gateway import registry as registry_mod

    class ProcessCrashJira:
        def __init__(self, context):
            self.context = context

        async def execute(self, tool_name, parameters, idempotency_key):
            async with httpx.AsyncClient(timeout=3) as client:
                response = await client.post(
                    provider_url,
                    json={"tool": tool_name, "idempotency_key": idempotency_key},
                )
                response.raise_for_status()
            if os.environ.get("PHASE2_TEST_KILL_AFTER_PROVIDER") == "1":
                os._exit(91)
            return {"ok": True, "issue_key": "SUP-CRASH-1"}

        async def verify_postcondition(self, tool_name, parameters, output):
            return True

    original_factories = registry_mod.default_factories

    def patched_factories():
        factories = dict(original_factories())
        factories["jira"] = registry_mod.AdapterFactory(
            provider="jira", build=ProcessCrashJira
        )
        return factories

    registry_mod.default_factories = patched_factories

uvicorn.run(app, host="127.0.0.1", port=port, log_level="critical")
"""


class _FakeProviderHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.server.call_count += 1  # type: ignore[attr-defined]
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"accepted":true}')

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class _FakeProviderServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        self.call_count = 0
        super().__init__(("127.0.0.1", 0), _FakeProviderHandler)


def _start_restart_api(
    *,
    role: str,
    actor: str,
    environment_overrides: dict[str, str] | None = None,
) -> tuple[subprocess.Popen[bytes], str]:
    """Run the real API routes in a disposable OS process for restart acceptance."""
    actor_id = _seed_actor(TENANT, actor, role)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
    repo_root = Path(__file__).resolve().parents[4]
    source_roots = (
        repo_root / "apps/api/src",
        repo_root / "apps/worker/src",
        repo_root / "packages/policy/src",
        repo_root / "packages/contracts/src",
        repo_root / "packages/observability/src",
        repo_root,
    )
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH", "")
    pythonpath_entries = [str(path) for path in source_roots]
    if existing_pythonpath:
        pythonpath_entries.append(existing_pythonpath)
    environment["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
    if environment_overrides:
        environment.update(environment_overrides)
    # Static test bootstrap plus fixed fixture identifiers; no shell or user input.
    # noqa is attached to the call because Bandit cannot inspect this local harness.
    process = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-c",
            _RESTART_API_BOOTSTRAP,
            TENANT,
            str(actor_id),
            role,
            str(port),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        env=environment,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("disposable API process exited before becoming ready")
        try:
            response = httpx.get(f"{base_url}/healthz", timeout=0.5)
            if response.status_code == 200:
                return process, base_url
        except httpx.RequestError:
            time.sleep(0.05)
    _stop_restart_api(process)
    raise RuntimeError("disposable API process did not become ready within 10 seconds")


def _stop_restart_api(process: subprocess.Popen[bytes]) -> None:
    """Hard-exit the disposable API between requests to model process loss."""
    if process.poll() is not None:
        return
    process.kill()
    process.wait(timeout=5)


def _restart_api_request(
    base_url: str,
    method: str,
    path: str,
    *,
    key: str,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    response = httpx.request(
        method,
        f"{base_url}{path}",
        headers={"Authorization": "Bearer pt_restart_test", "Idempotency-Key": key},
        json=body,
        timeout=5,
    )
    return int(response.status_code), response.json()


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": "Bearer pt_bootstrap_test", "Idempotency-Key": key}


def _clear() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tenant in (TENANT, OTHER_TENANT):
            conn.execute(
                text("DELETE FROM standard_flow_start_requests WHERE tenant_id = :t"),
                {"t": tenant},
            )
            conn.execute(
                text("DELETE FROM action_confirmations WHERE tenant_id = :t"), {"t": tenant}
            )
            conn.execute(
                text("DELETE FROM tool_execution_reconciliations WHERE tenant_id = :t"),
                {"t": tenant},
            )
            conn.execute(text("DELETE FROM tool_executions WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(text("DELETE FROM tool_proposals WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(
                text("DELETE FROM conversation_task_events WHERE tenant_id = :t"),
                {"t": tenant},
            )
            conn.execute(text("DELETE FROM conversation_tasks WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(text("DELETE FROM case_conversations WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(text("DELETE FROM cases WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(
                text("DELETE FROM enterprise_accounts WHERE tenant_id = :t"), {"t": tenant}
            )
            conn.execute(text("DELETE FROM memberships WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(text("DELETE FROM departments WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(text("DELETE FROM tool_definitions WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(text("DELETE FROM connectors WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(text("DELETE FROM conversation_turns WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(
                text("DELETE FROM conversation_control_leases WHERE tenant_id = :t"),
                {"t": tenant},
            )
            conn.execute(text("DELETE FROM audit_events WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(text("DELETE FROM outbox_events WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(
                text("DELETE FROM feature_flag_targets WHERE tenant_id = :t"),
                {"t": tenant},
            )
            conn.execute(text("DELETE FROM feature_flags WHERE tenant_id = :t"), {"t": tenant})
        conn.execute(text("DELETE FROM users WHERE primary_email LIKE 'r2flow-%@example.test'"))
    admin.dispose()


def _seed(*, enabled: bool = True, include_turn: bool = True) -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tenant, slug in ((TENANT, SLUG), (OTHER_TENANT, OTHER_SLUG)):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, :slug, 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tenant, "slug": slug},
            )
        conn.execute(
            text(
                "INSERT INTO conversation_control_leases "
                "(id, tenant_id, conversation_ref_id, owner_type, owner_ref, mode, "
                "lease_version, changed_reason, updated_at) "
                "VALUES (:id, :t, :c, 'human', :owner, 'HUMAN_ACTIVE', :v, 'test', 0) "
                "ON CONFLICT (tenant_id, conversation_ref_id) DO UPDATE SET "
                "owner_type='human', owner_ref=:owner, lease_version=:v"
            ),
            {
                "id": uuid.uuid4(),
                "t": TENANT,
                "c": CONVERSATION,
                "owner": str(AGENT_ID),
                "v": LEASE_VERSION,
            },
        )
        conn.execute(
            text(
                "INSERT INTO conversation_control_leases "
                "(id, tenant_id, conversation_ref_id, owner_type, owner_ref, mode, "
                "lease_version, changed_reason, updated_at) "
                "VALUES (:id, :t, :c, 'human', :owner, 'HUMAN_ACTIVE', 1, 'test', 0) "
                "ON CONFLICT (tenant_id, conversation_ref_id) DO NOTHING"
            ),
            {
                "id": uuid.uuid4(),
                "t": OTHER_TENANT,
                "c": OTHER_CONVERSATION,
                "owner": str(AGENT_ID),
            },
        )
        if enabled:
            conn.execute(
                text(
                    "INSERT INTO feature_flags "
                    "(id, tenant_id, key, description, enabled, rollout_percent, created_at) "
                    "VALUES (:id, :t, 'agent.standard_flow_instances', 'test', true, 100, 0)"
                ),
                {"id": uuid.uuid4(), "t": TENANT},
            )
        if include_turn:
            conn.execute(
                text(
                    "INSERT INTO conversation_turns "
                    "(id, tenant_id, conversation_ref_id, role, text_redacted, text_hash, "
                    "ts, ref, source, origin, source_refs, created_at) "
                    "VALUES (:id, :t, :c, 'customer', '订单进度如何？', :hash, 10, '', "
                    "'channel', '', '[]', 10) ON CONFLICT (id) DO NOTHING"
                ),
                {
                    "id": CUSTOMER_TURN,
                    "t": TENANT,
                    "c": CONVERSATION,
                    "hash": "a" * 64,
                },
            )
    admin.dispose()


@pytest.fixture(autouse=True)
def _clean_seed():
    _clear()
    _seed()
    yield
    _clear()


def _start(*, key: str = "flow-start-1", flow_key: str = "order_status"):
    return _client().post(
        f"/v1/workbench/conversations/{CONVERSATION}/standard-flows/tasks",
        headers=_headers(key),
        json={"flow_key": flow_key, "expected_lease_version": LEASE_VERSION},
    )


def _seed_invoice_context() -> tuple[str, str]:
    account_id = str(uuid.uuid4())
    case_id = str(uuid.uuid4())
    tool_id = str(uuid.uuid4())
    input_schema = {
        "type": "object",
        "properties": {
            "enterprise_account_id": {"type": "string"},
            "subject": {"type": "string"},
            "description": {"type": "string"},
            "priority": {"type": "string"},
            "category": {"type": "string"},
            "conversation_ref_id": {"type": "string"},
            "team_ref": {"type": "string"},
            "product_ref": {"type": "string"},
            "product_verification_source": {"type": "string", "enum": ["demo"]},
        },
        "required": ["enterprise_account_id", "subject"],
        "additionalProperties": False,
    }
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO enterprise_accounts "
                "(id, tenant_id, name, tier, contract_status, attributes, created_at, updated_at) "
                "VALUES (:id, :t, 'Verified billing account', 'standard', 'active', "
                "CAST(:attributes AS jsonb), 1, 1)"
            ),
            {
                "id": account_id,
                "t": TENANT,
                "attributes": json.dumps({"business_system_refs": {"business_api": "acme"}}),
            },
        )
        conn.execute(
            text(
                "INSERT INTO cases (id, tenant_id, enterprise_account_id, subject, description, "
                "category, priority, status, version, opened_at, elapsed_running_seconds, "
                "last_state_changed_at) VALUES (:id, :t, :account, 'Existing customer case', '', "
                "'general', 'p2', 'new', 1, 1, 0, 1)"
            ),
            {"id": case_id, "t": TENANT, "account": account_id},
        )
        conn.execute(
            text(
                "INSERT INTO case_conversations (id, tenant_id, case_id, conversation_ref_id, "
                "relationship) VALUES (:id, :t, :case, :conversation, 'origin')"
            ),
            {
                "id": uuid.uuid4(),
                "t": TENANT,
                "case": case_id,
                "conversation": CONVERSATION,
            },
        )
        conn.execute(
            text(
                "INSERT INTO tool_definitions (id, tenant_id, name, version, risk, input_schema, "
                "output_schema, required_permissions, timeout_ms, idempotent, "
                "requires_confirmation) "
                "VALUES (:id, :t, 'case.create', 1, 'confirmed_write', CAST(:schema AS jsonb), "
                "'{}'::jsonb, CAST(:permissions AS jsonb), 10000, true, true)"
            ),
            {
                "id": tool_id,
                "t": TENANT,
                "schema": json.dumps(input_schema),
                "permissions": json.dumps(["tool.write.confirmed"]),
            },
        )
    admin.dispose()
    return account_id, tool_id


def test_demo_presales_endpoint_returns_only_source_backed_non_quote_evidence() -> None:
    account_id, _tool_id = _seed_invoice_context()
    tasks = _client().get(f"/v1/workbench/conversations/{CONVERSATION}/tasks")
    assert tasks.status_code == 200, tasks.text
    assert tasks.json()["demo_presales_enabled"] is True

    response = _client().get(
        f"/v1/workbench/conversations/{CONVERSATION}/demo-presales/PCB-DEMO-100"
    )
    assert response.status_code == 200, response.text
    evidence = response.json()["evidence"]
    assert response.json()["demo_only"] is True
    assert evidence["product_ref"] == "PCB-DEMO-100"
    assert evidence["available_quantity"] == "120"
    assert evidence["indicative_unit_price_minor"] == 1250
    assert evidence["synthetic"] is True
    assert evidence["customer_quote_allowed"] is False
    assert evidence["handoff_required"] is True
    assert evidence["product_source_version"] == "demo-fixture-v1"
    assert evidence["inventory_source_version"] == "demo-fixture-v1"
    assert evidence["quote_source_version"] == "demo-fixture-v1"

    unknown = _client().get(
        f"/v1/workbench/conversations/{CONVERSATION}/demo-presales/PCB-NOT-IN-DEMO"
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "DEMO_PRESALES_RECORD_UNAVAILABLE"

    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "UPDATE enterprise_accounts SET attributes = CAST(:attributes AS jsonb) "
                "WHERE tenant_id = :t AND id = :account"
            ),
            {
                "t": TENANT,
                "account": account_id,
                "attributes": json.dumps({"business_system_refs": {"business_api": "other-co"}}),
            },
        )
    admin.dispose()

    foreign = _client().get(
        f"/v1/workbench/conversations/{CONVERSATION}/demo-presales/PCB-DEMO-100"
    )
    assert foreign.status_code == 409
    assert foreign.json()["error"]["code"] == "DEMO_PRESALES_ACCOUNT_UNVERIFIED"


def test_demo_presales_endpoint_requires_business_read_permission() -> None:
    _seed_invoice_context()
    viewer = _client(role="support_viewer")
    tasks = viewer.get(f"/v1/workbench/conversations/{CONVERSATION}/tasks")
    assert tasks.status_code == 200, tasks.text
    assert tasks.json()["demo_presales_enabled"] is False

    response = viewer.get(
        f"/v1/workbench/conversations/{CONVERSATION}/demo-presales/PCB-DEMO-100",
        headers=_headers("demo-presales-viewer-denied"),
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "POLICY_DENIED"


def _seed_flow_teams() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for slug in ("quality", "engineering"):
            conn.execute(
                text(
                    "INSERT INTO departments "
                    "(id, tenant_id, name, slug, created_at, updated_at) "
                    "VALUES (:id, :t, :slug, :slug, 1, 1) "
                    "ON CONFLICT (tenant_id, slug) DO NOTHING"
                ),
                {"id": uuid.uuid4(), "t": TENANT, "slug": slug},
            )
            department_id = conn.execute(
                text("SELECT id FROM departments WHERE tenant_id = :t AND slug = :slug"),
                {"t": TENANT, "slug": slug},
            ).scalar_one()
            email = f"r2flow-{slug}@example.test"
            user_id = uuid.uuid5(uuid.NAMESPACE_URL, email)
            conn.execute(
                text(
                    "INSERT INTO users (id, primary_email, display_name, is_service_account) "
                    "VALUES (:id, :email, :slug, false) ON CONFLICT (primary_email) DO NOTHING"
                ),
                {"id": user_id, "email": email, "slug": f"Demo {slug} owner"},
            )
            conn.execute(
                text(
                    "INSERT INTO memberships "
                    "(id, tenant_id, user_id, role, status, department_id) "
                    "VALUES (:id, :t, :user, 'support_agent', 'active', :department) "
                    "ON CONFLICT (tenant_id, user_id) DO UPDATE SET "
                    "role='support_agent', status='active', department_id=:department"
                ),
                {
                    "id": uuid.uuid4(),
                    "t": TENANT,
                    "user": user_id,
                    "department": department_id,
                },
            )
    admin.dispose()


def _seed_demo_order_reader() -> None:
    from platform_core.integrations.business_read import READ_TOOL_SCHEMAS

    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tool_definitions (id, tenant_id, name, version, risk, input_schema, "
                "output_schema, required_permissions, timeout_ms, idempotent, "
                "requires_confirmation) VALUES (:id, :t, 'order.get_status', 1, 'read', "
                "CAST(:schema AS jsonb), '{}'::jsonb, CAST(:permissions AS jsonb), 10000, "
                "true, false)"
            ),
            {
                "id": uuid.uuid4(),
                "t": TENANT,
                "schema": json.dumps(READ_TOOL_SCHEMAS["order.get_status"]),
                "permissions": json.dumps(["tool.read"]),
            },
        )
        conn.execute(
            text(
                "INSERT INTO connectors "
                "(id, tenant_id, provider, name, status, capabilities, configuration) "
                "VALUES (:id, :t, 'business_api', 'Demo ERP', 'active', "
                "CAST(:capabilities AS jsonb), '{}'::jsonb)"
            ),
            {
                "id": uuid.uuid4(),
                "t": TENANT,
                "capabilities": json.dumps(["orders_read"]),
            },
        )
    admin.dispose()


def test_start_binds_customer_turn_and_stays_out_of_scheduler() -> None:
    response = _start()
    assert response.status_code == 200, response.text
    task = response.json()["task"]
    assert task["flow_key"] == "order_status"
    assert task["flow_version"] == 1
    assert task["flow_title"] == "订单进度查询"
    assert task["source_turn_id"] == CUSTOMER_TURN
    assert task["status"] == "manual_flow"
    assert task["blocked_reason"] == "FLOW_EXECUTOR_UNAVAILABLE"
    assert task["flow_can_query_order"] is False
    assert task["missing_slots"] == ["order_id"]
    assert response.json()["replayed"] is False


def test_start_replays_same_request_and_refuses_different_body() -> None:
    first = _start(key="stable-flow-key")
    later_turn = uuid.uuid4()
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO conversation_turns "
                "(id, tenant_id, conversation_ref_id, role, text_redacted, text_hash, "
                "ts, ref, source, origin, source_refs, created_at) "
                "VALUES (:id, :t, :c, 'customer', '还有一条消息', :hash, 20, '', "
                "'channel', '', '[]', 20)"
            ),
            {
                "id": later_turn,
                "t": TENANT,
                "c": CONVERSATION,
                "hash": "c" * 64,
            },
        )
    admin.dispose()
    replay = _start(key="stable-flow-key")
    different = _start(key="stable-flow-key", flow_key="repair_quality_intake")
    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    assert replay.json()["task"]["task_id"] == first.json()["task"]["task_id"]
    assert replay.json()["task"]["source_turn_id"] == CUSTOMER_TURN
    assert different.status_code == 409
    assert different.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_different_idempotency_key_does_not_duplicate_the_same_turn_flow() -> None:
    first = _start(key="flow-start-a")
    second = _start(key="flow-start-b")
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["replayed"] is True
    assert second.json()["task"]["task_id"] == first.json()["task"]["task_id"]


def test_collected_flow_fields_remain_manual_and_cannot_prepare_a_proposal() -> None:
    _seed_invoice_context()
    started = _start()
    task = started.json()["task"]
    collected = _client().post(
        f"/v1/workbench/conversations/{CONVERSATION}/tasks/{task['task_id']}/commands",
        headers=_headers("flow-collect-1"),
        json={
            "command": "collect_fields",
            "expected_version": task["version"],
            "expected_lease_version": LEASE_VERSION,
            "fields": {"order_id": "SO-9001"},
        },
    )
    assert collected.status_code == 200, collected.text
    updated = collected.json()["task"]
    assert updated["status"] == "manual_flow"
    assert updated["missing_slots"] == []
    assert updated["slots"][0]["origin"] == "verified_business_record"
    assert updated["slots"][0]["value"] == "SO-9001"
    assert updated["slots"][0]["verification_source"] == "demo"

    proposal = _client().post(
        f"/v1/workbench/conversations/{CONVERSATION}/tasks/{task['task_id']}/commands",
        headers=_headers("flow-prepare-1"),
        json={
            "command": "prepare_proposal",
            "expected_version": updated["version"],
            "expected_lease_version": LEASE_VERSION,
        },
    )
    assert proposal.status_code == 409
    assert proposal.json()["error"]["code"] == "TASK_COMMAND_REFUSED"


def test_start_is_default_off_and_requires_the_human_owner() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(text("DELETE FROM feature_flags WHERE tenant_id = :t"), {"t": TENANT})
    admin.dispose()
    disabled = _start(key="off-by-default")
    assert disabled.status_code == 403
    assert disabled.json()["error"]["code"] == "STANDARD_FLOW_INSTANCES_DISABLED"

    not_owner = _client(agent="another-agent").post(
        f"/v1/workbench/conversations/{CONVERSATION}/standard-flows/tasks",
        headers=_headers("not-owner"),
        json={"flow_key": "order_status", "expected_lease_version": LEASE_VERSION},
    )
    assert not_owner.status_code == 409
    assert not_owner.json()["error"]["code"] == "LEASE_NOT_OWNED"


def test_start_requires_a_server_resolved_customer_turn() -> None:
    _clear()
    _seed(include_turn=False)
    response = _start(key="no-customer-turn")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "FLOW_SOURCE_TURN_REQUIRED"


def test_demo_order_flow_verifies_ownership_runs_tool_gateway_and_records_receipt() -> None:
    _seed_invoice_context()
    _seed_demo_order_reader()
    started = _start(key="order-query-start", flow_key="order_status")
    assert started.status_code == 200, started.text
    task = started.json()["task"]
    assert task["flow_can_query_order"] is True

    foreign = _client(role="support_admin").post(
        f"/v1/workbench/conversations/{CONVERSATION}/tasks/{task['task_id']}/commands",
        headers=_headers("order-query-foreign-order"),
        json={
            "command": "collect_fields",
            "expected_version": task["version"],
            "expected_lease_version": LEASE_VERSION,
            "fields": {"order_id": "SO-9002"},
        },
    )
    assert foreign.status_code == 409
    assert foreign.json()["error"]["code"] == "FLOW_BUSINESS_RECORD_UNVERIFIED"

    task = _collect_flow_fields(task, key="order-query-order-id", fields={"order_id": "SO-9001"})
    assert task["missing_slots"] == []
    order_slot = next(slot for slot in task["slots"] if slot["name"] == "order_id")
    assert order_slot["origin"] == "verified_business_record"
    assert order_slot["verification_source"] == "demo"

    queried = _client().post(
        f"/v1/workbench/conversations/{CONVERSATION}/tasks/{task['task_id']}/commands",
        headers=_headers("order-query-run"),
        json={
            "command": "query_order_status",
            "expected_version": task["version"],
            "expected_lease_version": LEASE_VERSION,
        },
    )
    assert queried.status_code == 200, queried.text
    verified = queried.json()["task"]
    assert verified["status"] == "succeeded"
    assert verified["proposal_id"]
    assert verified["execution_id"]
    receipt_slot = next(
        slot for slot in verified["slots"] if slot["name"] == "order_status_receipt"
    )
    assert receipt_slot["origin"] == "verified_receipt"
    assert receipt_slot["verification_source"] == "demo"
    assert receipt_slot["value"]["order_id"] == "SO-9001"
    assert receipt_slot["value"]["status"] == "in_production"
    assert receipt_slot["value"]["source"] == "demo"

    admin = create_engine(ADMIN_URL)
    with admin.connect() as conn:
        execution = conn.execute(
            text(
                "SELECT status, verification_status FROM tool_executions "
                "WHERE tenant_id = :t AND id = :id"
            ),
            {"t": TENANT, "id": verified["execution_id"]},
        ).one()
    admin.dispose()
    assert execution == ("executed", "verified")


@pytest.mark.parametrize(
    ("failure_mode", "expected_status"),
    [("unknown", "unknown"), ("timeout", "failed")],
)
def test_demo_order_query_unknown_or_timeout_never_creates_a_success_receipt(
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
    expected_status: str,
) -> None:
    _seed_invoice_context()
    _seed_demo_order_reader()
    started = _start(key=f"order-query-{failure_mode}-start", flow_key="order_status")
    task = _collect_flow_fields(
        started.json()["task"],
        key=f"order-query-{failure_mode}-fields",
        fields={"order_id": "SO-9001"},
    )

    class _UncertainOrderExecutor:
        async def execute(
            self, tool_name: str, parameters: dict[str, Any], idempotency_key: str
        ) -> dict[str, Any]:
            if failure_mode == "timeout":
                raise TimeoutError("synthetic connector timeout")
            return {
                "found": True,
                "order_id": parameters["order_id"],
                "status": "in_production",
                "account": "acme",
                "source": "demo",
                "fetched_at": "2026-09-28T00:00:00Z",
                "nodes": [],
                "eta": None,
            }

        async def verify_postcondition(
            self, tool_name: str, parameters: dict[str, Any], output: dict[str, Any] | None
        ) -> bool | None:
            return None if failure_mode == "unknown" else isinstance(output, dict)

    async def resolve_test_executor(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"order.get_status": _UncertainOrderExecutor()}

    monkeypatch.setattr(
        "platform_core.tool_gateway.registry.resolve_executors", resolve_test_executor
    )
    queried = _client().post(
        f"/v1/workbench/conversations/{CONVERSATION}/tasks/{task['task_id']}/commands",
        headers=_headers(f"order-query-{failure_mode}-run"),
        json={
            "command": "query_order_status",
            "expected_version": task["version"],
            "expected_lease_version": LEASE_VERSION,
        },
    )

    assert queried.status_code == 200, queried.text
    result_task = queried.json()["task"]
    assert result_task["status"] == expected_status
    assert result_task["blocked_reason"] in {"TOOL_EXECUTION_FAILED", "TOOL_EXECUTION_UNKNOWN"}
    assert not any(slot["name"] == "order_status_receipt" for slot in result_task["slots"])


def test_tenant_cannot_start_against_another_tenants_conversation() -> None:
    response = _client(tenant=OTHER_TENANT).post(
        f"/v1/workbench/conversations/{CONVERSATION}/standard-flows/tasks",
        headers=_headers("cross-tenant-start"),
        json={"flow_key": "order_status", "expected_lease_version": LEASE_VERSION},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CASE_NOT_FOUND"


def test_manual_flow_can_be_cancelled_or_handed_to_a_person() -> None:
    cancelled = _start(key="flow-cancel").json()["task"]
    handed = _start(key="flow-handoff", flow_key="repair_quality_intake").json()["task"]
    for task, command, key in (
        (cancelled, "cancel", "flow-cancel-command"),
        (handed, "handoff", "flow-handoff-command"),
    ):
        response = _client().post(
            f"/v1/workbench/conversations/{CONVERSATION}/tasks/{task['task_id']}/commands",
            headers=_headers(key),
            json={
                "command": command,
                "expected_version": task["version"],
                "expected_lease_version": LEASE_VERSION,
            },
        )
        assert response.status_code == 200, response.text
        expected = "cancelled" if command == "cancel" else "needs_human"
        assert response.json()["task"]["status"] == expected


def test_verified_business_fields_cannot_be_entered_as_free_text() -> None:
    task = _start(key="flow-verified-source", flow_key="repair_quality_intake").json()["task"]
    response = _client().post(
        f"/v1/workbench/conversations/{CONVERSATION}/tasks/{task['task_id']}/commands",
        headers=_headers("flow-verified-field"),
        json={
            "command": "collect_fields",
            "expected_version": task["version"],
            "expected_lease_version": LEASE_VERSION,
            "fields": {"customer_account_ref": "untrusted-account"},
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "FLOW_FIELD_SOURCE_REQUIRES_VERIFICATION"


def _collect_invoice_fields(task: dict[str, object], *, key: str) -> dict[str, object]:
    response = _client().post(
        f"/v1/workbench/conversations/{CONVERSATION}/tasks/{task['task_id']}/commands",
        headers=_headers(key),
        json={
            "command": "collect_fields",
            "expected_version": task["version"],
            "expected_lease_version": LEASE_VERSION,
            "fields": {"order_id": "SO-1234", "invoice_type": "增值税专用发票"},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["task"]


def _prepare_invoice_proposal(
    task: dict[str, object], *, key: str, role: str = "support_admin"
) -> Any:
    response = _client(role=role, agent=AGENT_REF).post(
        f"/v1/workbench/conversations/{CONVERSATION}/tasks/{task['task_id']}/commands",
        headers=_headers(key),
        json={
            "command": "prepare_proposal",
            "expected_version": task["version"],
            "expected_lease_version": LEASE_VERSION,
        },
    )
    return response


def _collect_flow_fields(
    task: dict[str, Any], *, key: str, fields: dict[str, str]
) -> dict[str, Any]:
    response = _client(role="support_admin").post(
        f"/v1/workbench/conversations/{CONVERSATION}/tasks/{task['task_id']}/commands",
        headers=_headers(key),
        json={
            "command": "collect_fields",
            "expected_version": task["version"],
            "expected_lease_version": LEASE_VERSION,
            "fields": fields,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["task"]


def _prepare_flow_proposal(task: dict[str, Any], *, key: str, role: str = "support_admin") -> Any:
    return _client(role=role, agent=AGENT_REF).post(
        f"/v1/workbench/conversations/{CONVERSATION}/tasks/{task['task_id']}/commands",
        headers=_headers(key),
        json={
            "command": "prepare_proposal",
            "expected_version": task["version"],
            "expected_lease_version": LEASE_VERSION,
        },
    )


@pytest.mark.parametrize(
    (
        "flow_key",
        "customer_field",
        "customer_text",
        "expected_category",
        "expected_team",
    ),
    [
        (
            "repair_quality_intake",
            "issue_summary",
            "焊点开裂，回电号码 13800138000，请质量团队核查",
            "quality_issue",
            "quality",
        ),
        (
            "technical_escalation",
            "question_or_symptom",
            "板卡上电后间歇复位，需要工程团队分析",
            "technical_escalation",
            "engineering",
        ),
    ],
)
def test_demo_quality_and_technical_flows_prepare_confirmed_team_cases(
    flow_key: str,
    customer_field: str,
    customer_text: str,
    expected_category: str,
    expected_team: str,
) -> None:
    _seed_invoice_context()
    _seed_flow_teams()
    started = _start(key=f"{flow_key}-start", flow_key=flow_key)
    assert started.status_code == 200, started.text
    task = started.json()["task"]
    assert set(task["missing_slots"]) == {"product_ref", customer_field}

    cross_account = _client(role="support_admin").post(
        f"/v1/workbench/conversations/{CONVERSATION}/tasks/{task['task_id']}/commands",
        headers=_headers(f"{flow_key}-wrong-product"),
        json={
            "command": "collect_fields",
            "expected_version": task["version"],
            "expected_lease_version": LEASE_VERSION,
            "fields": {"product_ref": "PCB-DEMO-900", customer_field: customer_text},
        },
    )
    assert cross_account.status_code == 409
    assert cross_account.json()["error"]["code"] == "FLOW_BUSINESS_RECORD_UNVERIFIED"

    task = _collect_flow_fields(
        task,
        key=f"{flow_key}-verified-fields",
        fields={"product_ref": "PCB-DEMO-100", customer_field: customer_text},
    )
    assert task["missing_slots"] == []
    product_slot = next(slot for slot in task["slots"] if slot["name"] == "product_ref")
    assert product_slot["origin"] == "verified_business_record"
    assert product_slot["verification_source"] == "demo"
    assert product_slot["authority_version"] == "demo-product-catalog-v1"

    denied = _prepare_flow_proposal(task, key=f"{flow_key}-agent-denied", role="support_agent")
    assert denied.status_code == 403
    prepared = _prepare_flow_proposal(task, key=f"{flow_key}-admin-proposal")
    assert prepared.status_code == 200, prepared.text
    proposal_task = prepared.json()["task"]
    assert proposal_task["status"] == "awaiting_confirmation"

    admin = create_engine(ADMIN_URL)
    with admin.connect() as conn:
        proposal = conn.execute(
            text(
                "SELECT sanitized_input, required_confirmation FROM tool_proposals "
                "WHERE tenant_id = :t AND id = :id"
            ),
            {"t": TENANT, "id": proposal_task["proposal_id"]},
        ).one()
    admin.dispose()
    assert proposal.sanitized_input["category"] == expected_category
    assert proposal.sanitized_input["team_ref"] == expected_team
    assert proposal.sanitized_input["product_ref"] == "PCB-DEMO-100"
    assert proposal.sanitized_input["product_verification_source"] == "demo"
    assert proposal.required_confirmation is True

    approver = _client(role="support_admin", agent=f"{flow_key}-approver")
    proposal_path = f"/v1/tool-proposals/{proposal_task['proposal_id']}"
    confirmed = approver.post(
        f"{proposal_path}/confirm", headers=_headers(f"{flow_key}-confirm"), json={}
    )
    assert confirmed.status_code == 200, confirmed.text
    executed = approver.post(
        f"{proposal_path}/execute",
        headers=_headers(f"{flow_key}-execute"),
        json={"reason": "坐席复核 Demo 产品归属后登记内部 Case"},
    )
    assert executed.status_code == 200, executed.text
    assert executed.json()["execution"]["verification_status"] == "verified"

    admin = create_engine(ADMIN_URL)
    with admin.connect() as conn:
        case_row = conn.execute(
            text(
                "SELECT category, team_ref, description FROM cases "
                "WHERE tenant_id = :t ORDER BY opened_at DESC LIMIT 1"
            ),
            {"t": TENANT},
        ).one()
    admin.dispose()
    assert case_row.category == expected_category
    assert case_row.team_ref == expected_team
    assert "本地 Demo 目录核验" in case_row.description
    from platform_core.evaluation.pii import redact_text

    safe_customer_text = redact_text(customer_text)[0]
    assert safe_customer_text in case_row.description
    if safe_customer_text != customer_text:
        assert customer_text not in case_row.description

    tasks = _client().get(f"/v1/workbench/conversations/{CONVERSATION}/tasks")
    completed = next(
        item for item in tasks.json()["items"] if item["task_id"] == proposal_task["task_id"]
    )
    assert completed["status"] == "succeeded"
    assert completed["execution_id"] == executed.json()["execution"]["execution_id"]


def test_demo_product_ownership_is_rechecked_when_confirmed_case_executes() -> None:
    account_id, _tool_id = _seed_invoice_context()
    _seed_flow_teams()
    started = _start(key="quality-stale-start", flow_key="repair_quality_intake")
    task = _collect_flow_fields(
        started.json()["task"],
        key="quality-stale-fields",
        fields={"product_ref": "PCB-DEMO-100", "issue_summary": "焊点开裂"},
    )
    prepared = _prepare_flow_proposal(task, key="quality-stale-proposal")
    assert prepared.status_code == 200, prepared.text
    task = prepared.json()["task"]

    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "UPDATE enterprise_accounts SET attributes = CAST(:attributes AS jsonb) "
                "WHERE tenant_id = :t AND id = :account"
            ),
            {
                "t": TENANT,
                "account": account_id,
                "attributes": json.dumps({"business_system_refs": {"business_api": "other-co"}}),
            },
        )
    admin.dispose()

    approver = _client(role="support_admin", agent="quality-stale-approver")
    proposal_path = f"/v1/tool-proposals/{task['proposal_id']}"
    confirmed = approver.post(
        f"{proposal_path}/confirm", headers=_headers("quality-stale-confirm"), json={}
    )
    assert confirmed.status_code == 200, confirmed.text
    executed = approver.post(
        f"{proposal_path}/execute",
        headers=_headers("quality-stale-execute"),
        json={"reason": "产品归属已变化，验证执行侧阻断"},
    )
    assert executed.status_code == 200, executed.text
    assert executed.json()["execution"]["verification_status"] == "failed"

    admin = create_engine(ADMIN_URL)
    with admin.connect() as conn:
        case_count = conn.execute(
            text("SELECT count(*) FROM cases WHERE tenant_id = :t"), {"t": TENANT}
        ).scalar_one()
    admin.dispose()
    assert case_count == 1


def test_demo_flow_refuses_to_prepare_when_the_department_has_no_active_owner() -> None:
    _seed_invoice_context()
    started = _start(key="quality-unowned-start", flow_key="repair_quality_intake")
    task = _collect_flow_fields(
        started.json()["task"],
        key="quality-unowned-fields",
        fields={"product_ref": "PCB-DEMO-100", "issue_summary": "焊点开裂"},
    )
    prepared = _prepare_flow_proposal(task, key="quality-unowned-proposal")
    assert prepared.status_code == 409
    assert prepared.json()["error"]["code"] == "FLOW_OWNER_UNASSIGNED"


def test_invoice_flow_prepares_only_a_confirmed_internal_case() -> None:
    account_id, _tool_id = _seed_invoice_context()
    started = _start(key="invoice-flow", flow_key="invoice_application")
    task = _collect_invoice_fields(started.json()["task"], key="invoice-fields")
    agent_denied = _prepare_invoice_proposal(
        task, key="invoice-agent-proposal", role="support_agent"
    )
    assert agent_denied.status_code == 403
    prepared = _prepare_invoice_proposal(task, key="invoice-proposal")
    assert prepared.status_code == 200, prepared.text
    proposed_task = prepared.json()["task"]
    assert proposed_task["status"] == "awaiting_confirmation"
    assert proposed_task["proposal_id"]
    assert proposed_task["blocked_reason"] == ""

    admin = create_engine(ADMIN_URL)
    with admin.connect() as conn:
        proposal = conn.execute(
            text(
                "SELECT sanitized_input, required_confirmation, status FROM tool_proposals "
                "WHERE tenant_id = :t AND id = :id"
            ),
            {"t": TENANT, "id": proposed_task["proposal_id"]},
        ).one()
    admin.dispose()
    assert proposal.sanitized_input["enterprise_account_id"] == account_id
    assert proposal.sanitized_input["category"] == "invoice_application"
    assert proposal.sanitized_input["conversation_ref_id"] == CONVERSATION
    assert proposal.required_confirmation is True


def test_invoice_flow_refuses_to_propose_without_an_unambiguous_account() -> None:
    started = _start(key="invoice-no-account", flow_key="invoice_application")
    task = _collect_invoice_fields(started.json()["task"], key="invoice-no-account-fields")
    response = _prepare_invoice_proposal(task, key="invoice-no-account-proposal")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "FLOW_ACCOUNT_UNVERIFIED"


def test_invoice_flow_refuses_conflicting_case_account_links() -> None:
    _seed_invoice_context()
    second_account = str(uuid.uuid4())
    second_case = str(uuid.uuid4())
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO enterprise_accounts "
                "(id, tenant_id, name, tier, contract_status, attributes, created_at, updated_at) "
                "VALUES (:id, :t, 'Conflicting account', 'standard', 'active', '{}'::jsonb, 1, 1)"
            ),
            {"id": second_account, "t": TENANT},
        )
        conn.execute(
            text(
                "INSERT INTO cases (id, tenant_id, enterprise_account_id, subject, description, "
                "category, priority, status, version, opened_at, elapsed_running_seconds, "
                "last_state_changed_at) VALUES (:id, :t, :account, 'Conflicting linked case', '', "
                "'general', 'p2', 'new', 1, 1, 0, 1)"
            ),
            {"id": second_case, "t": TENANT, "account": second_account},
        )
        conn.execute(
            text(
                "INSERT INTO case_conversations (id, tenant_id, case_id, conversation_ref_id, "
                "relationship) VALUES (:id, :t, :case, :conversation, 'related')"
            ),
            {
                "id": uuid.uuid4(),
                "t": TENANT,
                "case": second_case,
                "conversation": CONVERSATION,
            },
        )
    admin.dispose()

    started = _start(key="invoice-conflicting-account", flow_key="invoice_application")
    task = _collect_invoice_fields(started.json()["task"], key="invoice-conflicting-fields")
    response = _prepare_invoice_proposal(task, key="invoice-conflicting-proposal")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "FLOW_ACCOUNT_UNVERIFIED"


def test_cancel_withdraws_proposal_before_the_task_becomes_terminal() -> None:
    _seed_invoice_context()
    started = _start(key="invoice-cancel-flow", flow_key="invoice_application")
    task = _collect_invoice_fields(started.json()["task"], key="invoice-cancel-fields")
    prepared = _prepare_invoice_proposal(task, key="invoice-cancel-proposal")
    assert prepared.status_code == 200, prepared.text
    task = prepared.json()["task"]

    cancelled = _client().post(
        f"/v1/workbench/conversations/{CONVERSATION}/tasks/{task['task_id']}/commands",
        headers=_headers("invoice-cancel-command"),
        json={
            "command": "cancel",
            "expected_version": task["version"],
            "expected_lease_version": LEASE_VERSION,
        },
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["task"]["status"] == "cancelled"

    admin = create_engine(ADMIN_URL)
    with admin.connect() as conn:
        proposal_status = conn.execute(
            text("SELECT status FROM tool_proposals WHERE tenant_id = :t AND id = :id"),
            {"t": TENANT, "id": task["proposal_id"]},
        ).scalar_one()
    admin.dispose()
    assert proposal_status == "rejected"

    late_execute = _client(role="support_admin", agent="invoice-approver").post(
        f"/v1/tool-proposals/{task['proposal_id']}/execute",
        headers=_headers("invoice-late-execute"),
        json={},
    )
    assert late_execute.status_code == 409


def test_verified_tool_receipt_completes_the_linked_invoice_task() -> None:
    _seed_invoice_context()
    started = _start(key="invoice-execute-flow", flow_key="invoice_application")
    task = _collect_invoice_fields(started.json()["task"], key="invoice-execute-fields")
    prepared = _prepare_invoice_proposal(task, key="invoice-execute-proposal")
    assert prepared.status_code == 200, prepared.text
    task = prepared.json()["task"]
    approver = _client(role="support_admin", agent="invoice-approver")
    proposal_path = f"/v1/tool-proposals/{task['proposal_id']}"
    confirmed = approver.post(
        f"{proposal_path}/confirm",
        headers=_headers("invoice-confirm"),
        json={},
    )
    assert confirmed.status_code == 200, confirmed.text
    executed = approver.post(
        f"{proposal_path}/execute",
        headers=_headers("invoice-execute"),
        json={"reason": "人工复核后登记内部发票申请"},
    )
    assert executed.status_code == 200, executed.text
    assert executed.json()["execution"]["verification_status"] == "verified"

    tasks = _client().get(f"/v1/workbench/conversations/{CONVERSATION}/tasks")
    assert tasks.status_code == 200, tasks.text
    completed = next(item for item in tasks.json()["items"] if item["task_id"] == task["task_id"])
    assert completed["status"] == "succeeded"
    assert completed["execution_id"] == executed.json()["execution"]["execution_id"]


def test_linked_proposal_cannot_execute_after_its_task_lease_version_moves() -> None:
    _seed_invoice_context()
    started = _start(key="invoice-lease-start", flow_key="invoice_application")
    task = _collect_invoice_fields(started.json()["task"], key="invoice-lease-fields")
    prepared = _prepare_invoice_proposal(task, key="invoice-lease-proposal")
    assert prepared.status_code == 200, prepared.text
    task = prepared.json()["task"]

    admin = create_engine(ADMIN_URL)
    with admin.connect() as conn:
        proposal_lease_version = conn.execute(
            text(
                "SELECT proposal_lease_version FROM conversation_tasks "
                "WHERE tenant_id = :tenant AND id = :task"
            ),
            {"tenant": TENANT, "task": task["task_id"]},
        ).scalar_one()
    admin.dispose()
    assert proposal_lease_version == LEASE_VERSION

    approver = _client(role="support_admin", agent="invoice-lease-approver")
    proposal_path = f"/v1/tool-proposals/{task['proposal_id']}"
    confirmed = approver.post(
        f"{proposal_path}/confirm",
        headers=_headers("invoice-lease-confirm"),
        json={},
    )
    assert confirmed.status_code == 200, confirmed.text

    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "UPDATE conversation_control_leases SET lease_version = :version, "
                "owner_ref = :owner, changed_reason = 'reassigned while awaiting approval' "
                "WHERE tenant_id = :tenant AND conversation_ref_id = :conversation"
            ),
            {
                "version": LEASE_VERSION + 1,
                "owner": "another-human",
                "tenant": TENANT,
                "conversation": CONVERSATION,
            },
        )
    admin.dispose()

    executed = approver.post(
        f"{proposal_path}/execute",
        headers=_headers("invoice-lease-execute"),
        json={"reason": "the lease moved after confirmation"},
    )
    assert executed.status_code == 409, executed.text
    assert executed.json()["error"]["code"] == "TASK_LEASE_STALE"

    admin = create_engine(ADMIN_URL)
    with admin.connect() as conn:
        execution_count = conn.execute(
            text(
                "SELECT count(*) FROM tool_executions "
                "WHERE tenant_id = :tenant AND proposal_id = :proposal"
            ),
            {"tenant": TENANT, "proposal": task["proposal_id"]},
        ).scalar_one()
    admin.dispose()
    assert execution_count == 0


def test_pending_fields_and_approval_resume_after_api_process_restart() -> None:
    """Pending input and approval live in PostgreSQL, and stale leases stay fenced."""
    _seed_invoice_context()
    processes: list[subprocess.Popen[bytes]] = []

    def start_api(*, role: str, actor: str) -> tuple[subprocess.Popen[bytes], str]:
        process, base_url = _start_restart_api(role=role, actor=actor)
        processes.append(process)
        return process, base_url

    def stop_api(process: subprocess.Popen[bytes]) -> None:
        _stop_restart_api(process)
        if process in processes:
            processes.remove(process)

    try:
        first_process, first_url = start_api(role="support_agent", actor=AGENT_REF)
        status, started = _restart_api_request(
            first_url,
            "POST",
            f"/v1/workbench/conversations/{CONVERSATION}/standard-flows/tasks",
            key="process-restart-flow-start",
            body={"flow_key": "invoice_application", "expected_lease_version": LEASE_VERSION},
        )
        assert status == 200, started
        task = started["task"]
        assert task["status"] == "manual_flow"
        assert set(task["missing_slots"]) == {"order_id", "invoice_type"}
        stop_api(first_process)

        second_process, second_url = start_api(role="support_admin", actor=AGENT_REF)
        status, listed = _restart_api_request(
            second_url,
            "GET",
            f"/v1/workbench/conversations/{CONVERSATION}/tasks",
            key="process-restart-list-fields",
        )
        assert status == 200, listed
        resumed = next(item for item in listed["items"] if item["task_id"] == task["task_id"])
        assert resumed["status"] == "manual_flow"
        assert set(resumed["missing_slots"]) == {"order_id", "invoice_type"}

        status, collected = _restart_api_request(
            second_url,
            "POST",
            f"/v1/workbench/conversations/{CONVERSATION}/tasks/{task['task_id']}/commands",
            key="process-restart-collect-fields",
            body={
                "command": "collect_fields",
                "expected_version": resumed["version"],
                "expected_lease_version": LEASE_VERSION,
                "fields": {"order_id": "SO-1234", "invoice_type": "增值税专用发票"},
            },
        )
        assert status == 200, collected
        task = collected["task"]
        assert task["status"] == "manual_flow"
        assert task["missing_slots"] == []

        status, prepared = _restart_api_request(
            second_url,
            "POST",
            f"/v1/workbench/conversations/{CONVERSATION}/tasks/{task['task_id']}/commands",
            key="process-restart-prepare-proposal",
            body={
                "command": "prepare_proposal",
                "expected_version": task["version"],
                "expected_lease_version": LEASE_VERSION,
            },
        )
        assert status == 200, prepared
        task = prepared["task"]
        assert task["status"] == "awaiting_confirmation"
        proposal_id = task["proposal_id"]
        stop_api(second_process)

        third_process, third_url = start_api(role="support_admin", actor="restart-approver")
        status, listed = _restart_api_request(
            third_url,
            "GET",
            f"/v1/workbench/conversations/{CONVERSATION}/tasks",
            key="process-restart-list-approval",
        )
        assert status == 200, listed
        resumed = next(item for item in listed["items"] if item["task_id"] == task["task_id"])
        assert resumed["status"] == "awaiting_confirmation"
        assert resumed["proposal_id"] == proposal_id

        status, confirmed = _restart_api_request(
            third_url,
            "POST",
            f"/v1/tool-proposals/{proposal_id}/confirm",
            key="process-restart-confirm",
            body={},
        )
        assert status == 200, confirmed
        stop_api(third_process)

        fourth_process, fourth_url = start_api(role="support_admin", actor="restart-approver")
        status, proposal_detail = _restart_api_request(
            fourth_url,
            "GET",
            f"/v1/tool-proposals/{proposal_id}",
            key="process-restart-read-approved-proposal",
        )
        assert status == 200, proposal_detail
        assert proposal_detail["proposal"]["effective_status"] == "confirmed"

        admin = create_engine(ADMIN_URL)
        with admin.begin() as conn:
            conn.execute(
                text(
                    "UPDATE conversation_control_leases SET lease_version = :version, "
                    "owner_ref = :owner, changed_reason = 'reassigned during API restart drill' "
                    "WHERE tenant_id = :tenant AND conversation_ref_id = :conversation"
                ),
                {
                    "version": LEASE_VERSION + 1,
                    "owner": "another-human",
                    "tenant": TENANT,
                    "conversation": CONVERSATION,
                },
            )
        admin.dispose()

        status, rejected = _restart_api_request(
            fourth_url,
            "POST",
            f"/v1/tool-proposals/{proposal_id}/execute",
            key="process-restart-stale-lease-execute",
            body={"reason": "lease changed while approval was pending"},
        )
        assert status == 409, rejected
        assert rejected["error"]["code"] == "TASK_LEASE_STALE"

        admin = create_engine(ADMIN_URL)
        with admin.connect() as conn:
            execution_count = conn.execute(
                text(
                    "SELECT count(*) FROM tool_executions "
                    "WHERE tenant_id = :tenant AND proposal_id = :proposal"
                ),
                {"tenant": TENANT, "proposal": proposal_id},
            ).scalar_one()
        admin.dispose()
        assert execution_count == 0
        stop_api(fourth_process)
    finally:
        for process in list(processes):
            _stop_restart_api(process)


def test_api_process_crash_after_provider_acceptance_never_replays_the_write() -> None:
    """A committed execution intent fences the same proposal after a hard exit."""
    provider = _FakeProviderServer()
    provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()
    admin = create_engine(ADMIN_URL)
    tool_id = uuid.uuid4()
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tool_definitions "
                "(id, tenant_id, name, version, risk, input_schema, output_schema, "
                "required_permissions, timeout_ms, idempotent, requires_confirmation) "
                "VALUES (:id, :tenant, 'jira.create_issue', 1, 'confirmed_write', "
                "CAST(:schema AS jsonb), '{}'::jsonb, '[]'::jsonb, 10000, true, true)"
            ),
            {
                "id": tool_id,
                "tenant": TENANT,
                "schema": json.dumps(
                    {
                        "type": "object",
                        "properties": {
                            "project": {"type": "string"},
                            "summary": {"type": "string"},
                        },
                        "required": ["project", "summary"],
                        "additionalProperties": False,
                    }
                ),
            },
        )
        conn.execute(
            text(
                "INSERT INTO connectors "
                "(id, tenant_id, provider, name, status, capabilities, configuration, "
                "credential_ref) VALUES (:id, :tenant, 'jira', 'Crash drill Jira', 'active', "
                "'[\"create_issue\"]'::jsonb, '{}'::jsonb, 'vault://phase2/fake')"
            ),
            {"id": uuid.uuid4(), "tenant": TENANT},
        )

    provider_url = f"http://127.0.0.1:{provider.server_port}/accepted"
    processes: list[subprocess.Popen[bytes]] = []

    def start_api(*, kill_after_provider: bool) -> tuple[subprocess.Popen[bytes], str]:
        process, base_url = _start_restart_api(
            role="support_admin",
            actor=AGENT_REF,
            environment_overrides={
                "PHASE2_TEST_FAKE_PROVIDER_URL": provider_url,
                "PHASE2_TEST_KILL_AFTER_PROVIDER": "1" if kill_after_provider else "0",
            },
        )
        processes.append(process)
        return process, base_url

    def stop_api(process: subprocess.Popen[bytes]) -> None:
        _stop_restart_api(process)
        if process in processes:
            processes.remove(process)

    try:
        first_process, first_url = start_api(kill_after_provider=True)
        status, proposed = _restart_api_request(
            first_url,
            "POST",
            "/v1/tool-proposals",
            key="gateway-crash-proposal",
            body={
                "tool_name": "jira.create_issue",
                "arguments": {"project": "SUP", "summary": "synthetic lost-ack case"},
            },
        )
        assert status == 200, proposed
        proposal_id = proposed["proposal"]["proposal_id"]

        status, confirmed = _restart_api_request(
            first_url,
            "POST",
            f"/v1/tool-proposals/{proposal_id}/confirm",
            key="gateway-crash-confirm",
            body={},
        )
        assert status == 200, confirmed

        try:
            _restart_api_request(
                first_url,
                "POST",
                f"/v1/tool-proposals/{proposal_id}/execute",
                key="gateway-crash-execute",
                body={},
            )
        except httpx.RequestError:
            pass
        else:
            pytest.fail("the synthetic API process should hard-exit after provider acceptance")
        first_process.wait(timeout=5)
        assert provider.call_count == 1
        stop_api(first_process)

        with admin.begin() as conn:
            persisted = conn.execute(
                text(
                    "SELECT e.status, e.idempotency_key, p.status "
                    "FROM tool_executions e JOIN tool_proposals p ON p.id = e.proposal_id "
                    "WHERE e.tenant_id = :tenant AND p.id = :proposal"
                ),
                {"tenant": TENANT, "proposal": proposal_id},
            ).one()
            conn.execute(
                text(
                    "UPDATE tool_executions SET started_at = 0 "
                    "WHERE tenant_id = :tenant AND proposal_id = :proposal"
                ),
                {"tenant": TENANT, "proposal": proposal_id},
            )
        assert persisted == ("executing", "gateway-crash-proposal", "executing")

        second_process, second_url = start_api(kill_after_provider=False)
        status, unresolved = _restart_api_request(
            second_url,
            "POST",
            f"/v1/tool-proposals/{proposal_id}/execute",
            key="gateway-crash-execute",
            body={},
        )
        assert status == 409, unresolved
        assert unresolved["error"]["code"] == "TOOL_EXECUTION_OUTCOME_UNKNOWN"
        assert provider.call_count == 1, "the provider mutation must not be re-sent"

        status, reconciled = _restart_api_request(
            second_url,
            "POST",
            f"/v1/tool-proposals/{proposal_id}/reconcile",
            key="gateway-crash-reconcile",
            body={"decision": "applied", "evidence_reference": "SUP-CRASH-1"},
        )
        assert status == 200, reconciled
        assert reconciled["execution"]["status"] == "executed"
        assert reconciled["execution"]["verification_status"] == "verified"
        assert provider.call_count == 1
        stop_api(second_process)
    finally:
        for process in list(processes):
            _stop_restart_api(process)
        provider.shutdown()
        provider.server_close()
        provider_thread.join(timeout=5)
        admin.dispose()


def test_invoice_executor_rechecks_account_link_before_creating_the_case() -> None:
    _seed_invoice_context()
    started = _start(key="invoice-stale-account-flow", flow_key="invoice_application")
    task = _collect_invoice_fields(started.json()["task"], key="invoice-stale-account-fields")
    prepared = _prepare_invoice_proposal(task, key="invoice-stale-account-proposal")
    assert prepared.status_code == 200, prepared.text
    task = prepared.json()["task"]

    # Make the conversation's account mapping ambiguous after proposal creation.
    # The executor must re-check the source relation rather than trusting a
    # stale proposal value or claiming the internal Case was created.
    second_account = str(uuid.uuid4())
    second_case = str(uuid.uuid4())
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO enterprise_accounts "
                "(id, tenant_id, name, tier, contract_status, attributes, created_at, updated_at) "
                "VALUES (:id, :t, 'Changed account', 'standard', 'active', '{}'::jsonb, 1, 1)"
            ),
            {"id": second_account, "t": TENANT},
        )
        conn.execute(
            text(
                "INSERT INTO cases (id, tenant_id, enterprise_account_id, subject, description, "
                "category, priority, status, version, opened_at, elapsed_running_seconds, "
                "last_state_changed_at) VALUES (:id, :t, :account, 'Changed account case', '', "
                "'general', 'p2', 'new', 1, 1, 0, 1)"
            ),
            {"id": second_case, "t": TENANT, "account": second_account},
        )
        conn.execute(
            text(
                "INSERT INTO case_conversations (id, tenant_id, case_id, conversation_ref_id, "
                "relationship) VALUES (:id, :t, :case, :conversation, 'related')"
            ),
            {
                "id": uuid.uuid4(),
                "t": TENANT,
                "case": second_case,
                "conversation": CONVERSATION,
            },
        )
    admin.dispose()

    approver = _client(role="support_admin", agent="invoice-approver")
    proposal_path = f"/v1/tool-proposals/{task['proposal_id']}"
    confirmed = approver.post(
        f"{proposal_path}/confirm",
        headers=_headers("invoice-stale-confirm"),
        json={},
    )
    assert confirmed.status_code == 200, confirmed.text
    executed = approver.post(
        f"{proposal_path}/execute",
        headers=_headers("invoice-stale-execute"),
        json={},
    )
    assert executed.status_code == 200, executed.text
    assert executed.json()["execution"]["verification_status"] == "failed"

    tasks = _client().get(f"/v1/workbench/conversations/{CONVERSATION}/tasks")
    failed = next(item for item in tasks.json()["items"] if item["task_id"] == task["task_id"])
    assert failed["status"] == "failed"
    assert failed["blocked_reason"] == "TOOL_EXECUTION_FAILED"

    admin = create_engine(ADMIN_URL)
    with admin.connect() as conn:
        case_count = conn.execute(
            text("SELECT count(*) FROM cases WHERE tenant_id = :t"), {"t": TENANT}
        ).scalar_one()
    admin.dispose()
    assert case_count == 2


def _count_start_requests(tenant: str) -> int:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    async def _count() -> int:
        engine = create_async_engine(APP_URL)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant}
                )
                return int(
                    await session.scalar(text("SELECT count(*) FROM standard_flow_start_requests"))
                    or 0
                )
        finally:
            await engine.dispose()

    return asyncio.run(_count(), loop_factory=asyncio.SelectorEventLoop)


def test_start_request_receipts_are_tenant_rls_scoped_and_append_only() -> None:
    response = _start(key="rls-receipt")
    assert response.status_code == 200, response.text
    assert _count_start_requests(TENANT) == 1
    assert _count_start_requests(OTHER_TENANT) == 0

    admin = create_engine(ADMIN_URL)
    with admin.connect() as conn:
        privileges = conn.execute(
            text(
                "SELECT has_table_privilege('platform_app', "
                "'standard_flow_start_requests', 'SELECT'), "
                "has_table_privilege('platform_app', 'standard_flow_start_requests', 'INSERT'), "
                "has_table_privilege('platform_app', 'standard_flow_start_requests', 'UPDATE'), "
                "has_table_privilege('platform_app', 'standard_flow_start_requests', 'DELETE')"
            )
        ).one()
    admin.dispose()
    assert privileges == (True, True, False, False)
