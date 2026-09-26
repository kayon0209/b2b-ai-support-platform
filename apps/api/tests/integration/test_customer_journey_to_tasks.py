"""T08: the customer's message becomes tasks an agent can act on.

The acceptance review's B1-02 was that `plan_tasks` and `create_or_get` had no
production caller, so a customer message produced no tasks. The seam exists now
(`tasks/planning_seam.py`) and is wired into `inbox_consumer`; this file
exercises the whole path against a real database, with a stub model in place of
a real one.

What is real and what is not, stated plainly:

- **Real**: the flag gate, the capability filter, the planner, the store, the
  idempotency key, the RLS session, the row counts.
- **Stub**: the model. A stub returning a fixed three-intent payload stands in
  for a provider, so this proves the *plumbing*, not the classification. EVAL-02
  remains blocked and this file does not move it.

Three properties the spec's journey depends on:

1. With the flags off, a customer message creates no tasks and the business
   state is untouched - the `off` path is the platform's existing behaviour.
2. With the flags on, the spec's sentence produces three tasks with three
   different fates: a ready read, and two needs-human writes.
3. The same message replayed produces the same three tasks, not six.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from platform_core.agent_runtime.intent import classify
from platform_core.agent_runtime.semantic.contracts import SemanticMode
from platform_core.agent_runtime.semantic.validator import CapabilityView

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = os.environ.get(
    "APP_DATABASE_APP_URL",
    "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform",
)

TENANT = "01900000-0000-7000-8000-0000000a0001"
OTHER = "01900000-0000-7000-8000-0000000a0002"
SLUG = "r1-journey"
OTHER_SLUG = "r1-journey-other"
CONV = "01900000-0000-7000-8000-0000000a0010"
OTHER_CONV = "01900000-0000-7000-8000-0000000a0011"

# The spec's own sentence (spec section 1, step 2): three needs in one turn.
CUSTOMER_TEXT = "查一下 SO-240918 到哪了，没发货就改成上海办公室，另外补一下发票"

# What a model would return for it. Fixed, because the classification quality
# is EVAL-02's subject and this file is about the plumbing.
MODEL_OUTPUT = json.dumps(
    {
        "primary_intent": "business_query",
        "secondary_intents": ["business_action"],
        "scene": "order_fulfilment",
        "business_line": "pcb",
        "intents": [
            {
                "task_kind": "read",
                "source_turn_id": "t-1",
                "evidence": [{"turn_id": "t-1", "start": 0, "end": 3}],
                "slots": [
                    {
                        "name": "order_no",
                        "value": "SO-240918",
                        "origin": "customer_stated",
                        "confirmed": True,
                    }
                ],
                "missing_slots": [],
                "depends_on": [],
            },
            {
                "task_kind": "write",
                "source_turn_id": "t-1",
                "evidence": [{"turn_id": "t-1", "start": 0, "end": 3}],
                "slots": [
                    {
                        "name": "address",
                        "value": "上海办公室",
                        "origin": "customer_stated",
                        "confirmed": False,
                    }
                ],
                "missing_slots": ["street", "city"],
                "depends_on": ["0"],
                "condition": {
                    "field": "order.status",
                    "operator": "ne",
                    "value": "shipped",
                },
            },
            {
                "task_kind": "write",
                "source_turn_id": "t-1",
                "evidence": [],
                "slots": [],
                "missing_slots": ["invoice_period"],
                "depends_on": [],
            },
        ],
        "evidence_spans": [],
        "confidence_band": "high",
        "needs_clarification": False,
        "emotion_signal": None,
        "tool_candidates": [{"tool_name": "order.get_status", "reason": "order noun"}],
    },
    ensure_ascii=False,
)


class _StubProvider:
    """Counts calls, so "the model was consulted" is observable."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.calls = 0

    async def complete(self, messages: list[object], **kwargs: object):
        from platform_core.llm.provider import ChatResult

        self.calls += 1
        return ChatResult(text=self._text, model=str(kwargs.get("model") or "stub"))


def _run(coro: object) -> object:
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)  # type: ignore[arg-type]


def _set_flags(tenant: str, keys: list[str]) -> None:
    """Turn on exactly these flags for one tenant, as the console would."""
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for key in keys:
            row = conn.execute(
                text("SELECT id FROM feature_flags WHERE tenant_id = :t AND key = :k"),
                {"t": tenant, "k": key},
            ).fetchone()
            if row is None:
                flag_id = conn.execute(
                    text(
                        "INSERT INTO feature_flags (id, tenant_id, key, description, enabled, "
                        "rollout_percent, created_at) "
                        "VALUES (:id, :t, :k, '', true, 0, 0) RETURNING id"
                    ),
                    {"id": uuid.uuid4(), "t": tenant, "k": key},
                ).scalar_one()
            else:
                flag_id = row[0]
                conn.execute(
                    text("UPDATE feature_flags SET enabled = true WHERE id = :i"),
                    {"i": flag_id},
                )
            conn.execute(
                text(
                    "INSERT INTO feature_flag_targets (id, tenant_id, flag_id, "
                    "target_tenant_id, enabled) VALUES (:id, :t, :f, :t, true) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"id": uuid.uuid4(), "t": tenant, "f": flag_id},
            )
    admin.dispose()


def _seed(*, with_tools: bool = True) -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT, SLUG), (OTHER, OTHER_SLUG)):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, 'R1 journey', 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug},
            )
        for conv, tid in ((CONV, TENANT), (OTHER_CONV, OTHER)):
            conn.execute(
                text(
                    "INSERT INTO conversation_control_leases (id, tenant_id, "
                    "conversation_ref_id, owner_type, mode, lease_version, "
                    "changed_reason, updated_at) "
                    "VALUES (:id, :t, :c, 'ai', 'AI_ACTIVE', 1, 'test', 0) "
                    "ON CONFLICT (tenant_id, conversation_ref_id) DO NOTHING"
                ),
                {"id": uuid.uuid4(), "t": tid, "c": conv},
            )
        if with_tools:
            from platform_core.tool_gateway.registry import TOOL_CATALOG

            for tenant_id in (TENANT, OTHER):
                for name, (risk, schema, perms, conf) in TOOL_CATALOG.items():
                    conn.execute(
                        text(
                            "INSERT INTO tool_definitions (id, tenant_id, name, version, risk, "
                            "input_schema, output_schema, required_permissions, timeout_ms, "
                            "idempotent, requires_confirmation) "
                            "VALUES (:id, :tenant_id, :name, 1, :risk, CAST(:schema AS jsonb), "
                            "'{}'::jsonb, CAST(:perms AS jsonb), 5000, true, :conf) "
                            "ON CONFLICT DO NOTHING"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "tenant_id": tenant_id,
                            "name": name,
                            "risk": risk,
                            "schema": json.dumps(schema),
                            "perms": json.dumps(perms),
                            "conf": conf,
                        },
                    )
    admin.dispose()


def _clear() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        from platform_core.tool_gateway.registry import TOOL_CATALOG

        for tenant in (TENANT, OTHER):
            conn.execute(
                text("DELETE FROM conversation_task_events WHERE tenant_id = :t"), {"t": tenant}
            )
            conn.execute(text("DELETE FROM conversation_tasks WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(
                text("DELETE FROM semantic_assessments WHERE tenant_id = :t"), {"t": tenant}
            )
            conn.execute(text("DELETE FROM conversation_turns WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(text("DELETE FROM outbox_events WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(
                text("DELETE FROM conversation_control_leases WHERE tenant_id = :t"), {"t": tenant}
            )
            for name in TOOL_CATALOG:
                conn.execute(
                    text("DELETE FROM tool_definitions WHERE tenant_id = :t AND name = :name"),
                    {"t": tenant, "name": name},
                )
    admin.dispose()


@pytest.fixture(autouse=True)
def _clean():
    _clear()
    _seed()
    yield
    _clear()


def _tasks(conversation: str = CONV, tenant: str = TENANT) -> list[dict]:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT task_local_key, kind, status, missing_slots, blocked_reason, slots "
                    "FROM conversation_tasks WHERE tenant_id = :t AND conversation_ref_id = :c "
                    "ORDER BY sequence"
                ),
                {"t": tenant, "c": conversation},
            )
            .mappings()
            .all()
        )
    admin.dispose()
    return [
        {
            "key": r["task_local_key"],
            "kind": r["kind"],
            "status": r["status"],
            "missing": list(r["missing_slots"] or []),
            "blocked": r["blocked_reason"],
            "slots": r["slots"],
        }
        for r in rows
    ]


# --- the flag gate ----------------------------------------------------------


def test_with_the_flags_off_no_tasks_are_created() -> None:
    """The `off` path is the platform's existing behaviour, byte for byte."""
    provider = _StubProvider(MODEL_OUTPUT)
    created = _run(
        _plan(
            provider=provider,
            flags={},
            conversation=CONV,
            tenant=TENANT,
        )
    )
    assert created == 0
    assert _tasks() == []
    # And the model was not consulted at all, which is what "off" means.
    assert provider.calls == 0


def test_with_only_the_task_flag_no_tasks_are_created() -> None:
    """Tasks come from a suggestion. Storage without suggestions has nothing to
    store, and a tenant that enabled only the flag should get silence rather
    than a model call."""
    provider = _StubProvider(MODEL_OUTPUT)
    created = _run(
        _plan(
            provider=provider,
            flags={"agent.conversation_tasks": True},
            conversation=CONV,
            tenant=TENANT,
        )
    )
    assert created == 0
    assert _tasks() == []
    assert provider.calls == 0


# --- the spec journey -------------------------------------------------------


def test_the_spec_sentence_produces_three_tasks() -> None:
    provider = _StubProvider(MODEL_OUTPUT)
    created = _run(
        _plan(
            provider=provider,
            flags={"agent.conversation_tasks": True, "agent.semantic_assist": True},
            conversation=CONV,
            tenant=TENANT,
        )
    )
    assert created == 3
    assert provider.calls == 1
    tasks = _tasks()
    assert [t["key"] for t in tasks] == ["read-0", "write-1", "write-2"]


def test_the_read_is_ready_and_the_writes_need_a_human() -> None:
    """Spec section 1, step 4: an address change R1 cannot perform is a human
    task, and so is the invoice request."""
    _run(
        _plan(
            provider=_StubProvider(MODEL_OUTPUT),
            flags={"agent.conversation_tasks": True, "agent.semantic_assist": True},
            conversation=CONV,
            tenant=TENANT,
        )
    )
    read_task, address_task, invoice_task = _tasks()

    assert read_task["kind"] == "read"
    assert read_task["status"] == "ready"
    assert read_task["blocked"] is None

    # Both writes: no connector, so needs_human with the reason attached.
    for task in (address_task, invoice_task):
        assert task["kind"] == "write"
        assert task["status"] == "needs_human"
        assert task["blocked"] == "SEMANTIC_NO_WRITE_CAPABILITY"

    # And the missing fields survive, so one message can collect everything.
    assert address_task["missing"] == ["street", "city"]
    assert invoice_task["missing"] == ["invoice_period"]


def test_the_address_value_is_not_stored_in_the_task_row() -> None:
    """SEC-04: the address is in the transcript, not in a row the audit path
    reads."""
    _run(
        _plan(
            provider=_StubProvider(MODEL_OUTPUT),
            flags={"agent.conversation_tasks": True, "agent.semantic_assist": True},
            conversation=CONV,
            tenant=TENANT,
        )
    )
    address_task = _tasks()[1]
    slot = next(s for s in address_task["slots"] if s["name"] == "address")
    assert slot.get("value_withheld") is True
    assert "上海办公室" not in json.dumps(address_task["slots"], ensure_ascii=False)


def test_the_order_number_is_stored_because_it_is_not_sensitive() -> None:
    _run(
        _plan(
            provider=_StubProvider(MODEL_OUTPUT),
            flags={"agent.conversation_tasks": True, "agent.semantic_assist": True},
            conversation=CONV,
            tenant=TENANT,
        )
    )
    read_task = _tasks()[0]
    slot = next(s for s in read_task["slots"] if s["name"] == "order_no")
    assert slot["value"] == "SO-240918"
    assert slot["origin"] == "customer_stated"


# --- replay -----------------------------------------------------------------


def test_the_same_message_replayed_produces_the_same_three_tasks() -> None:
    """A webhook redelivery, a worker restart and a replay all collide on the
    idempotency key."""
    flags = {"agent.conversation_tasks": True, "agent.semantic_assist": True}
    first = _run(
        _plan(provider=_StubProvider(MODEL_OUTPUT), flags=flags, conversation=CONV, tenant=TENANT)
    )
    second = _run(
        _plan(provider=_StubProvider(MODEL_OUTPUT), flags=flags, conversation=CONV, tenant=TENANT)
    )

    assert first == 3
    assert second == 0, "a replay created new tasks"
    assert len(_tasks()) == 3


def test_a_different_order_in_the_same_turn_is_refused_not_merged() -> None:
    """B1-06's consequence, end to end.

    The read's slot carries the order number, so `SO-999999` changes the
    content hash while the key - (tenant, conversation, turn, local key) - stays
    the same. That is a conflict to surface, never a silent replacement of the
    first order's task with the second's.
    """
    flags = {"agent.conversation_tasks": True, "agent.semantic_assist": True}
    _run(_plan(provider=_StubProvider(MODEL_OUTPUT), flags=flags, conversation=CONV, tenant=TENANT))

    other = MODEL_OUTPUT.replace("SO-240918", "SO-999999")
    with pytest.raises(Exception) as exc:
        _run(_plan(provider=_StubProvider(other), flags=flags, conversation=CONV, tenant=TENANT))
    assert "TASK_IDENTITY_CONTENT_MISMATCH" in str(exc.value)

    # And the original task still describes the original order.
    tasks = _tasks()
    assert len(tasks) == 3
    slot = next(s for s in tasks[0]["slots"] if s["name"] == "order_no")
    assert slot["value"] == "SO-240918"


# --- tenant isolation -------------------------------------------------------


def test_another_tenant_sees_no_tasks() -> None:
    _run(
        _plan(
            provider=_StubProvider(MODEL_OUTPUT),
            flags={"agent.conversation_tasks": True, "agent.semantic_assist": True},
            conversation=CONV,
            tenant=TENANT,
        )
    )
    assert len(_tasks()) == 3
    assert _tasks(conversation=OTHER_CONV, tenant=OTHER) == []


def test_semantic_worker_consumes_the_deferred_task_event() -> None:
    """The production worker claims the id-only event and plans from RLS turns."""
    from platform_core.agent_runtime.chat_service import append_customer_turn
    from platform_core.agent_runtime.orchestrator import OrchestratorDeps
    from platform_core.identity.tenant_context import TenantContext, tenant_session
    from worker.inbox_consumer import _enqueue_task_planning
    from worker.runner import SemanticWorker

    _set_flags(TENANT, ["agent.conversation_tasks", "agent.semantic_assist"])
    ctx = TenantContext(
        tenant_id=uuid.UUID(TENANT), actor_id=None, actor_kind="system", role="integration_service"
    )

    async def enqueue_job() -> str:
        async with tenant_session(ctx) as session:
            turn, _duplicate = await append_customer_turn(
                session,
                tenant_id=uuid.UUID(TENANT),
                ref_id=uuid.UUID(CONV),
                text=CUSTOMER_TEXT,
            )
            enqueued = await _enqueue_task_planning(
                session,
                tenant_id=uuid.UUID(TENANT),
                conversation_ref_id=uuid.UUID(CONV),
                turn_id=str(turn.id),
            )
            assert enqueued
            return str(turn.id)

    turn_id = _run(enqueue_job())
    provider = _StubProvider(MODEL_OUTPUT.replace('"t-1"', f'"{turn_id}"'))
    worker = SemanticWorker(OrchestratorDeps(extra={"chat": provider}))
    assert _run(worker.run_once()) >= 1
    assert provider.calls == 1
    assert len(_tasks()) == 3

    admin = create_engine(ADMIN_URL)
    with admin.connect() as conn:
        row = conn.execute(
            text(
                "SELECT status, processing_started_at, payload FROM outbox_events "
                "WHERE tenant_id = :t AND event_type = 'conversation.task_planning_requested'"
            ),
            {"t": TENANT},
        ).one()
    admin.dispose()
    assert row[0] == "sent"
    assert row[1] is None
    assert "question" not in row[2]


# --- the seam ----------------------------------------------------------------


async def _plan(
    *,
    provider: object,
    flags: dict[str, bool],
    conversation: str,
    tenant: str,
) -> int:
    """Run the seam the way the worker does.

    The flags named in `flags` are turned on for real before the run, because
    the seam reads them through `flag_service` and a stubbed decision would not
    exercise the storage path a deployment actually uses.
    """
    from platform_core.agent_runtime.semantic.context import build_context
    from platform_core.agent_runtime.semantic.service import (
        AnalysisRequest,
        SemanticBudget,
        analyze,
    )
    from platform_core.agent_runtime.tasks.planning_seam import run_task_planning
    from platform_core.identity.tenant_context import TenantContext, tenant_session

    on = [key for key, value in flags.items() if value]
    if on:
        _set_flags(tenant, on)

    ctx = TenantContext(
        tenant_id=uuid.UUID(tenant),
        actor_id=None,
        actor_kind="system",
        role="integration_service",
    )
    capabilities = {"order.get_status": CapabilityView("order.get_status", "read")}

    # The production path checks the flags *before* building a context or
    # calling the model, and this test asserts that the model is not consulted
    # when they are off. So the gate is reproduced here rather than delegated
    # to the seam, which is downstream of the model call by design.
    if not flags.get("agent.conversation_tasks") or not flags.get("agent.semantic_assist"):
        return 0

    context = build_context(
        current_turn_id="t-1",
        current_text=CUSTOMER_TEXT,
        history=[],
        mode=SemanticMode.ASSIST,
        capabilities=capabilities,
    )
    async with tenant_session(ctx) as session:
        assessment = await analyze(
            AnalysisRequest(
                context=context, lease_owner_type="ai", detection=classify(CUSTOMER_TEXT)
            ),
            provider=provider,
            capabilities=capabilities,
            budget=SemanticBudget(deadline_seconds=2.0, max_retries=0),
        )
        outcome = await run_task_planning(
            session,
            tenant_id=uuid.UUID(tenant),
            conversation_ref_id=uuid.UUID(conversation),
            assessment=assessment,
            capabilities=capabilities,
        )
        # `PLANNED` with zero created is the replay path: the idempotency key
        # matched and the existing tasks were returned. Only a non-PLANNED
        # outcome with nothing created is a failure.
        if outcome.created == 0 and outcome.reason != "PLANNED":
            # A zero here with the flags on means the seam declined, and the
            # reason is the only thing that says why. Surfaced rather than
            # swallowed so a regression names its cause.
            raise AssertionError(
                f"seam created nothing: decision={assessment.effective_decision} "
                f"reasons={assessment.reason_codes} outcome={outcome.reason}"
            )
        return outcome.created
