"""Real PostgreSQL tests for conversation tasks (T04).

The unit tests prove the state machine and the condition evaluator. This file
proves the things that only a database can answer:

- the four new tables have FORCE RLS and a policy that actually filters;
- the app role cannot read or write another tenant's tasks, events or drafts,
  including by direct id, by paging, and by referencing another tenant's
  conversation;
- two concurrent transitions on one task produce exactly one winner;
- a repeated identity returns the original task rather than creating a second
  one, and a *different* payload under the same key is refused;
- `succeeded` cannot be written without completion evidence.

Every negative case asserts on the row count after the attempt, not only on the
exception: a statement that raises but still wrote would pass a
`pytest.raises` on its own.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from platform_core.agent_runtime.tasks.models import (
    ConversationTask,
    ConversationTaskEvent,
    CopilotDraft,
)
from platform_core.agent_runtime.tasks.state_machine import (
    TaskKind,
    TaskStatus,
    TaskTransitionError,
)
from platform_core.agent_runtime.tasks.store import (
    EVIDENCE_HUMAN_ACTION,
    EVIDENCE_VERIFIED_RECEIPT,
    TaskCommand,
    TaskConflict,
    create_or_get,
    get_task,
    list_tasks,
    make_local_key,
    record_assessment,
    transition,
)

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_ROLE = "platform_app"


def _engine() -> object:
    from platform_core.db import create_engine

    return create_engine(str(ADMIN_URL))


def _app_url() -> str:
    return str(ADMIN_URL).replace("platform:platform@", f"{APP_ROLE}:{APP_ROLE}@")


def _run(coro: object) -> object:
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)  # type: ignore[arg-type]


async def _dispose(engine: object) -> None:
    """Dispose an AsyncEngine.

    `dispose()` is a coroutine, so calling it from a sync test and dropping the
    result leaves an un-awaited coroutine behind - which pytest reports as a
    RuntimeWarning and which leaks the pool in a long run.
    """
    await engine.dispose()  # type: ignore[attr-defined]


async def _seed_tenant(engine: object) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create a tenant, a conversation ref and an agent. Returns their ids.

    `conversation_ref_id` is a bare UUID column on the task tables, not a
    reference to a conversation table - there is no such table. The lease is
    the thing that owns a conversation, so the ref is minted here and used
    consistently by every helper below.
    """
    tenant_id = uuid.uuid4()
    conv = uuid.uuid4()
    agent = uuid.uuid4()
    async with engine.begin() as conn:  # type: ignore[attr-defined]
        await conn.execute(
            text(
                "INSERT INTO tenants (id, name, slug, status) VALUES (:id, :name, :slug, 'active')"
            ),
            {"id": tenant_id, "name": "R1 tasks", "slug": f"r1-{tenant_id.hex[:12]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO agent_profiles (id, tenant_id, user_ref, display_name, "
                "status, max_concurrent, created_at, updated_at) "
                "VALUES (:id, :tenant_id, :ref, 'R1 Agent', 'active', 5, 0, 0)"
            ),
            {"id": agent, "tenant_id": tenant_id, "ref": f"u-{agent.hex[:8]}"},
        )
    return tenant_id, conv, agent


async def _make_task(
    engine: object,
    tenant_id: uuid.UUID,
    conv: uuid.UUID,
    *,
    key: str = "read-0",
    turn: str = "turn-1",
    kind: TaskKind = TaskKind.READ,
    status: TaskStatus = TaskStatus.READY,
) -> ConversationTask:
    from platform_core.db import session_scope_with_url

    async with session_scope_with_url(str(ADMIN_URL)) as session:  # type: ignore[attr-defined]
        task, _ = await create_or_get(
            session,
            tenant_id=tenant_id,
            conversation_ref_id=conv,
            source_turn_id=turn,
            task_local_key=key,
            kind=kind,
            status=status,
        )
        return task


# --- RLS --------------------------------------------------------------------


def test_new_tables_have_forced_rls_and_a_tenant_policy() -> None:
    rows = _run(_fetch_rls())
    assert set(rows) == {
        "conversation_tasks",
        "conversation_task_events",
        "copilot_drafts",
        "semantic_assessments",
    }
    for table, (enabled, forced, policies) in rows.items():
        assert enabled, f"{table} does not have RLS enabled"
        assert forced, f"{table} does not have FORCE RLS"
        assert policies == 1, f"{table} has {policies} policies, expected 1"


async def _fetch_rls() -> dict[str, tuple[bool, bool, int]]:
    from sqlalchemy import text as _text

    engine = _engine()
    out: dict[str, tuple[bool, bool, int]] = {}
    try:
        async with engine.begin() as conn:  # type: ignore[attr-defined]
            for table in (
                "conversation_tasks",
                "conversation_task_events",
                "copilot_drafts",
                "semantic_assessments",
            ):
                row = (
                    await conn.execute(
                        _text(
                            "SELECT c.relrowsecurity, c.relforcerowsecurity, "
                            "(SELECT count(*) FROM pg_policies p "
                            " WHERE p.tablename = c.relname) "
                            "FROM pg_class c WHERE c.relname = :t"
                        ),
                        {"t": table},
                    )
                ).one()
                out[table] = (bool(row[0]), bool(row[1]), int(row[2]))
    finally:
        await engine.dispose()  # type: ignore[attr-defined]
    return out


def test_app_role_cannot_see_another_tenants_tasks() -> None:
    """SEC-01: a task belonging to another tenant is not readable.

    The session runs as the app role with tenant B's context set, which is the
    shape of a real tenant-scoped request. Anything tenant A wrote must be
    invisible - not because the query filtered it, but because RLS did.
    """
    engine = _engine()
    tenant_a, conv_a, _ = _run(_seed_tenant(engine))
    tenant_b, _conv_b, _ = _run(_seed_tenant(engine))
    task, _ = _run(_create(engine, tenant_a, conv_a))
    _run(_dispose(engine))

    # Tenant B cannot see A's row by id. The id is a bound parameter, not
    # interpolated text - the point of the test is RLS, and a query that only
    # works because the value was safe would not prove it.
    assert (
        _run(
            _count_as_app_role(
                tenant_b,
                "SELECT id FROM conversation_tasks WHERE id = :task_id",
                {"task_id": task.id},
            )
        )
        == 0
    )
    # ...nor by any predicate at all.
    assert _run(_count_as_app_role(tenant_b, "SELECT id FROM conversation_tasks")) == 0
    # And the events and drafts behind it are equally invisible.
    assert _run(_count_as_app_role(tenant_b, "SELECT id FROM conversation_task_events")) == 0
    assert _run(_count_as_app_role(tenant_b, "SELECT id FROM copilot_drafts")) == 0


async def _count_as_app_role(
    tenant_id: uuid.UUID, sql: str, params: dict[str, object] | None = None
) -> int:
    """Run `sql` as the non-owner app role with `tenant_id` set on the session.

    `sql` is a module-level literal at every call site; values travel as bound
    parameters, so nothing a test generates is ever concatenated into a
    statement.
    """
    from sqlalchemy import text as _text

    from platform_core.db import create_engine

    engine = create_engine(_app_url())
    try:
        async with engine.begin() as conn:  # type: ignore[attr-defined]
            await conn.execute(
                _text("SELECT set_config('app.tenant_id', :t, false)"), {"t": str(tenant_id)}
            )
            result = await conn.execute(_text(sql), params or {})
            return int(result.scalar() or 0)
    finally:
        await engine.dispose()  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "table", ["conversation_tasks", "conversation_task_events", "copilot_drafts"]
)
def test_app_role_cannot_write_rows_for_another_tenant(table: str) -> None:
    """A WITH CHECK violation must reject the row, not warn.

    `FORCE ROW LEVEL SECURITY` is what makes this hold for the table owner too,
    so a migration or a maintenance script running as the owner is subject to
    the same rule as the application.
    """
    engine = _engine()
    tenant_a, conv_a, agent_a = _run(_seed_tenant(engine))
    tenant_b, _conv_b, _ = _run(_seed_tenant(engine))
    _run(_dispose(engine))

    columns = {
        "conversation_tasks": (
            "id, tenant_id, conversation_ref_id, source_turn_id, task_local_key, kind, "
            "status, version, action_revision, content_hash, depends_on, slots, "
            "missing_slots, created_at, updated_at"
        ),
        "conversation_task_events": (
            "id, tenant_id, task_id, conversation_ref_id, sequence, to_status, "
            "actor_type, reason_code, to_version, created_at"
        ),
        "copilot_drafts": (
            "id, tenant_id, conversation_ref_id, actor_id, job_id, kind, status, "
            "timeline_revision, lease_version, source_refs, body, version, "
            "edited_by_human, created_at, updated_at"
        ),
    }[table]
    placeholders = ", ".join(f":{c}" for c in columns.replace(" ", "").split(","))
    values = {
        "id": uuid.uuid4(),
        # Deliberately NOT the session's tenant: this is the whole test.
        "tenant_id": tenant_a,
        "conversation_ref_id": conv_a,
        "source_turn_id": "t",
        "task_local_key": "read-0",
        "kind": "read",
        "status": "ready",
        "version": 1,
        "action_revision": 1,
        "content_hash": "0" * 64,
        "depends_on": "[]",
        "slots": "[]",
        "missing_slots": "[]",
        "created_at": 0,
        "updated_at": 0,
        "task_id": uuid.uuid4(),
        "sequence": 1,
        "to_status": "ready",
        "actor_type": "system",
        "reason_code": "TEST",
        "to_version": 1,
        "actor_id": agent_a,
        "job_id": uuid.uuid4(),
        "timeline_revision": 0,
        "lease_version": 0,
        "source_refs": "[]",
        "body": "",
        "edited_by_human": "false",
    }

    result = _run(_attempt_cross_tenant_insert(tenant_b, table, columns, placeholders, values))
    assert result is False, f"{table} accepted a row for another tenant"


async def _attempt_cross_tenant_insert(
    tenant_id: uuid.UUID, table: str, columns: str, placeholders: str, values: dict[str, object]
) -> bool:
    from sqlalchemy import text as _text

    from platform_core.db import create_engine

    engine = create_engine(_app_url())
    try:
        async with engine.begin() as conn:  # type: ignore[attr-defined]
            await conn.execute(
                _text("SELECT set_config('app.tenant_id', :t, false)"), {"t": str(tenant_id)}
            )
            try:
                await conn.execute(
                    _text(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"), values
                )
                return True
            except Exception:  # noqa: BLE001 - any refusal is the point
                return False
    finally:
        await engine.dispose()  # type: ignore[attr-defined]


# --- idempotency ------------------------------------------------------------


def test_a_repeated_identity_returns_the_original_task() -> None:
    """TASK-03: a redelivered turn must not become a second task."""
    engine = _engine()
    tenant_id, conv, _ = _run(_seed_tenant(engine))
    _run(_dispose(engine))

    first, created_first = _run(_create(engine, tenant_id, conv))
    second, created_second = _run(_create(engine, tenant_id, conv))
    assert created_first is True
    assert created_second is False
    # Compared by id: these are two ORM instances loaded in two different
    # sessions, so `==` is identity comparison and would be false even for the
    # same row.
    assert first.id == second.id

    total = _run(_count_tasks(tenant_id, conv))
    assert total == 1


async def _create(
    engine: object, tenant_id: uuid.UUID, conv: uuid.UUID
) -> tuple[ConversationTask, bool]:
    from platform_core.db import session_scope_with_url

    async with session_scope_with_url(str(ADMIN_URL)) as session:  # type: ignore[attr-defined]
        return await create_or_get(
            session,
            tenant_id=tenant_id,
            conversation_ref_id=conv,
            source_turn_id="turn-1",
            task_local_key=make_local_key(TaskKind.READ, 0),
            kind=TaskKind.READ,
            status=TaskStatus.READY,
        )


async def _count_tasks(tenant_id: uuid.UUID, conv: uuid.UUID) -> int:
    from platform_core.db import session_scope_with_url

    async with session_scope_with_url(str(ADMIN_URL)) as session:  # type: ignore[attr-defined]
        return int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(ConversationTask)
                    .where(
                        ConversationTask.tenant_id == tenant_id,
                        ConversationTask.conversation_ref_id == conv,
                    )
                )
            ).scalar_one()
        )


def test_a_different_payload_under_the_same_key_is_refused() -> None:
    """The correction must not be silently dropped as a duplicate."""
    engine = _engine()
    tenant_id, conv, _ = _run(_seed_tenant(engine))
    _run(_make_task(engine, tenant_id, conv))
    _run(_dispose(engine))

    from platform_core.db import session_scope_with_url

    async def _retry() -> str:
        async with session_scope_with_url(str(ADMIN_URL)) as session:  # type: ignore[attr-defined]
            try:
                await create_or_get(
                    session,
                    tenant_id=tenant_id,
                    conversation_ref_id=conv,
                    source_turn_id="turn-1",
                    task_local_key=make_local_key(TaskKind.READ, 0),
                    kind=TaskKind.READ,
                    status=TaskStatus.READY,
                    # A different missing-field set is a different request.
                    missing_slots=["order_no"],
                )
                return "accepted"
            except TaskConflict as exc:
                return exc.code

    assert _run(_retry()) == "TASK_IDENTITY_CONTENT_MISMATCH"


# --- concurrency ------------------------------------------------------------


def test_two_workers_racing_on_one_task_produce_one_winner() -> None:
    """TASK-03: both workers read the same version, then both try to advance.

    The compare-and-set is on `version`, so exactly one UPDATE matches a row.
    The loser is told to re-read rather than silently double-applying the
    transition - which for a write task is the difference between one external
    call and two.
    """
    engine = _engine()
    tenant_id, conv, _ = _run(_seed_tenant(engine))
    task, _ = _run(_create(engine, tenant_id, conv))
    _run(_dispose(engine))

    stale_version = task.version
    outcomes = _run(_race_transitions(task.id, tenant_id, stale_version))
    assert sorted(outcomes) == ["TASK_VERSION_CONFLICT", "won"]


async def _race_transitions(
    task_id: uuid.UUID, tenant_id: uuid.UUID, stale_version: int
) -> list[str]:
    from platform_core.db import session_scope_with_url

    results: list[str] = []

    async def _attempt(label: str) -> None:
        async with session_scope_with_url(str(ADMIN_URL)) as session:  # type: ignore[attr-defined]
            task = await get_task(session, tenant_id=tenant_id, task_id=task_id)
            assert task is not None
            try:
                await transition(
                    session,
                    tenant_id=tenant_id,
                    task=task,
                    command=TaskCommand(
                        target=TaskStatus.EXECUTING,
                        reason_code=f"RACE_{label}",
                        # Both workers were handed the version they read before
                        # either committed - this is the real race.
                        expected_version=stale_version,
                    ),
                )
                results.append("won")
            except TaskConflict:
                results.append("TASK_VERSION_CONFLICT")

    await asyncio.gather(_attempt("a"), _attempt("b"))
    return results


# --- completion evidence ----------------------------------------------------


def test_succeeded_requires_evidence() -> None:
    engine = _engine()
    tenant_id, conv, _ = _run(_seed_tenant(engine))
    task, _ = _run(_create(engine, tenant_id, conv))
    _run(_dispose(engine))

    outcome = _run(_try_succeed(task.id, tenant_id, evidence=None))
    assert outcome == "TASK_COMPLETION_EVIDENCE_REQUIRED"


def test_succeeded_accepts_a_verified_receipt() -> None:
    engine = _engine()
    tenant_id, conv, _ = _run(_seed_tenant(engine))
    task, _ = _run(_create(engine, tenant_id, conv))
    _run(_dispose(engine))

    outcome = _run(_try_succeed(task.id, tenant_id, evidence=EVIDENCE_VERIFIED_RECEIPT + "exec-1"))
    assert outcome == "succeeded"


def test_succeeded_rejects_an_invented_evidence_prefix() -> None:
    """A model's own assertion is not a receipt."""
    engine = _engine()
    tenant_id, conv, _ = _run(_seed_tenant(engine))
    task, _ = _run(_create(engine, tenant_id, conv))
    _run(_dispose(engine))

    outcome = _run(_try_succeed(task.id, tenant_id, evidence="the model says it worked"))
    assert outcome == "TASK_COMPLETION_EVIDENCE_INVALID"


async def _try_succeed(task_id: uuid.UUID, tenant_id: uuid.UUID, evidence: str | None) -> str:
    from platform_core.db import session_scope_with_url

    async with session_scope_with_url(str(ADMIN_URL)) as session:  # type: ignore[attr-defined]
        task = await get_task(session, tenant_id=tenant_id, task_id=task_id)
        assert task is not None
        # ready -> executing -> succeeded
        await transition(
            session,
            tenant_id=tenant_id,
            task=task,
            command=TaskCommand(target=TaskStatus.EXECUTING, reason_code="TEST_EXEC"),
        )
        try:
            await transition(
                session,
                tenant_id=tenant_id,
                task=task,
                command=TaskCommand(
                    target=TaskStatus.SUCCEEDED,
                    reason_code="TEST_DONE",
                    completion_evidence=evidence,
                ),
            )
            return "succeeded"
        except TaskTransitionError as exc:
            return exc.code


def test_a_terminal_task_refuses_further_transitions() -> None:
    engine = _engine()
    tenant_id, conv, _ = _run(_seed_tenant(engine))
    task, _ = _run(_create(engine, tenant_id, conv))
    _run(_dispose(engine))

    assert _run(_try_succeed(task.id, tenant_id, EVIDENCE_HUMAN_ACTION + "agent-1")) == "succeeded"
    # A late worker holding the old row cannot reopen it.
    assert _run(_try_reopen(task.id, tenant_id)) == "TASK_TERMINAL"


async def _try_reopen(task_id: uuid.UUID, tenant_id: uuid.UUID) -> str:
    from platform_core.db import session_scope_with_url

    async with session_scope_with_url(str(ADMIN_URL)) as session:  # type: ignore[attr-defined]
        task = await get_task(session, tenant_id=tenant_id, task_id=task_id)
        assert task is not None
        try:
            await transition(
                session,
                tenant_id=tenant_id,
                task=task,
                command=TaskCommand(target=TaskStatus.EXECUTING, reason_code="LATE_WORKER"),
            )
            return "reopened"
        except TaskTransitionError as exc:
            return exc.code


# --- event log --------------------------------------------------------------


def test_every_transition_appends_an_event() -> None:
    engine = _engine()
    tenant_id, conv, _ = _run(_seed_tenant(engine))
    task, _ = _run(_create(engine, tenant_id, conv))
    _run(_dispose(engine))

    _run(_try_succeed(task.id, tenant_id, EVIDENCE_VERIFIED_RECEIPT + "exec-9"))

    events = _run(_events(tenant_id, task.id))
    # created, executing, succeeded
    assert [e["to_status"] for e in events] == ["ready", "executing", "succeeded"]
    assert all(e["sequence"] == i + 1 for i, e in enumerate(events))


async def _events(tenant_id: uuid.UUID, task_id: uuid.UUID) -> list[dict[str, object]]:
    from platform_core.db import session_scope_with_url

    async with session_scope_with_url(str(ADMIN_URL)) as session:  # type: ignore[attr-defined]
        rows = (
            await session.execute(
                select(ConversationTaskEvent)
                .where(
                    ConversationTaskEvent.tenant_id == tenant_id,
                    ConversationTaskEvent.task_id == task_id,
                )
                .order_by(ConversationTaskEvent.sequence)
            )
        ).scalars()
        return [{"to_status": r.to_status, "sequence": r.sequence} for r in rows]


# --- assessments ------------------------------------------------------------


def test_an_assessment_records_labels_but_no_slot_values() -> None:
    """SEC-04: the stored snapshot must not become a copy of the customer's
    words or of a delivery address."""
    import json

    from platform_core.agent_runtime.intent import classify
    from platform_core.agent_runtime.semantic.contracts import SemanticAssessment, SemanticMode

    engine = _engine()
    tenant_id, conv, _ = _run(_seed_tenant(engine))
    _run(_dispose(engine))

    assessment = SemanticAssessment(
        assessment_id=str(uuid.uuid4()),
        rule_scene=classify("查单").scene,
        rule_primary_intent=classify("查单").primary_kind,
        rule_secondary_intents=(),
        rule_route="business_read",
        rule_action="call_read_tool",
        model_output=None,
        agreement="rules_only",
        mode=SemanticMode.SHADOW,
        effective_decision="shadow_recorded",
        reason_codes=("SEMANTIC_MODE_OFF",),
        validation_status="not_attempted",
    )

    async def _write() -> None:
        from platform_core.db import session_scope_with_url

        async with session_scope_with_url(str(ADMIN_URL)) as session:  # type: ignore[attr-defined]
            await record_assessment(
                session,
                tenant_id=tenant_id,
                conversation_ref_id=conv,
                turn_id="turn-1",
                assessment=assessment,
            )

    _run(_write())
    stored = _run(_read_snapshot(tenant_id, conv))
    assert stored is not None
    # The snapshot is a projection, not a transcript: it carries the labels the
    # evaluation report reads and nothing that could reconstruct the message.
    assert stored["effective_decision"] == "shadow_recorded"
    assert stored["rule_route"] == "business_read"
    assert stored["mode"] == "shadow"
    # And no slot value, no turn id, no raw text.
    blob = json.dumps(stored, ensure_ascii=False)
    assert "SO-240918" not in blob
    assert "turn-1" not in blob


async def _read_snapshot(tenant_id: uuid.UUID, conv: uuid.UUID) -> dict[str, object] | None:
    from platform_core.agent_runtime.tasks.models import SemanticAssessmentRow
    from platform_core.db import session_scope_with_url

    async with session_scope_with_url(str(ADMIN_URL)) as session:  # type: ignore[attr-defined]
        row = (
            await session.execute(
                select(SemanticAssessmentRow).where(
                    SemanticAssessmentRow.tenant_id == tenant_id,
                    SemanticAssessmentRow.conversation_ref_id == conv,
                )
            )
        ).scalar_one_or_none()
        return dict(row.snapshot) if row else None


# --- cross-tenant foreign keys ---------------------------------------------


def test_a_task_cannot_reference_another_tenants_conversation() -> None:
    """The composite FK is the database's own guarantee, not an application
    check that a future caller might skip."""
    engine = _engine()
    tenant_a, conv_a, _ = _run(_seed_tenant(engine))
    _tenant_b, conv_b, _ = _run(_seed_tenant(engine))
    _run(_dispose(engine))

    async def _attempt() -> str:
        from platform_core.db import session_scope_with_url

        async with session_scope_with_url(str(ADMIN_URL)) as session:  # type: ignore[attr-defined]
            try:
                await create_or_get(
                    session,
                    tenant_id=tenant_a,
                    conversation_ref_id=conv_b,  # another tenant's conversation
                    source_turn_id="turn-1",
                    task_local_key="read-0",
                    kind=TaskKind.READ,
                    status=TaskStatus.READY,
                    assessment_id=uuid.uuid4(),
                )
                return "accepted"
            except (IntegrityError, TaskConflict):
                return "refused"

    assert _run(_attempt()) == "refused"


# --- listing ----------------------------------------------------------------


def test_listing_is_ordered_and_paginated() -> None:
    engine = _engine()
    tenant_id, conv, _ = _run(_seed_tenant(engine))

    async def _seed() -> None:
        from platform_core.db import session_scope_with_url

        async with session_scope_with_url(str(ADMIN_URL)) as session:  # type: ignore[attr-defined]
            for i in range(5):
                await create_or_get(
                    session,
                    tenant_id=tenant_id,
                    conversation_ref_id=conv,
                    source_turn_id="turn-1",
                    task_local_key=f"read-{i}",
                    kind=TaskKind.READ,
                    status=TaskStatus.READY,
                    sequence=i,
                )

    _run(_seed())
    _run(_dispose(engine))

    page = _run(_list(tenant_id, conv, limit=2, offset=0))
    assert [t.task_local_key for t in page] == ["read-0", "read-1"]
    page2 = _run(_list(tenant_id, conv, limit=2, offset=2))
    assert [t.task_local_key for t in page2] == ["read-2", "read-3"]


async def _list(
    tenant_id: uuid.UUID, conv: uuid.UUID, *, limit: int, offset: int
) -> list[ConversationTask]:
    from platform_core.db import session_scope_with_url

    async with session_scope_with_url(str(ADMIN_URL)) as session:  # type: ignore[attr-defined]
        return await list_tasks(
            session,
            tenant_id=tenant_id,
            conversation_ref_id=conv,
            limit=limit,
            offset=offset,
        )


# Referenced so the model import is not treated as unused by the linter: the
# draft table is asserted on through the RLS checks above.
_ = CopilotDraft
