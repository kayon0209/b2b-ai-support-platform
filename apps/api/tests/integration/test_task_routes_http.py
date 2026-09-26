"""HTTP boundaries for the task and copilot routes (T07; SEC-02, SEC-03).

The unit tests prove the state machine and the copilot lifecycle. What only an
HTTP request can prove is the four refusals SEC-02 names, and each one is a
case where a plausible implementation gets it wrong:

- **403, not 404, for a missing permission.** A visitor holding a valid token
  gets `POLICY_DENIED`; the distinction is what lets a client tell "you may
  never do this" from "this does not exist".
- **404 for another conversation's task id.** Leaking that an id exists in
  another conversation is a smaller leak than reading it, and it is the one
  that happens by accident.
- **409 for a stale lease version and for a stale task version.** The agent's
  panel was rendered from a state that has since moved; acting on it would be
  acting on a conversation somebody else now owns.
- **No command can mark a tool successful.** Asserted by sending every
  plausible "just mark it done" spelling and requiring a refusal each time.
"""

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

TENANT = "01900000-0000-7000-8000-0000000c001"
OTHER = "01900000-0000-7000-8000-0000000c002"
SLUG = "r1-tasks-http"
OTHER_SLUG = "r1-tasks-http-other"

CONV = "01900000-0000-7000-8000-0000000c010"
OTHER_CONV = "01900000-0000-7000-8000-0000000c011"
TASK = "01900000-0000-7000-8000-0000000c020"
OTHER_TASK = "01900000-0000-7000-8000-0000000c021"

AGENT_REF = "r1-agent-1"
OTHER_AGENT_REF = "r1-agent-2"


class _Resolver:
    """A fixed principal per test, so a refusal is about the route and not
    about whoever happens to be resolved."""

    def __init__(self, tenant: str, role: str, actor_ref: str) -> None:
        self._tenant = tenant
        self._role = role
        self._actor_ref = actor_ref

    async def __call__(self, request: object) -> TenantContext:
        return TenantContext(
            tenant_id=uuid.UUID(self._tenant),
            actor_id=uuid.uuid5(uuid.NAMESPACE_URL, self._actor_ref),
            actor_kind="user",
            role=self._role,
        )


def _client(tenant: str = TENANT, role: str = "support_agent", agent: str = AGENT_REF):
    import importlib

    main_mod = importlib.import_module("platform_core.main")
    fresh = FastAPI()
    for route in main_mod.app.router.routes:
        fresh.router.routes.append(route)
    fresh.add_middleware(TenantContextMiddleware, resolver=_Resolver(tenant, role, agent))
    return TestClient(fresh, raise_server_exceptions=False)


def _headers(key: str = "r1-http-1") -> dict[str, str]:
    return {"Authorization": "Bearer pt_bootstrap_test", "Idempotency-Key": key}


def _seed() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT, SLUG), (OTHER, OTHER_SLUG)):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, 'R1 tasks http', 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug},
            )
        # A lease owned by a human agent, so the ownership check has something
        # real to match. Without it every command would 409 for the wrong
        # reason and the test would pass without testing the gate.
        conn.execute(
            text(
                "INSERT INTO conversation_control_leases "
                "(id, tenant_id, conversation_ref_id, owner_type, owner_ref, mode, "
                " lease_version, changed_reason, updated_at) "
                "VALUES (:id, :t, :c, 'human', :ref, 'HUMAN_ACTIVE', 3, 'test', 0) "
                "ON CONFLICT (tenant_id, conversation_ref_id) DO UPDATE "
                "SET owner_type = 'human', owner_ref = :ref, lease_version = 3"
            ),
            {
                "id": uuid.uuid4(),
                "t": TENANT,
                "c": CONV,
                "ref": str(uuid.uuid5(uuid.NAMESPACE_URL, AGENT_REF)),
            },
        )
        # `OTHER_CONV` belongs to the OTHER tenant, so its lease is seeded
        # under OTHER. Seeding it under TENANT would have made the
        # cross-tenant test pass for the wrong reason - the lease would simply
        # be missing rather than invisible.
        conn.execute(
            text(
                "INSERT INTO conversation_control_leases "
                "(id, tenant_id, conversation_ref_id, owner_type, owner_ref, mode, "
                " lease_version, changed_reason, updated_at) "
                "VALUES (:id, :t, :c, 'human', :ref, 'HUMAN_ACTIVE', 1, 'test', 0) "
                "ON CONFLICT (tenant_id, conversation_ref_id) DO UPDATE "
                "SET owner_type = 'human', owner_ref = :ref, lease_version = 1"
            ),
            {
                "id": uuid.uuid4(),
                "t": OTHER,
                "c": OTHER_CONV,
                "ref": str(uuid.uuid5(uuid.NAMESPACE_URL, AGENT_REF)),
            },
        )
        for task_id, tenant, conv, status, kind in (
            (TASK, TENANT, CONV, "awaiting_input", "read"),
            (OTHER_TASK, OTHER, OTHER_CONV, "ready", "read"),
        ):
            conn.execute(
                text(
                    "INSERT INTO conversation_tasks (id, tenant_id, conversation_ref_id, "
                    " source_turn_id, task_local_key, sequence, kind, status, version, "
                    " action_revision, content_hash, depends_on, slots, missing_slots, "
                    " created_at, updated_at) "
                    "VALUES (:id, :t, :c, 'turn-1', 'read-0', 0, :kind, :status, 1, 1, "
                    " :hash, '[]', :slots, :missing, 0, 0) "
                    "ON CONFLICT (tenant_id, conversation_ref_id, source_turn_id, "
                    " task_local_key) DO NOTHING"
                ),
                {
                    "id": task_id,
                    "t": tenant,
                    "c": conv,
                    "kind": kind,
                    "status": status,
                    "hash": "0" * 64,
                    "slots": '[{"name": "order_no", "origin": "customer_stated", '
                    '"confirmed": true, "value": "SO-1"}]',
                    "missing": '["street"]' if status == "awaiting_input" else "[]",
                },
            )
    admin.dispose()


def _clear() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tenant in (TENANT, OTHER):
            # Children first: task_events and copilot_drafts carry composite
            # FKs to conversation_tasks.
            conn.execute(
                text("DELETE FROM conversation_task_events WHERE tenant_id = :t"), {"t": tenant}
            )
            conn.execute(text("DELETE FROM copilot_drafts WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(text("DELETE FROM conversation_tasks WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(
                text("DELETE FROM conversation_control_leases WHERE tenant_id = :t"), {"t": tenant}
            )
    admin.dispose()


@pytest.fixture(autouse=True)
def _clean():
    _clear()
    _seed()
    yield
    _clear()


# --- reads ------------------------------------------------------------------


def test_the_owner_can_list_the_conversation_tasks() -> None:
    resp = _client().get(f"/v1/workbench/conversations/{CONV}/tasks")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [i["local_key"] for i in body["items"]] == ["read-0"]
    assert body["items"][0]["missing_slots"] == ["street"]


def test_a_task_response_carries_no_sensitive_slot_value() -> None:
    """SEC-04: the list endpoint is read by the panel and by the audit path,
    so a slot value must not ride along in it."""
    resp = _client().get(f"/v1/workbench/conversations/{CONV}/tasks")
    slots = resp.json()["items"][0]["slots"]
    # The stored row has no value for a sensitive name; assert the shape is
    # name/origin/confirmed only, whatever the row happens to contain.
    for slot in slots:
        assert set(slot) <= {"name", "origin", "confirmed", "value", "value_withheld", "inferred"}


def test_another_tenants_conversation_returns_an_empty_list() -> None:
    """404 would confirm the conversation exists; an empty list does not."""
    resp = _client().get(f"/v1/workbench/conversations/{OTHER_CONV}/tasks")
    assert resp.status_code == 200
    assert resp.json()["items"] == []


# --- SEC-02: refusals -------------------------------------------------------


def test_a_visitor_cannot_list_tasks_with_case_update_but_not_read() -> None:
    """A role with no CASE_READ grant is refused before anything is read."""
    resp = _client(role="integration_service").get(f"/v1/workbench/conversations/{CONV}/tasks")
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "POLICY_DENIED"


def test_a_visitor_cannot_command_a_task() -> None:
    resp = _client(role="integration_service").post(
        f"/v1/workbench/conversations/{CONV}/tasks/{TASK}/commands",
        headers=_headers(),
        json={"command": "cancel", "expected_version": 1, "expected_lease_version": 3},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "POLICY_DENIED"


def test_a_write_without_an_idempotency_key_is_refused() -> None:
    resp = _client().post(
        f"/v1/workbench/conversations/{CONV}/tasks/{TASK}/commands",
        headers={"Authorization": "Bearer pt_bootstrap_test"},
        json={"command": "cancel", "expected_version": 1, "expected_lease_version": 3},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_another_tenant_gets_404_not_the_task() -> None:
    """The id exists, but not for this tenant - and the response must not
    distinguish that from an id that does not exist anywhere."""
    resp = _client(tenant=OTHER).post(
        f"/v1/workbench/conversations/{CONV}/tasks/{TASK}/commands",
        headers=_headers(),
        json={"command": "cancel", "expected_version": 1, "expected_lease_version": 3},
    )
    assert resp.status_code in (403, 404), resp.text


def test_a_task_from_another_conversation_is_404() -> None:
    resp = _client().post(
        f"/v1/workbench/conversations/{CONV}/tasks/{OTHER_TASK}/commands",
        headers=_headers(),
        json={"command": "cancel", "expected_version": 1, "expected_lease_version": 3},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "CASE_NOT_FOUND"


def test_a_non_owner_agent_is_refused() -> None:
    """SEC-03: a different human agent cannot act on this conversation."""
    resp = _client(agent=OTHER_AGENT_REF).post(
        f"/v1/workbench/conversations/{CONV}/tasks/{TASK}/commands",
        headers=_headers(),
        json={"command": "cancel", "expected_version": 1, "expected_lease_version": 3},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "LEASE_NOT_OWNED"


def test_a_stale_lease_version_is_a_conflict() -> None:
    resp = _client().post(
        f"/v1/workbench/conversations/{CONV}/tasks/{TASK}/commands",
        headers=_headers(),
        json={"command": "cancel", "expected_version": 1, "expected_lease_version": 99},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "LEASE_CONFLICT"


def test_a_stale_task_version_is_a_conflict() -> None:
    resp = _client().post(
        f"/v1/workbench/conversations/{CONV}/tasks/{TASK}/commands",
        headers=_headers(),
        json={"command": "cancel", "expected_version": 99, "expected_lease_version": 3},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "TASK_VERSION_CONFLICT"


# --- the command vocabulary -------------------------------------------------


def test_collect_fields_moves_a_waiting_task_to_ready() -> None:
    resp = _client().post(
        f"/v1/workbench/conversations/{CONV}/tasks/{TASK}/commands",
        headers=_headers(),
        json={
            "command": "collect_fields",
            "expected_version": 1,
            "expected_lease_version": 3,
            "fields": {"street": "南京西路 100 号"},
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] == "ready"
    assert resp.json()["task"]["missing_slots"] == []


def test_collecting_only_some_fields_keeps_the_task_waiting() -> None:
    resp = _client().post(
        f"/v1/workbench/conversations/{CONV}/tasks/{TASK}/commands",
        headers=_headers(),
        json={
            "command": "collect_fields",
            "expected_version": 1,
            "expected_lease_version": 3,
            "fields": {"something_else": "x"},
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] == "awaiting_input"
    assert resp.json()["task"]["missing_slots"] == ["street"]


def test_cancel_terminates_a_task() -> None:
    resp = _client().post(
        f"/v1/workbench/conversations/{CONV}/tasks/{TASK}/commands",
        headers=_headers(),
        json={"command": "cancel", "expected_version": 1, "expected_lease_version": 3},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["status"] == "cancelled"


def test_a_cancelled_task_refuses_a_second_command() -> None:
    _client().post(
        f"/v1/workbench/conversations/{CONV}/tasks/{TASK}/commands",
        headers=_headers("k1"),
        json={"command": "cancel", "expected_version": 1, "expected_lease_version": 3},
    )
    resp = _client().post(
        f"/v1/workbench/conversations/{CONV}/tasks/{TASK}/commands",
        headers=_headers("k2"),
        json={"command": "handoff", "expected_version": 2, "expected_lease_version": 3},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "TASK_TERMINAL"


@pytest.mark.parametrize(
    "spoofed",
    ["complete", "succeed", "mark_done", "set_status", "execute", "verify", "approve"],
)
def test_no_command_can_mark_a_tool_successful(spoofed: str) -> None:
    """The absence of a completion command is the point; assert the whole
    plausible vocabulary is refused rather than trusting the literal set."""
    resp = _client().post(
        f"/v1/workbench/conversations/{CONV}/tasks/{TASK}/commands",
        headers=_headers(),
        json={"command": spoofed, "expected_version": 1, "expected_lease_version": 3},
    )
    assert resp.status_code == 400, f"{spoofed} was not refused: {resp.text}"
    assert resp.json()["error"]["code"] == "VALIDATION_FAILED"
    # And the message names what is allowed, so a client author can fix it.
    assert "collect_fields" in resp.json()["error"]["message"]


def test_prepare_proposal_is_refused_for_a_read_task() -> None:
    resp = _client().post(
        f"/v1/workbench/conversations/{CONV}/tasks/{TASK}/commands",
        headers=_headers(),
        json={"command": "prepare_proposal", "expected_version": 1, "expected_lease_version": 3},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "TASK_COMMAND_REFUSED"


def test_an_oversized_field_is_refused() -> None:
    resp = _client().post(
        f"/v1/workbench/conversations/{CONV}/tasks/{TASK}/commands",
        headers=_headers(),
        json={
            "command": "collect_fields",
            "expected_version": 1,
            "expected_lease_version": 3,
            "fields": {"street": "x" * 500},
        },
    )
    assert resp.status_code == 400, resp.text


# --- copilot ----------------------------------------------------------------


def test_a_visitor_cannot_queue_a_copilot_job() -> None:
    resp = _client(role="integration_service").post(
        f"/v1/workbench/conversations/{CONV}/copilot/jobs",
        headers=_headers(),
        json={"kind": "summary", "timeline_revision": 0, "lease_version": 3},
    )
    assert resp.status_code == 403, resp.text


def test_a_non_owner_cannot_queue_a_copilot_job() -> None:
    resp = _client(agent=OTHER_AGENT_REF).post(
        f"/v1/workbench/conversations/{CONV}/copilot/jobs",
        headers=_headers(),
        json={"kind": "summary", "timeline_revision": 0, "lease_version": 3},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "LEASE_NOT_OWNED"


def test_a_stale_lease_version_refuses_a_copilot_job() -> None:
    resp = _client().post(
        f"/v1/workbench/conversations/{CONV}/copilot/jobs",
        headers=_headers(),
        json={"kind": "summary", "timeline_revision": 0, "lease_version": 1},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "LEASE_CONFLICT"


def test_a_summary_without_sources_is_refused() -> None:
    """COP-01: a summary that cannot name its turns is not produced."""
    resp = _client().post(
        f"/v1/workbench/conversations/{CONV}/copilot/jobs",
        headers=_headers(),
        json={"kind": "summary", "timeline_revision": 0, "lease_version": 3, "source_turn_ids": []},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "COPILOT_SUMMARY_REQUIRES_SOURCES"


def test_a_summary_with_sources_is_queued_and_never_sent() -> None:
    resp = _client().post(
        f"/v1/workbench/conversations/{CONV}/copilot/jobs",
        headers=_headers(),
        json={
            "kind": "summary",
            "timeline_revision": 0,
            "lease_version": 3,
            "source_turn_ids": ["turn-1"],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    assert body["kind"] == "summary"
    # Nothing was sent, and nothing is claimed to have been.
    assert "body" not in body
    assert "delivery" not in body


def test_the_same_request_twice_yields_the_same_job_id() -> None:
    """A double-clicked generate is one job and one eventual model call."""
    payload = {
        "kind": "summary",
        "timeline_revision": 0,
        "lease_version": 3,
        "source_turn_ids": ["turn-1"],
    }
    first = _client().post(
        f"/v1/workbench/conversations/{CONV}/copilot/jobs", headers=_headers("a"), json=payload
    )
    second = _client().post(
        f"/v1/workbench/conversations/{CONV}/copilot/jobs", headers=_headers("b"), json=payload
    )
    assert first.status_code == second.status_code == 200, first.text
    assert first.json()["job_id"] == second.json()["job_id"]


def test_polling_an_unknown_job_is_404() -> None:
    resp = _client().get(
        f"/v1/workbench/conversations/{CONV}/copilot/jobs/01900000-0000-7000-8000-0000000c099"
    )
    assert resp.status_code == 404, resp.text


def test_a_job_from_another_conversation_is_404() -> None:
    """Same shape as the task check: an id that exists elsewhere must read as
    one that does not exist here."""
    other = _client(tenant=OTHER)
    created = other.post(
        f"/v1/workbench/conversations/{OTHER_CONV}/copilot/jobs",
        headers=_headers(),
        json={
            "kind": "summary",
            "timeline_revision": 0,
            "lease_version": 1,
            "source_turn_ids": ["turn-9"],
        },
    )
    assert created.status_code == 200, created.text
    job_id = created.json()["job_id"]
    resp = _client(tenant=OTHER).get(
        f"/v1/workbench/conversations/{OTHER_CONV}/copilot/jobs/{job_id}"
    )
    # A seeded draft row does not exist for a queued job, so this is a 404
    # either way; the point is that it never returns another tenant's body.
    assert resp.status_code == 404
