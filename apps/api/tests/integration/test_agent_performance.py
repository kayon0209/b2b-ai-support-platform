"""Integration: agent performance and reply adoption.

The first test here is a **defect fix**, not a new feature. Batch 4 added the
reply path and it did not satisfy the SLA first-response clock: an agent could
answer, the customer could be happy, and the case would still escalate for
"no first response". `first_responded_at` was only ever set by an explicit
`record_first_response` command that no production caller issued.

The rest is the report that the reply path made possible - and its negative
cases, which matter more than the positive ones: a rate with no denominator must
be `None`, and an unknown provenance must not be folded into either bucket.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest
from sqlalchemy import create_engine as _admin_engine
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from platform_core.db import create_engine

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"

_NS = uuid.uuid5(uuid.NAMESPACE_URL, "b2b-ai-support/tests/agent-performance")
TENANT = str(uuid.uuid5(_NS, "tenant"))
TENANT_OTHER = str(uuid.uuid5(_NS, "tenant-other"))

_TEARDOWN = (
    "case_attachments",
    "case_conversations",
    "case_escalations",
    "cases",
    "conversation_turns",
    "conversation_control_leases",
    "agent_profiles",
)


def _run(coro):
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def _wipe(where: str, params: dict) -> None:
    admin = _admin_engine(ADMIN_URL)
    with admin.begin() as conn:
        for table in _TEARDOWN:
            conn.execute(text(f"DELETE FROM {table} WHERE {where}"), params)  # noqa: S608
    admin.dispose()


@pytest.fixture(autouse=True)
def _clean():
    _wipe("tenant_id IN (:a, :b)", {"a": TENANT, "b": TENANT_OTHER})
    yield
    _wipe("tenant_id IN (:a, :b)", {"a": TENANT, "b": TENANT_OTHER})


@pytest.fixture(scope="module", autouse=True)
def _tenants():
    admin = _admin_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT, "perf-a"), (TENANT_OTHER, "perf-b")):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, :slug, 'active') ON CONFLICT (slug) DO NOTHING"
                ),
                {"id": tid, "slug": slug},
            )
    yield
    with admin.begin() as conn:
        sub = "(SELECT id FROM tenants WHERE slug LIKE 'perf-%')"
        for table in _TEARDOWN:
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id IN {sub}"))  # noqa: S608
        conn.execute(text("DELETE FROM tenants WHERE slug LIKE 'perf-%'"))
    admin.dispose()


async def _with_session(tenant: str, fn):
    engine = create_engine(APP_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant}
            )
            try:
                result = await fn(session)
            except Exception:
                await session.rollback()
                raise
            await session.commit()
            return result
    finally:
        await engine.dispose()


def _add_agent(tenant: str, ref: str, *, max_concurrent: int = 5) -> None:
    from platform_core.cases.assignment import upsert_agent

    _run(
        _with_session(
            tenant,
            lambda s: upsert_agent(
                s,
                tenant_id=uuid.UUID(tenant),
                user_ref=ref,
                display_name=ref,
                max_concurrent=max_concurrent,
            ),
        )
    )


def _open_case(tenant: str, ref: uuid.UUID, *, assignee: str | None = None) -> uuid.UUID:
    """A case linked to a conversation, assigned through the normal command."""
    from platform_core.cases.service import CaseService

    async def create(session):
        case = await CaseService(session).create_case(
            tenant_id=uuid.UUID(tenant),
            subject="加急咨询",
            priority="p2",
            conversation_ref_id=ref,
        )
        if assignee:
            await CaseService(session).apply_command(
                tenant_id=uuid.UUID(tenant),
                case_id=case.id,
                command="assign",
                parameters={"assignee_ref": assignee},
            )
        return case.id

    return _run(_with_session(tenant, create))


def _reply(tenant: str, ref: uuid.UUID, body: str, agent: str, **kwargs):
    from platform_core.agent_runtime.agent_reply import send_agent_reply

    return _run(
        _with_session(
            tenant,
            lambda s: send_agent_reply(
                s,
                tenant_id=uuid.UUID(tenant),
                conversation_ref_id=ref,
                text=body,
                agent_ref=agent,
                **kwargs,
            ),
        )
    )


def _report(tenant: str, window_seconds: int = 7 * 24 * 3600):
    from platform_core.evaluation.agent_metrics import agent_performance_report

    return _run(
        _with_session(
            tenant,
            lambda s: agent_performance_report(
                s, tenant_id=uuid.UUID(tenant), window_seconds=window_seconds
            ),
        )
    )


# --- the defect fix --------------------------------------------------------


def test_an_agent_reply_satisfies_the_first_response_clock() -> None:
    """Without this an agent can answer and the case still escalates for "no
    first response" - the customer is served and the SLA says otherwise."""
    ref = uuid.uuid4()
    case_id = _open_case(TENANT, ref, assignee="agent-1")
    _add_agent(TENANT, "agent-1")

    _reply(TENANT, ref, "已为您加急。", "agent-1")

    admin = _admin_engine(ADMIN_URL)
    with admin.begin() as conn:
        row = conn.execute(
            text("SELECT first_responded_at, version FROM cases WHERE tenant_id = :t AND id = :c"),
            {"t": TENANT, "c": str(case_id)},
        ).one()
    admin.dispose()
    assert row[0] is not None, "the reply must satisfy the first-response clock"
    # Recorded through the case command, so the version moved with it.
    assert int(row[1]) > 1


def test_a_second_reply_does_not_move_the_first_response() -> None:
    """First response is a fact about the first one, not the latest."""
    ref = uuid.uuid4()
    case_id = _open_case(TENANT, ref, assignee="agent-1")
    _add_agent(TENANT, "agent-1")

    _reply(TENANT, ref, "第一条", "agent-1")
    admin = _admin_engine(ADMIN_URL)
    with admin.begin() as conn:
        first = conn.execute(
            text("SELECT first_responded_at FROM cases WHERE id = :c"), {"c": str(case_id)}
        ).scalar()
    admin.dispose()

    _reply(TENANT, ref, "第二条", "agent-1")
    admin = _admin_engine(ADMIN_URL)
    with admin.begin() as conn:
        second = conn.execute(
            text("SELECT first_responded_at FROM cases WHERE id = :c"), {"c": str(case_id)}
        ).scalar()
    admin.dispose()
    assert first == second


def test_a_reply_with_no_linked_case_still_sends() -> None:
    """A conversation with no case is the common case on `/support`; it must not
    block the reply."""
    result = _reply(TENANT, uuid.uuid4(), "你好。", "agent-1")
    assert result.turn_id is not None


# --- the report ------------------------------------------------------------


def test_reply_counts_and_adoption_are_attributed_to_the_author() -> None:
    ref = uuid.uuid4()
    _reply(TENANT, ref, "建议话术原样发送", "agent-a", origin="ai_suggestion")
    _reply(TENANT, ref, "模板", "agent-a", origin="canned")
    # `free` is an explicit value, not the default: a client that reports
    # provenance says "the agent typed this", and that is a different fact from
    # a client that reports nothing (which leaves the origin unknown).
    _reply(TENANT, ref, "自己写的", "agent-a", origin="free")

    report = _report(TENANT)
    stat = next(a for a in report.agents if a.user_ref == "agent-a")

    assert stat.replies_sent == 3
    assert stat.replies_from_ai_suggestion == 1
    assert stat.replies_from_canned == 1
    assert stat.replies_free == 1
    assert stat.replies_unknown_origin == 0
    # 1 of 3 - all three reported an origin, so all three are in the denominator.
    assert stat.ai_suggestion_adoption == pytest.approx(1 / 3, abs=1e-4)


def test_an_unknown_origin_is_reported_and_excluded_from_the_denominator() -> None:
    """A client that reports nothing is not evidence that agents type by hand."""
    ref = uuid.uuid4()
    _reply(TENANT, ref, "有来源", "agent-b", origin="ai_suggestion")

    # A row with no origin at all, as a client that does not report provenance
    # would produce. Written directly because the service refuses unknown values.
    admin = _admin_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO conversation_turns (id, tenant_id, conversation_ref_id, role, "
                "text_redacted, text_hash, ts, source, created_at, origin, author_ref) VALUES "
                "(:i, :t, :r, 'agent', 'x', 'h', :ts, 'agent', :ts, '', 'agent-b')"
            ),
            {"i": str(uuid.uuid4()), "t": TENANT, "r": str(ref), "ts": int(time.time())},
        )
    admin.dispose()

    stat = next(a for a in _report(TENANT).agents if a.user_ref == "agent-b")
    assert stat.replies_sent == 2
    assert stat.replies_unknown_origin == 1
    # 1 of 1 known-origin reply, not 1 of 2.
    assert stat.ai_suggestion_adoption == 1.0


def test_a_rate_with_no_denominator_is_none_not_zero() -> None:
    """ "Nobody replied" and "everybody typed from scratch" are opposite facts."""
    _add_agent(TENANT, "quiet")

    stat = next(a for a in _report(TENANT).agents if a.user_ref == "quiet")
    assert stat.replies_sent == 0
    assert stat.ai_suggestion_adoption is None
    assert stat.canned_adoption is None
    assert stat.first_time_fix_rate is None
    assert stat.first_response_minutes_p50 is None


def test_an_unknown_origin_value_is_refused() -> None:
    """A typo would silently dilute the rate it exists to measure."""
    from platform_core.agent_runtime.agent_reply import AgentReplyError

    with pytest.raises(AgentReplyError, match="unknown reply origin"):
        _reply(TENANT, uuid.uuid4(), "x", "agent-1", origin="hand_typed")


def test_utilisation_and_open_load_come_from_the_directory() -> None:
    _add_agent(TENANT, "busy", max_concurrent=2)
    for _ in range(2):
        _open_case(TENANT, uuid.uuid4(), assignee="busy")

    stat = next(a for a in _report(TENANT).agents if a.user_ref == "busy")
    assert stat.open_cases == 2
    assert stat.max_concurrent == 2
    assert stat.utilisation == 1.0


def test_an_assignment_the_directory_does_not_know_is_counted_not_dropped() -> None:
    """Someone typed a name by hand, or an agent left. Hiding it would make the
    queue look smaller than it is."""
    _open_case(TENANT, uuid.uuid4(), assignee="ghost")

    report = _report(TENANT)
    assert report.orphaned_open_cases == 1
    assert any(a.user_ref == "ghost" for a in report.agents)


def test_unassigned_open_cases_are_reported_separately() -> None:
    _open_case(TENANT, uuid.uuid4())

    report = _report(TENANT)
    assert report.unassigned_open_cases >= 1


def test_another_tenant_sees_none_of_our_agents() -> None:
    _add_agent(TENANT, "ours")
    _reply(TENANT, uuid.uuid4(), "hello", "ours")

    report = _report(TENANT_OTHER)
    assert report.agents == []
    assert report.ai_suggestion_adoption is None
