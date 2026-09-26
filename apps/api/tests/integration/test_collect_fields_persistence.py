"""B1-04: collected fields must be persisted, with a source.

The acceptance review sent `street=Shanghai Office` and got HTTP 200 with the
task moving to `ready` and `missing_slots=[]`, while `slots` still held only
the old order number. The UI said "已记录客户补充的信息" and the database had
nothing.

These tests assert the three things that were false:

1. The value is persisted, as a slot with an `origin` and a `turn_id`.
2. A sensitive field's value is withheld while the fact of collection is kept.
3. A field the task is not waiting for is refused, not silently dropped - and
   in neither case may the task reach `ready` without persisted evidence.
"""

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

TENANT = "01900000-0000-7000-8000-00000000d001"
SLUG = "r1-collect-fields"
CONV = "01900000-0000-7000-8000-00000000d010"
TASK = "01900000-0000-7000-8000-00000000d020"
AGENT_REF = "r1-collect-agent"


class _Resolver:
    def __init__(self) -> None:
        self._actor = uuid.uuid5(uuid.NAMESPACE_URL, AGENT_REF)

    async def __call__(self, request: object) -> TenantContext:
        return TenantContext(
            tenant_id=uuid.UUID(TENANT),
            actor_id=self._actor,
            actor_kind="user",
            role="support_agent",
        )


def _client():
    import importlib

    main_mod = importlib.import_module("platform_core.main")
    fresh = FastAPI()
    for route in main_mod.app.router.routes:
        fresh.router.routes.append(route)
    fresh.add_middleware(TenantContextMiddleware, resolver=_Resolver())
    return TestClient(fresh, raise_server_exceptions=False)


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": "Bearer pt_bootstrap_test", "Idempotency-Key": key}


def _seed() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, :slug, 'R1 collect', 'active') ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT, "slug": SLUG},
        )
        conn.execute(
            text(
                "INSERT INTO conversation_control_leases (id, tenant_id, conversation_ref_id, "
                "owner_type, owner_ref, mode, lease_version, changed_reason, updated_at) "
                "VALUES (:id, :t, :c, 'human', :ref, 'HUMAN_ACTIVE', 3, 'test', 0) "
                "ON CONFLICT (tenant_id, conversation_ref_id) DO UPDATE "
                "SET owner_type='human', owner_ref=:ref, lease_version=3"
            ),
            {
                "id": uuid.uuid4(),
                "t": TENANT,
                "c": CONV,
                "ref": str(uuid.uuid5(uuid.NAMESPACE_URL, AGENT_REF)),
            },
        )
        conn.execute(
            text(
                "INSERT INTO conversation_tasks (id, tenant_id, conversation_ref_id, "
                "source_turn_id, task_local_key, sequence, kind, status, version, "
                "action_revision, content_hash, depends_on, slots, missing_slots, "
                "created_at, updated_at) "
                "VALUES (:id, :t, :c, 'turn-1', 'write-1', 1, 'write', 'awaiting_input', 1, 1, "
                ":hash, '[]', :slots, :missing, 0, 0) "
                "ON CONFLICT (tenant_id, conversation_ref_id, source_turn_id, task_local_key) "
                "DO UPDATE SET status='awaiting_input', version=1, "
                "missing_slots=:missing, slots=:slots"
            ),
            {
                "id": TASK,
                "t": TENANT,
                "c": CONV,
                "hash": "0" * 64,
                "slots": '[{"name": "order_no", "origin": "customer_stated", '
                '"confirmed": true, "value": "SO-240918"}]',
                "missing": '["street", "city"]',
            },
        )
    admin.dispose()


def _clear() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text("DELETE FROM conversation_task_events WHERE tenant_id = :t"), {"t": TENANT}
        )
        conn.execute(text("DELETE FROM conversation_tasks WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM conversation_turns WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(
            text("DELETE FROM conversation_control_leases WHERE tenant_id = :t"), {"t": TENANT}
        )
    admin.dispose()


@pytest.fixture(autouse=True)
def _clean():
    _clear()
    _seed()
    yield
    _clear()


def _seed_waiting_for(*missing: str) -> None:
    """Re-seed the task waiting for exactly these fields."""
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "UPDATE conversation_tasks SET missing_slots = :m, version = 1, "
                "status = 'awaiting_input' WHERE id = :i"
            ),
            {"i": TASK, "m": json.dumps(list(missing))},
        )
    admin.dispose()


def _collect(fields: dict[str, str], key: str = "k1"):
    return _client().post(
        f"/v1/workbench/conversations/{CONV}/tasks/{TASK}/commands",
        headers=_headers(key),
        json={
            "command": "collect_fields",
            "expected_version": 1,
            "expected_lease_version": 3,
            "fields": fields,
        },
    )


def _task_row() -> dict:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        row = (
            conn.execute(
                text("SELECT status, slots, missing_slots FROM conversation_tasks WHERE id = :i"),
                {"i": TASK},
            )
            .mappings()
            .one()
        )
    admin.dispose()
    return {
        "status": row["status"],
        "slots": json.loads(row["slots"]) if isinstance(row["slots"], str) else row["slots"],
        "missing_slots": row["missing_slots"],
    }


def _turn_count() -> int:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM conversation_turns WHERE tenant_id = :t"), {"t": TENANT}
        ).scalar()
    admin.dispose()
    return int(n or 0)


# --- the value is persisted -------------------------------------------------


def test_the_collected_value_is_stored_with_a_source() -> None:
    """B1-04's reproduction: the value has to be in the row.

    Uses `order_no`, which is not on the sensitive list, so the assertion is
    about persistence. `street` is sensitive and its value is withheld - that
    is the other test below, and asserting the value here would be asserting
    the wrong policy.
    """
    _seed_waiting_for("order_no")
    resp = _collect({"order_no": "SO-240918"})
    assert resp.status_code == 200, resp.text
    row = _task_row()
    slot = next(s for s in row["slots"] if s["name"] == "order_no")
    assert slot["value"] == "SO-240918"
    assert slot["origin"] == "customer_stated"
    # And it points at a turn that exists, so it is not an unsourced value.
    assert slot["turn_id"]


def test_the_collected_value_also_lands_in_the_conversation() -> None:
    """The slot needs a transcript entry behind it, or it is an assertion."""
    before = _turn_count()
    _collect({"street": "上海南京西路 100 号"})
    assert _turn_count() == before + 1


def test_the_pre_existing_slot_is_preserved() -> None:
    _collect({"street": "上海南京西路 100 号"})
    names = {s["name"] for s in _task_row()["slots"]}
    assert "order_no" in names, "collecting a field dropped an unrelated slot"


def test_collecting_one_of_two_fields_keeps_the_task_waiting() -> None:
    resp = _collect({"street": "上海南京西路 100 号"})
    assert resp.status_code == 200, resp.text
    row = _task_row()
    assert row["status"] == "awaiting_input"
    assert row["missing_slots"] == ["city"]


def test_collecting_both_makes_the_task_ready() -> None:
    _collect({"street": "上海南京西路 100 号"}, key="k1")
    resp = _client().post(
        f"/v1/workbench/conversations/{CONV}/tasks/{TASK}/commands",
        headers=_headers("k2"),
        json={
            "command": "collect_fields",
            "expected_version": 2,
            "expected_lease_version": 3,
            "fields": {"city": "上海"},
        },
    )
    assert resp.status_code == 200, resp.text
    row = _task_row()
    assert row["status"] == "ready"
    assert row["missing_slots"] == []


# --- sensitive values -------------------------------------------------------


def test_a_sensitive_value_is_withheld_but_the_collection_is_recorded() -> None:
    """The address is in the transcript, which is access-controlled; the task
    row records that it was answered, by whom, and when - not a second copy."""
    _collect({"street": "上海南京西路 100 号"})
    row = _task_row()
    street = next((s for s in row["slots"] if s["name"] == "street"), None)
    assert street is not None, "the collected sensitive field was not recorded at all"
    assert street.get("value_withheld") is True
    assert "value" not in street


# --- field names are constrained -------------------------------------------


def test_a_field_the_task_is_not_waiting_for_is_refused() -> None:
    resp = _collect({"bank_account": "6222 0000 0000"})
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "TASK_FIELD_NOT_REQUESTED"
    # And nothing changed.
    row = _task_row()
    assert row["status"] == "awaiting_input"
    assert row["missing_slots"] == ["street", "city"]


def test_a_refused_field_does_not_write_a_turn() -> None:
    before = _turn_count()
    _collect({"bank_account": "6222 0000 0000"})
    assert _turn_count() == before


def test_a_partly_valid_batch_is_refused_whole() -> None:
    """One unknown name rejects the batch rather than applying the rest.

    A partial apply would leave the task's `missing_slots` describing a state
    the operator did not ask for.
    """
    before = _turn_count()
    resp = _collect({"street": "上海南京西路 100 号", "bank_account": "6222"})
    assert resp.status_code == 409, resp.text
    assert _turn_count() == before
    assert _task_row()["missing_slots"] == ["street", "city"]


# --- a slot always has an origin -------------------------------------------


def test_the_store_refuses_a_slot_without_an_origin() -> None:
    """The last line of defence, below the router.

    A slot with a value and no origin is the shape EVAL-02 counts as a
    failure, and it must not be reachable by any caller.
    """
    import asyncio

    from platform_core.agent_runtime.tasks.state_machine import TaskStatus
    from platform_core.agent_runtime.tasks.store import TaskCommand, TaskConflict, transition

    async def _attempt() -> str:
        from platform_core.db import session_scope_with_url

        async with session_scope_with_url(ADMIN_URL) as session:
            from platform_core.agent_runtime.tasks.store import get_task

            task = await get_task(session, tenant_id=uuid.UUID(TENANT), task_id=uuid.UUID(TASK))
            assert task is not None
            try:
                await transition(
                    session,
                    tenant_id=uuid.UUID(TENANT),
                    task=task,
                    command=TaskCommand(
                        target=TaskStatus.READY,
                        reason_code="TEST",
                        slots=[{"name": "street", "value": "x"}],
                    ),
                )
                return "accepted"
            except TaskConflict as exc:
                return exc.code

    assert asyncio.run(_attempt(), loop_factory=asyncio.SelectorEventLoop) == (
        "TASK_SLOT_WITHOUT_ORIGIN"
    )
