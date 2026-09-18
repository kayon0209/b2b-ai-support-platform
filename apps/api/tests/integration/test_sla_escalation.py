"""Integration tests: the SLA escalation sweep.

Why these are integration tests: the guarantee that a Case is escalated **once**
is a unique constraint, not a Python check. A `SELECT`-then-`INSERT` in the
scanner looks correct in a unit test and loses the race under two workers, which
is the exact failure the ledger exists to prevent. So the race is exercised here,
in the database, where it can actually happen.

The rest of the assertions are about what an escalation leaves behind: the
ledger row, an audit event and a business event, all in one transaction - so
"we escalated" and "we said we escalated" cannot diverge.
"""

import asyncio
import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from platform_core.cases.escalation import (
    AUDIT_ESCALATED,
    CLOCK_FIRST_RESPONSE,
    DEFAULT_ESCALATION_TEAM,
    ESCALATION_LADDER_SECONDS,
    EscalationTarget,
    escalate,
    escalate_due_cases,
)
from platform_core.db import session_scope_with_url
from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = os.environ.get(
    "APP_DATABASE_URL",
    "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform",
)

TENANT = "0190d000-0000-7000-8000-0000000000f1"
TENANT_OTHER = "0190d000-0000-7000-8000-0000000000f2"

NOW = 1_800_000_000

_CLEAN: tuple[str, ...] = (
    "DELETE FROM case_escalations WHERE tenant_id IN (:a, :b)",
    "DELETE FROM cases WHERE tenant_id IN (:a, :b)",
    "DELETE FROM audit_events WHERE tenant_id IN (:a, :b)",
    "DELETE FROM outbox_events WHERE tenant_id IN (:a, :b)",
)


def _clean(conn) -> None:
    for stmt in _CLEAN:
        conn.execute(text(stmt), {"a": TENANT, "b": TENANT_OTHER})


def _run(coro):
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


@pytest.fixture(scope="module", autouse=True)
def seed_tenants():
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        for tid, slug in ((TENANT, "sla-t1"), (TENANT_OTHER, "sla-t2")):
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) VALUES "
                    "(:id, :slug, :name, 'active') "
                    "ON CONFLICT (slug) DO UPDATE SET id = EXCLUDED.id, status = 'active'"
                ),
                {"id": tid, "slug": slug, "name": slug},
            )
    yield
    with admin.begin() as conn:
        _clean(conn)
        conn.execute(text("DELETE FROM tenants WHERE slug LIKE 'sla-t%'"))
    admin.dispose()


@pytest.fixture(autouse=True)
def clean_rows():
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        _clean(conn)
    yield
    with admin.begin() as conn:
        _clean(conn)
    admin.dispose()


def _seed_case(
    *,
    tenant: str = TENANT,
    status: str = "in_progress",
    first_response_due_at: int | None = None,
    resolution_due_at: int | None = None,
    first_responded_at: int | None = None,
    resolved_at: int | None = None,
    team_ref: str | None = None,
) -> str:
    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as conn:
            case_id = conn.execute(
                text(
                    "INSERT INTO cases (id, tenant_id, subject, description, category, "
                    "priority, status, version, opened_at, last_state_changed_at, "
                    "elapsed_running_seconds, first_response_due_at, resolution_due_at, "
                    "first_responded_at, resolved_at, team_ref, metadata) VALUES "
                    "(gen_random_uuid(), :t, 'Refund not received', '', 'general', "
                    "'p2', :status, 1, :opened, :opened, 0, :fr, :res, :frat, :rat, "
                    ":team, '{}') RETURNING id"
                ),
                {
                    "t": tenant,
                    "status": status,
                    "opened": NOW - 86_400,
                    "fr": first_response_due_at,
                    "res": resolution_due_at,
                    "frat": first_responded_at,
                    "rat": resolved_at,
                    "team": team_ref,
                },
            ).scalar_one()
    finally:
        admin.dispose()
    return str(case_id)


def _ctx(tenant: str = TENANT) -> TenantContext:
    return TenantContext(tenant_id=uuid.UUID(tenant), actor_id=None, actor_kind="service")


def _sweep(*, tenant: str = TENANT, now: int = NOW) -> dict:
    ctx = _ctx(tenant)

    async def _drive() -> dict:
        async with session_scope_with_url(APP_URL) as session:
            await apply_rls_tenant(session, ctx)
            stats = await escalate_due_cases(session, tenant_id=ctx.tenant_id, ctx=ctx, now=now)
            await session.commit()
            return {
                "scanned": stats.scanned,
                "escalated": stats.escalated,
                "skipped": stats.skipped_already_escalated,
            }

    return _run(_drive())


def _ledger(tenant: str = TENANT) -> list[tuple]:
    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as conn:
            return list(
                conn.execute(
                    text(
                        "SELECT case_id, clock, level, reason_code, breach_seconds, team_ref "
                        "FROM case_escalations WHERE tenant_id = :t ORDER BY clock, level"
                    ),
                    {"t": tenant},
                ).all()
            )
    finally:
        admin.dispose()


def _count(table: str, tenant: str = TENANT, *, event_type: str | None = None) -> int:
    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as conn:
            if event_type is None:
                return int(
                    conn.execute(
                        text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t"),  # noqa: S608
                        {"t": tenant},
                    ).scalar_one()
                )
            return int(
                conn.execute(
                    text(
                        f"SELECT count(*) FROM {table} "  # noqa: S608
                        "WHERE tenant_id = :t AND event_type = :e"
                    ),
                    {"t": tenant, "e": event_type},
                ).scalar_one()
            )
    finally:
        admin.dispose()


# --- the sweep --------------------------------------------------------------


def test_a_breached_case_is_escalated() -> None:
    case_id = _seed_case(first_response_due_at=NOW - 30)

    stats = _sweep()

    assert stats["escalated"] == 1
    rows = _ledger()
    assert len(rows) == 1
    assert str(rows[0][0]) == case_id
    assert rows[0][1] == CLOCK_FIRST_RESPONSE
    assert rows[0][2] == 1
    assert rows[0][3] == "SLA_FIRST_RESPONSE_BREACHED_L1"
    # How far past the deadline it was *at escalation time*. Recorded rather
    # than derived, because the clock keeps running.
    assert rows[0][4] == 30


def test_a_second_sweep_changes_nothing() -> None:
    _seed_case(first_response_due_at=NOW - 30)

    assert _sweep()["escalated"] == 1
    second = _sweep()

    assert second["escalated"] == 0
    assert len(_ledger()) == 1


def test_a_case_that_is_not_yet_breached_is_not_touched() -> None:
    _seed_case(first_response_due_at=NOW + 60, resolution_due_at=NOW + 3600)

    assert _sweep() == {"scanned": 0, "escalated": 0, "skipped": 0}
    assert _ledger() == []


def test_the_sweep_records_an_audit_event_and_a_business_event() -> None:
    """Both in the escalation's own transaction: a ledger row without the audit
    event would mean a commitment was missed with no record of who was told."""
    _seed_case(first_response_due_at=NOW - 30)

    _sweep()

    assert _count("audit_events", event_type=None) >= 1
    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as conn:
            action = conn.execute(
                text(
                    "SELECT action FROM audit_events WHERE tenant_id = :t "
                    "ORDER BY occurred_at DESC LIMIT 1"
                ),
                {"t": TENANT},
            ).scalar_one()
    finally:
        admin.dispose()
    assert action == AUDIT_ESCALATED
    assert _count("outbox_events", event_type="case.sla_breached") == 1


def test_the_second_level_routes_an_unassigned_case() -> None:
    _seed_case(first_response_due_at=NOW - (ESCALATION_LADDER_SECONDS[1] + 5))

    assert _sweep()["escalated"] == 2
    assert [row[2] for row in _ledger()] == [1, 2]

    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as conn:
            assert (
                conn.execute(
                    text("SELECT team_ref FROM cases WHERE tenant_id = :t"), {"t": TENANT}
                ).scalar_one()
                == DEFAULT_ESCALATION_TEAM
            )
    finally:
        admin.dispose()


def test_the_second_level_does_not_overwrite_a_human_routing_decision() -> None:
    """Someone already assigned this Case to their own team. Re-routing their
    work is how an escalation system gets switched off."""
    _seed_case(
        first_response_due_at=NOW - (ESCALATION_LADDER_SECONDS[1] + 5),
        team_ref="team-payments",
    )

    _sweep()

    assert len(_ledger()) == 2
    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as conn:
            assert (
                conn.execute(
                    text("SELECT team_ref FROM cases WHERE tenant_id = :t"), {"t": TENANT}
                ).scalar_one()
                == "team-payments"
            )
    finally:
        admin.dispose()


def test_a_resolved_case_is_never_escalated() -> None:
    _seed_case(
        status="resolved",
        first_response_due_at=NOW - 30,
        resolution_due_at=NOW - 10,
        resolved_at=NOW - 20,
    )

    assert _sweep()["escalated"] == 0
    assert _ledger() == []


def test_an_answered_clock_is_never_escalated() -> None:
    _seed_case(
        first_response_due_at=NOW - 30,
        first_responded_at=NOW - 20,
        resolution_due_at=NOW + 600,
    )

    assert _sweep()["escalated"] == 0
    assert _ledger() == []


def test_another_tenants_breached_case_is_not_touched() -> None:
    """Tenant isolation on the write path, not just the read path: a sweep
    bound to one tenant must not escalate another tenant's Case, and the other
    tenant's row must be invisible to it."""
    foreign_case = _seed_case(tenant=TENANT_OTHER, first_response_due_at=NOW - 30)
    _seed_case(first_response_due_at=NOW - 30)

    assert _sweep()["escalated"] == 1

    assert len(_ledger()) == 1
    assert _ledger(TENANT_OTHER) == []
    assert foreign_case


# --- the constraint is the guarantee ---------------------------------------


def test_the_database_refuses_a_duplicate_rung() -> None:
    """The race the ledger exists for: two workers both read "not yet
    escalated". Only the constraint can decide it, so it is asserted directly
    through `escalate` twice, bypassing the scanner's own filter."""
    case_id = _seed_case(first_response_due_at=NOW - 30)
    ctx = _ctx()
    target = EscalationTarget(
        case_id=uuid.UUID(case_id),
        clock=CLOCK_FIRST_RESPONSE,
        level=1,
        deadline=NOW - 30,
        breach_seconds=30,
        reason_code="SLA_FIRST_RESPONSE_BREACHED_L1",
    )

    async def _drive() -> tuple[bool, bool]:
        async with session_scope_with_url(APP_URL) as session:
            await apply_rls_tenant(session, ctx)
            first = await _escalate_once(session, ctx, case_id, target)
            second = await _escalate_once(session, ctx, case_id, target)
            await session.commit()
            return first, second

    first, second = _run(_drive())
    assert (first, second) == (True, False)
    assert len(_ledger()) == 1


async def _escalate_once(session, ctx, case_id: str, target: EscalationTarget) -> bool:
    from platform_core.cases.models import Case

    case = await session.get(Case, uuid.UUID(case_id))
    assert case is not None
    return await escalate(session, ctx=ctx, case=case, target=target)


def test_a_raw_duplicate_insert_is_rejected_by_the_constraint() -> None:
    case_id = _seed_case(first_response_due_at=NOW - 30)
    admin = create_engine(ADMIN_URL)
    insert = text(
        "INSERT INTO case_escalations (id, tenant_id, case_id, clock, level, reason_code, "
        "breach_seconds, escalated_at) VALUES (gen_random_uuid(), :t, :c, 'first_response', "
        "1, 'SLA_FIRST_RESPONSE_BREACHED_L1', 30, :now)"
    )
    try:
        with admin.begin() as conn:
            conn.execute(insert, {"t": TENANT, "c": case_id, "now": NOW})
        with pytest.raises(IntegrityError):
            with admin.begin() as conn:
                conn.execute(insert, {"t": TENANT, "c": case_id, "now": NOW})
    finally:
        admin.dispose()


# --- the worker loop is actually wired --------------------------------------


def test_the_worker_sweep_reaches_a_seeded_case() -> None:
    """`drain_sla_once` sweeps every active tenant, so this asserts on our own
    Case rather than on global counts: other tenants in a shared development
    database are not this test's business."""
    from worker.sla_consumer import drain_sla_once

    case_id = _seed_case(first_response_due_at=NOW - 30)

    stats = _run(drain_sla_once(now=NOW))

    assert stats.escalated >= 1
    assert str(_ledger()[0][0]) == case_id
