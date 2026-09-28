"""HTTP and PostgreSQL guarantees for operator-started standard flows."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Any

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


def _client(
    tenant: str = TENANT,
    *,
    agent: str = AGENT_REF,
    role: str = "support_agent",
) -> TestClient:
    import importlib

    main_mod = importlib.import_module("platform_core.main")
    fresh = FastAPI()
    for route in main_mod.app.router.routes:
        fresh.router.routes.append(route)
    fresh.add_middleware(
        TenantContextMiddleware,
        resolver=_Resolver(tenant=tenant, agent=agent, role=role),
    )
    return TestClient(fresh, raise_server_exceptions=False)


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
            conn.execute(text("DELETE FROM tool_definitions WHERE tenant_id = :t"), {"t": tenant})
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
                "'{}'::jsonb, 1, 1)"
            ),
            {"id": account_id, "t": TENANT},
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
    started = _start()
    task = started.json()["task"]
    collected = _client().post(
        f"/v1/workbench/conversations/{CONVERSATION}/tasks/{task['task_id']}/commands",
        headers=_headers("flow-collect-1"),
        json={
            "command": "collect_fields",
            "expected_version": task["version"],
            "expected_lease_version": LEASE_VERSION,
            "fields": {"order_id": "SO-1234"},
        },
    )
    assert collected.status_code == 200, collected.text
    updated = collected.json()["task"]
    assert updated["status"] == "manual_flow"
    assert updated["missing_slots"] == []
    assert updated["slots"][0]["origin"] == "agent_collected"
    assert updated["slots"][0]["value"] == "SO-1234"

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
            "fields": {"product_ref": "SKU-1"},
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
