"""A lease change must not leave a conversation's queue silently orphaned.

What went wrong
---------------
A customer sends twenty questions in a row. The third trips the clarification
limit, the lease moves from the AI to the human queue, and the other seventeen
runs - already written, already queued, already charged against the tenant's
quota - are left to be discovered one at a time by a worker. Each is then
skipped at the ownership guard and recorded as `handed_off`.

`handed_off` is the wrong word, and that is the whole defect. It means "a
person has this". At that moment nobody has it: the conversation is parked in a
queue and a human has not looked at it. Downstream, `handed_off` is what the
conversation list, the run replay and the outcome metrics read, so eighteen
questions nobody ever answered are indistinguishable from eighteen a colleague
picked up. Measured on a live stack: 20 messages in, 3 replies out, 18 runs
reported as handed to a human.

The customer is not told either. The notice that reaches them is written at
question-submission time, and all twenty questions were submitted before the
lease moved - so no path says anything at all.

The properties pinned here
--------------------------
1. Releasing the lease to the queue closes out that conversation's queued runs
   with a status meaning "accepted, never executed, nobody is on it".
2. The customer gets one notice saying their remaining questions are waiting -
   not zero, and not one per question.
3. Runs in other conversations and other tenants are untouched.
4. Runs that already reached an outcome are never rewritten.
"""

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
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"

TENANT = "01900000-0000-7000-8000-0000000000d4"
OTHER_TENANT = "01900000-0000-7000-8000-0000000000d5"
SLUG = "lease-close-queued"
OTHER_SLUG = "lease-close-other"


def _run(coro):
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


@pytest.fixture(scope="module", autouse=True)
def seed() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT, SLUG), (OTHER_TENANT, OTHER_SLUG)):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) "
                    "VALUES (:id, :slug, 'Lease Close Tenant', 'active') "
                    "ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug},
            )
    yield
    with admin.begin() as conn:
        for table in ("conversation_turns", "agent_runs", "conversation_control_leases"):
            conn.execute(
                text(f"DELETE FROM {table} WHERE tenant_id = ANY(:t)"),  # noqa: S608 - fixed names
                {"t": [TENANT, OTHER_TENANT]},
            )
        conn.execute(
            text("DELETE FROM tenants WHERE slug = ANY(:s)"),
            {"s": [SLUG, OTHER_SLUG]},
        )
    admin.dispose()


def _seed_runs(conn, tenant: str, conversation: str, *, count: int, status: str) -> list[uuid.UUID]:
    ids = [uuid.uuid4() for _ in range(count)]
    for run_id in ids:
        conn.execute(
            text(
                "INSERT INTO agent_runs (id, tenant_id, conversation_ref_id, route, status) "
                "VALUES (:id, :t, :c, 'knowledge_qa', :s)"
            ),
            {"id": str(run_id), "t": tenant, "c": conversation, "s": status},
        )
    return ids


def _statuses(conn, ids: list[uuid.UUID]) -> list[str]:
    rows = conn.execute(
        text("SELECT status FROM agent_runs WHERE id = ANY(:ids) ORDER BY id"),
        {"ids": [str(i) for i in ids]},
    ).all()
    return [r[0] for r in rows]


def _system_notices(conn, conversation: str) -> list[str]:
    rows = conn.execute(
        text(
            "SELECT text_redacted FROM conversation_turns "
            "WHERE tenant_id = :t AND conversation_ref_id = :c AND role = 'system' "
            "ORDER BY created_at"
        ),
        {"t": TENANT, "c": conversation},
    ).all()
    return [r[0] for r in rows]


def test_releasing_to_the_queue_closes_queued_runs_and_notifies_once() -> None:
    from platform_core.agent_runtime.handoff import hand_off_to_human_queue
    from platform_core.db import create_engine as create_app_engine
    from platform_core.identity import lease_service

    conversation = uuid.uuid4()
    other_conversation = uuid.uuid4()

    async def scenario() -> dict:
        admin = create_engine(ADMIN_URL)
        app = create_app_engine(APP_URL)
        factory = async_sessionmaker(app, expire_on_commit=False)
        try:
            with admin.begin() as conn:
                queued = _seed_runs(conn, TENANT, str(conversation), count=17, status="queued")
                settled = _seed_runs(conn, TENANT, str(conversation), count=2, status="completed")
                # A sibling conversation in the same tenant, and a whole other
                # tenant: neither may be touched.
                sibling = _seed_runs(
                    conn, TENANT, str(other_conversation), count=4, status="queued"
                )
                foreign = _seed_runs(
                    conn, OTHER_TENANT, str(other_conversation), count=3, status="queued"
                )

            async with factory() as session:
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
                )
                await lease_service.acquire_or_get(
                    session, tenant_id=uuid.UUID(TENANT), conversation_ref_id=conversation
                )
                await session.commit()
                # Re-bind after the commit: `set_config(..., true)` is
                # transaction-scoped, so the binding that made the insert
                # visible is gone by the next statement. `release_to_queue`'s
                # own docstring says as much; without it the UPDATE matches
                # nothing under RLS and reports "lease row missing".
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
                )
                await hand_off_to_human_queue(
                    session,
                    tenant_id=uuid.UUID(TENANT),
                    conversation_ref_id=conversation,
                    reason="abstain:CLARIFICATION_LIMIT",
                )
                await session.commit()

            with admin.connect() as conn:
                return {
                    "queued": _statuses(conn, queued),
                    "settled": _statuses(conn, settled),
                    "sibling": _statuses(conn, sibling),
                    "foreign": _statuses(conn, foreign),
                    "notices": _system_notices(conn, str(conversation)),
                }
        finally:
            await app.dispose()
            admin.dispose()

    result = _run(scenario())

    # 1. Every queued run of that conversation now says it was never executed.
    assert result["queued"] == ["superseded"] * 17, result["queued"]
    # 2. A run that already finished keeps its outcome.
    assert result["settled"] == ["completed"] * 2, result["settled"]
    # 3. Nothing else moved.
    assert result["sibling"] == ["queued"] * 4, result["sibling"]
    assert result["foreign"] == ["queued"] * 3, result["foreign"]
    # 4. The customer is told once, and the notice is in the timeline they read.
    assert len(result["notices"]) == 1, result["notices"]
    assert "人工" in result["notices"][0], result["notices"]


def test_releasing_a_conversation_with_nothing_queued_still_works() -> None:
    """The common case must not regress: no queued runs, no spurious notice."""
    from platform_core.agent_runtime.handoff import hand_off_to_human_queue
    from platform_core.db import create_engine as create_app_engine
    from platform_core.identity import lease_service

    conversation = uuid.uuid4()

    async def scenario() -> tuple[int, int]:
        admin = create_engine(ADMIN_URL)
        app = create_app_engine(APP_URL)
        factory = async_sessionmaker(app, expire_on_commit=False)
        try:
            with admin.begin() as conn:
                _seed_runs(conn, TENANT, str(conversation), count=1, status="completed")
            async with factory() as session:
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
                )
                await lease_service.acquire_or_get(
                    session, tenant_id=uuid.UUID(TENANT), conversation_ref_id=conversation
                )
                await session.commit()
                # Re-bind after the commit: `set_config(..., true)` is
                # transaction-scoped, so the binding that made the insert
                # visible is gone by the next statement. `release_to_queue`'s
                # own docstring says as much; without it the UPDATE matches
                # nothing under RLS and reports "lease row missing".
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
                )
                await hand_off_to_human_queue(
                    session,
                    tenant_id=uuid.UUID(TENANT),
                    conversation_ref_id=conversation,
                    reason="abstain:OUT_OF_HOURS",
                )
                await session.commit()
            with admin.connect() as conn:
                return (
                    len(_system_notices(conn, str(conversation))),
                    len(_statuses(conn, [])),
                )
        finally:
            await app.dispose()
            admin.dispose()

    notices, _ = _run(scenario())
    assert notices == 0, "a notice with nothing queued to explain is noise"


def test_a_superseded_placeholder_is_adopted_not_duplicated() -> None:
    """One logical question must stay one run row.

    The close-out marks a conversation's queued runs the moment ownership
    moves, while their inbox events are still in the queue. When the worker
    reaches one of those events, adoption used to look only for a `queued`
    placeholder, find none, and create a *second* run for a question that
    already had one. Measured on a live stack with a twenty-message burst:
    39 run rows for 20 turns, and the extra 18 were the duplicated ones.

    So the placeholder is adopted in its `superseded` state and returned
    untouched - no status rewrite, no `started_at` on a run that never began.
    """
    from observability import TraceContext
    from platform_core.agent_runtime.handoff import hand_off_to_human_queue
    from platform_core.db import create_engine as create_app_engine
    from platform_core.identity import lease_service

    conversation = uuid.uuid4()

    async def scenario() -> tuple[int, list[str]]:
        admin = create_engine(ADMIN_URL)
        app = create_app_engine(APP_URL)
        factory = async_sessionmaker(app, expire_on_commit=False)
        try:
            with admin.begin() as conn:
                _seed_runs(conn, TENANT, str(conversation), count=3, status="queued")

            async with factory() as session:
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
                )
                await lease_service.acquire_or_get(
                    session, tenant_id=uuid.UUID(TENANT), conversation_ref_id=conversation
                )
                await session.commit()
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
                )
                closed = await hand_off_to_human_queue(
                    session,
                    tenant_id=uuid.UUID(TENANT),
                    conversation_ref_id=conversation,
                    reason="abstain:CLARIFICATION_LIMIT",
                )
                await session.commit()

                assert closed == 3, closed
                # Re-bind: the commit above ended the transaction, and a
                # transaction-scoped `set_config` does not survive it. Unbound,
                # RLS correctly hides every row - which is indistinguishable,
                # from the outside, from adoption having found nothing. This is
                # the same re-bind `inbox_consumer` does per event.
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
                )

                # Exactly what the worker does when it reaches a stale event:
                # the real `_adopt_or_create_run`, three times. A minimal stand-in
                # for `self` is enough because the superseded branch returns
                # before touching the collaborators - and if that early return
                # ever moves, this test fails on the missing attribute rather
                # than quietly passing against a re-implementation.
                adopt = _real_adopt(session)
                for _ in range(3):
                    await adopt(
                        tenant_id=uuid.UUID(TENANT),
                        conversation_ref_id=conversation,
                        route="knowledge_qa",
                        ctx=TraceContext("probe"),
                        context=None,
                        detection=None,
                        rewritten=False,
                        retrieval_query="q",
                        question="q",
                    )
                await session.commit()

            with admin.connect() as conn:
                total = conn.execute(
                    text(
                        "SELECT count(*) FROM agent_runs "
                        "WHERE tenant_id = :t AND conversation_ref_id = :c"
                    ),
                    {"t": TENANT, "c": str(conversation)},
                ).scalar_one()
                statuses = [
                    r[0]
                    for r in conn.execute(
                        text(
                            "SELECT status FROM agent_runs "
                            "WHERE tenant_id = :t AND conversation_ref_id = :c ORDER BY id"
                        ),
                        {"t": TENANT, "c": str(conversation)},
                    ).all()
                ]
                return int(total), statuses
        finally:
            await app.dispose()
            admin.dispose()

    total, statuses = _run(scenario())
    assert total == 3, f"adoption duplicated runs: {total} rows for 3 questions"
    assert statuses == ["superseded"] * 3, statuses


def _real_adopt(session: object):
    """Bind the orchestrator's real adoption method to a minimal `self`."""
    from platform_core.agent_runtime.orchestrator import AgentOrchestrator

    class _SessionOnly:
        _session = session

    return AgentOrchestrator._adopt_or_create_run.__get__(_SessionOnly())
