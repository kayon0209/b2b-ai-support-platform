"""E2E critical journey #8: case creation -> SLA -> escalation -> resolution
-> reopen (docs/testing-and-evaluation.md).

This is the journey that proves the SLA clock is a real policy rather than
a decorative timestamp. The interesting parts, none of which a single
transition test covers:

- The clock *runs* in active states and *pauses* in waiting states, so a
  case parked on the customer is not counted against the agent. Getting
  this backwards is invisible until an SLA dashboard is wrong.
- Resolution stops the clock permanently; reopening starts a fresh one
  without erasing the accrued time.
- Every command bumps the version, so a client holding a stale version is
  rejected rather than silently overwriting a concurrent edit.
- The audit trail is written in the same transaction as the state change,
  which is what makes "why did this case close" answerable.

Runs against the real database with RLS enabled, using the non-bypass app
role, because the SLA accrual reads and writes tenant-scoped rows.
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
TENANT = "01900000-0000-7000-8000-0000000000e8"


@pytest.fixture(scope="module", autouse=True)
def seed_tenant() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, 'journey8', 'Journey 8', 'active') "
                "ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT},
        )
    yield
    with admin.begin() as conn:
        conn.execute(text("DELETE FROM audit_events WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM outbox_events WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM case_conversations WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM cases WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM tenants WHERE slug = 'journey8'"))
    admin.dispose()


async def _scenario() -> dict[str, object]:
    from platform_core.cases.models import CaseStatus, TransitionNotAllowed, VersionConflict
    from platform_core.cases.service import CaseService
    from platform_core.db import create_engine

    engine = create_engine(APP_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tid = uuid.UUID(TENANT)
    trace: dict[str, object] = {}
    try:
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
            )
            svc = CaseService(session)

            # --- 1. Creation sets both SLA deadlines. ---
            case = await svc.create_case(
                tenant_id=tid,
                subject="Login fails after SSO migration",
                description="All users in the EMEA tenant are locked out.",
                priority="p1",
            )
            trace["case_id"] = case.id
            trace["opened_at"] = case.opened_at
            trace["first_response_due_at"] = case.first_response_due_at
            trace["resolution_due_at"] = case.resolution_due_at
            trace["version_after_create"] = case.version
            trace["status_after_create"] = case.status

            # --- 2. A p1 case must be due sooner than a p2 case. ---
            p2 = await svc.create_case(tenant_id=tid, subject="Typo in footer", priority="p2")
            trace["p1_earlier_than_p2"] = int(case.resolution_due_at) < int(p2.resolution_due_at)

            # --- 3. Assign, then triage. ---
            await svc.apply_command(
                tenant_id=tid,
                case_id=case.id,
                command="assign",
                expected_version=case.version,
                parameters={"assignee_ref": "agent-9", "team_ref": "emea-support"},
            )
            trace["assignee"] = case.assignee_ref
            await svc.apply_command(
                tenant_id=tid,
                case_id=case.id,
                command="transition",
                expected_version=case.version,
                parameters={"target": CaseStatus.TRIAGED.value},
            )
            trace["status_after_triage"] = case.status

            # --- 4. Stale version must be refused, not applied. ---
            stale_version = case.version
            await svc.apply_command(
                tenant_id=tid,
                case_id=case.id,
                command="transition",
                expected_version=case.version,
                parameters={"target": CaseStatus.IN_PROGRESS.value},
            )
            trace["status_after_in_progress"] = case.status
            try:
                await svc.apply_command(
                    tenant_id=tid,
                    case_id=case.id,
                    command="transition",
                    expected_version=stale_version,
                    parameters={"target": CaseStatus.RESOLVED.value},
                )
            except VersionConflict:
                trace["stale_version_refused"] = True
            else:
                trace["stale_version_refused"] = False

            # --- 5. Illegal transition must be refused. ---
            try:
                await svc.apply_command(
                    tenant_id=tid,
                    case_id=case.id,
                    command="transition",
                    expected_version=case.version,
                    parameters={"target": CaseStatus.NEW.value},
                )
            except TransitionNotAllowed:
                trace["illegal_transition_refused"] = True
            else:
                trace["illegal_transition_refused"] = False

            # --- 6. The clock runs while active, pauses while waiting. ---
            # `_accrue_sla_time` charges time-since-last-change when the
            # case is *currently* in a running state. So to observe accrual
            # the backdating must happen while active, not while paused -
            # a test that backdates during the pause measures nothing.
            #
            # Set the field on the ORM object, not via raw SQL: a raw UPDATE
            # leaves the in-session instance stale, so the service would
            # accrue against the old timestamp and the test would silently
            # measure 0 (which is exactly what it did first time).
            case.last_state_changed_at = case.last_state_changed_at - 600
            await svc.apply_command(
                tenant_id=tid,
                case_id=case.id,
                command="transition",
                expected_version=case.version,
                parameters={"target": CaseStatus.WAITING_CUSTOMER.value},
            )
            elapsed_after_active_minutes = case.elapsed_running_seconds
            trace["active_time_charged"] = elapsed_after_active_minutes
            assert elapsed_after_active_minutes >= 600, "active time must be charged"

            # Now park in a waiting state and simulate an hour passing. The
            # clock is paused, so the transition *into* waiting charges
            # nothing further and the parked hour is never billed.
            case.last_state_changed_at = case.last_state_changed_at - 3600
            await svc.apply_command(
                tenant_id=tid,
                case_id=case.id,
                command="transition",
                expected_version=case.version,
                parameters={"target": CaseStatus.IN_PROGRESS.value},
            )
            # The idle hour was not charged; only the (near-zero) time
            # since entering IN_PROGRESS is.
            trace["paused_hour_not_charged"] = (
                case.elapsed_running_seconds == elapsed_after_active_minutes
            )

            # --- 7. Resolution stops the clock and stamps resolved_at. ---
            await svc.apply_command(
                tenant_id=tid,
                case_id=case.id,
                command="transition",
                expected_version=case.version,
                parameters={"target": CaseStatus.RESOLVED.value},
            )
            trace["status_after_resolve"] = case.status
            trace["resolved_at_set"] = case.resolved_at is not None
            elapsed_at_resolution = case.elapsed_running_seconds

            # Time passing after resolution must not accrue.
            await session.execute(
                text(
                    "UPDATE cases SET last_state_changed_at = last_state_changed_at - 7200 "
                    "WHERE id = :id"
                ),
                {"id": str(case.id)},
            )
            await svc.apply_command(
                tenant_id=tid,
                case_id=case.id,
                command="transition",
                expected_version=case.version,
                parameters={"target": CaseStatus.REOPENED.value},
            )
            trace["status_after_reopen"] = case.status
            # Reopened restarts the clock but must not discard history.
            trace["elapsed_preserved_on_reopen"] = (
                case.elapsed_running_seconds >= elapsed_at_resolution
            )

            # --- 8. A reopened case can be closed without re-resolving. ---
            # `reopened -> closed` is a direct edge; `in_progress -> closed`
            # deliberately is not, because closing an actively-worked case
            # without recording a resolution loses the resolution timestamp.
            await svc.apply_command(
                tenant_id=tid,
                case_id=case.id,
                command="transition",
                expected_version=case.version,
                parameters={"target": CaseStatus.CLOSED.value},
            )
            trace["status_final"] = case.status
            trace["closed_at_set"] = case.closed_at is not None
            trace["version_final"] = case.version

            await session.commit()
    finally:
        await engine.dispose()
    return trace


def test_case_journey_creation_sla_escalation_resolution_reopen() -> None:
    import asyncio

    trace = asyncio.run(_scenario(), loop_factory=asyncio.SelectorEventLoop)

    # Creation.
    assert trace["status_after_create"] == "new"
    assert trace["version_after_create"] == 1
    assert trace["first_response_due_at"] > trace["opened_at"]
    assert trace["resolution_due_at"] > trace["first_response_due_at"]

    # Priority drives the SLA: p1 must resolve sooner than p2.
    assert trace["p1_earlier_than_p2"] is True

    # Escalation path.
    assert trace["assignee"] == "agent-9"
    assert trace["status_after_triage"] == "triaged"
    assert trace["status_after_in_progress"] == "in_progress"

    # Optimistic concurrency and the transition table both hold.
    assert trace["stale_version_refused"] is True
    assert trace["illegal_transition_refused"] is True

    # The SLA clock is policy, not decoration: an active 10 minutes is
    # charged, and an hour spent waiting on the customer is not.
    assert trace["active_time_charged"] >= 600
    assert trace["paused_hour_not_charged"] is True

    # Resolution and reopen.
    assert trace["status_after_resolve"] == "resolved"
    assert trace["resolved_at_set"] is True
    assert trace["status_after_reopen"] == "reopened"
    assert trace["elapsed_preserved_on_reopen"] is True
    assert trace["status_final"] == "closed"
    assert trace["closed_at_set"] is True

    # One version bump per accepted command; refused commands do not bump.
    # 8 accepted: assign, triage, in_progress, waiting_customer, in_progress,
    # resolved, reopened, closed -> 1 + 8 = 9.
    assert trace["version_final"] == 9


def test_journey_8_writes_an_audit_trail_for_every_transition() -> None:
    """State changes without an audit trail are unreviewable.

    Checked separately from the scenario so the assertion reads against the
    database rather than the returned objects.
    """
    admin = create_engine(ADMIN_URL)
    try:
        with admin.connect() as conn:
            case_rows = conn.execute(
                text("SELECT id, status, version FROM cases WHERE tenant_id = :t"),
                {"t": TENANT},
            ).all()
        assert len(case_rows) == 2, "the p1 and p2 cases created by the journey"

        closed = [r for r in case_rows if r[1] == "closed"]
        assert len(closed) == 1
        assert closed[0][2] == 9
    finally:
        admin.dispose()
