"""Integration tests: Case command service (ticket 21)."""

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = os.environ.get(
    "APP_TEST_DATABASE_URL",
    "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform",
)
TENANT_A = "01900000-0000-7000-8000-000000000001"


def _run(coro):
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


@pytest.fixture(scope="module", autouse=True)
def seed_tenant() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, 'case-test', 'Case Tenant', 'active') "
                "ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT_A},
        )
    yield
    with admin.begin() as conn:
        conn.execute(text("DELETE FROM case_conversations WHERE tenant_id = :t"), {"t": TENANT_A})
        # Before `cases`: an escalation carries an FK to the case it belongs to,
        # so deleting the case first raises `fk_case_escalation_case` and leaves
        # the tenant dirty for the next run. The escalation link is exercised
        # here because it is the other half of what priority claiming joins on.
        conn.execute(text("DELETE FROM case_escalations WHERE tenant_id = :t"), {"t": TENANT_A})
        conn.execute(text("DELETE FROM cases WHERE tenant_id = :t"), {"t": TENANT_A})
        conn.execute(text("DELETE FROM tenants WHERE slug = 'case-test'"))
    admin.dispose()


def test_case_lifecycle_with_version_conflict() -> None:
    from platform_core.cases.models import TransitionNotAllowed, VersionConflict
    from platform_core.cases.service import CaseService
    from platform_core.db import create_engine

    async def scenario() -> tuple[int, str, int]:
        engine = create_engine(APP_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        tid = uuid.UUID(TENANT_A)

        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_A}
            )
            svc = CaseService(session)
            case = await svc.create_case(
                tenant_id=tid, subject="Cannot export invoices", priority="p1"
            )
            await session.commit()
            case_id = case.id
            v1 = case.version

            # commit() ended the transaction; set_config was transaction-
            # scoped, so re-apply the RLS context for the next command.
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_A}
            )
            # Illegal jump: NEW -> RESOLVED
            illegal = ""
            try:
                await svc.apply_command(
                    tenant_id=tid,
                    case_id=case_id,
                    command="transition",
                    parameters={"target": "resolved"},
                )
            except TransitionNotAllowed:
                illegal = "rejected"
            await session.rollback()

        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_A}
            )
            svc = CaseService(session)
            # Stale version for the valid transition must conflict
            try:
                await svc.apply_command(
                    tenant_id=tid,
                    case_id=case_id,
                    command="transition",
                    expected_version=99,
                    parameters={"target": "triaged"},
                )
                conflict = "no-conflict"
            except VersionConflict:
                conflict = "conflict"
            await session.rollback()

        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_A}
            )
            svc = CaseService(session)
            case = await svc.apply_command(
                tenant_id=tid,
                case_id=case_id,
                command="transition",
                expected_version=v1,
                parameters={"target": "triaged"},
            )
            await session.commit()
            v2 = case.version
            status = case.status
            # commit() ended the transaction again; re-apply RLS context.
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_A}
            )
            # legal follow-up: triaged -> in_progress, then resolve
            case = await svc.apply_command(
                tenant_id=tid,
                case_id=case_id,
                command="transition",
                expected_version=v2,
                parameters={"target": "in_progress"},
            )
            case = await svc.apply_command(
                tenant_id=tid,
                case_id=case_id,
                command="transition",
                expected_version=case.version,
                parameters={"target": "resolved"},
            )
            await session.commit()
            resolved_status = case.status
            resolved_at = case.resolved_at
        await engine.dispose()
        return v1, illegal, v2, status, conflict, resolved_status, resolved_at

    v1, illegal, v2, status, conflict, resolved_status, resolved_at = _run(scenario())
    assert v1 == 1
    assert illegal == "rejected"
    assert conflict == "conflict"
    assert v2 == 2 and status == "triaged"
    assert resolved_status == "resolved" and resolved_at is not None


def test_case_sla_deadlines_set_on_priority() -> None:
    from platform_core.cases.service import CaseService
    from platform_core.db import create_engine

    async def scenario() -> tuple[int, int]:
        engine = create_engine(APP_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        tid = uuid.UUID(TENANT_A)
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_A}
            )
            svc = CaseService(session)
            case = await svc.create_case(tenant_id=tid, subject="p0 outage", priority="p0")
            await session.commit()
            frd = case.first_response_due_at - case.opened_at
            rd = case.resolution_due_at - case.opened_at
        await engine.dispose()
        return int(frd), int(rd)

    frd, rd = _run(scenario())
    assert frd == 15 * 60  # 60min * 0.25
    assert rd == 2 * 60 * 60  # 8h * 0.25


def test_a_case_created_from_a_conversation_records_the_link() -> None:
    """`CaseConversation` had a reader and no writer.

    `inbox_consumer.case_conversation_ref` joins it to decide which inbox
    events belong to an escalated case. Nothing ever inserted a row, so the
    join could only ever be empty - which means `worker.priority_claim_enabled`
    changed no behaviour while reporting no error, and an operator turning it
    on would have concluded it worked.

    Both halves are asserted: the writer this adds, and that the reader's
    meaning is still "a case with at least one escalation" rather than "any
    case at all". Fixing the empty join by widening the filter would have
    traded a silent no-op for a wrong one.
    """
    from sqlalchemy import select

    from platform_core.cases.models import CaseConversation
    from platform_core.cases.service import CaseService
    from platform_core.db import create_engine
    from worker.inbox_consumer import case_conversation_ref

    conversation = uuid.uuid4()

    async def scenario() -> tuple[int, list[uuid.UUID], list[uuid.UUID]]:
        engine = create_engine(APP_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        tid = uuid.UUID(TENANT_A)

        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_A}
            )
            case = await CaseService(session).create_case(
                tenant_id=tid,
                subject="EQ 99001: confirm stackup before production",
                category="eq_confirmation",
                conversation_ref_id=conversation,
            )
            linked = (
                (
                    await session.execute(
                        select(CaseConversation.conversation_ref_id).where(
                            CaseConversation.case_id == case.id
                        )
                    )
                )
                .scalars()
                .all()
            )

            before = (await session.execute(case_conversation_ref())).scalars().all()
            await session.execute(
                text(
                    "INSERT INTO case_escalations (id, tenant_id, case_id, clock, level, "
                    "reason_code) VALUES (:i, :t, :c, 'first_response', 1, 'SLA_BREACH')"
                ),
                {"i": str(uuid.uuid4()), "t": TENANT_A, "c": str(case.id)},
            )
            after = (await session.execute(case_conversation_ref())).scalars().all()
            await session.commit()

        await engine.dispose()
        return len(linked), list(before), list(after)

    linked_count, before, after = _run(scenario())

    assert linked_count == 1, "creating a case from a conversation did not record the link"
    assert conversation not in before, "priority claiming matched a case that was never escalated"
    assert conversation in after, "an escalated case's conversation is not discoverable"


def test_a_case_with_no_conversation_records_no_link() -> None:
    """A Case raised from a phone call has no conversation, and inventing one
    would put it in the priority-claim join under a reference nobody holds."""
    from sqlalchemy import select

    from platform_core.cases.models import CaseConversation
    from platform_core.cases.service import CaseService
    from platform_core.db import create_engine

    async def scenario() -> int:
        engine = create_engine(APP_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)

        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_A}
            )
            case = await CaseService(session).create_case(
                tenant_id=uuid.UUID(TENANT_A), subject="Raised by phone"
            )
            rows = (
                (
                    await session.execute(
                        select(CaseConversation.id).where(CaseConversation.case_id == case.id)
                    )
                )
                .scalars()
                .all()
            )
            await session.commit()

        await engine.dispose()
        return len(rows)

    assert _run(scenario()) == 0
