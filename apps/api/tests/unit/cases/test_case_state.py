"""Unit tests: Case state machine + SLA math (ticket 21)."""

import pytest

from platform_core.cases.models import (
    DEFAULT_SLA,
    CaseStatus,
    TransitionNotAllowed,
    VersionConflict,
    check_transition,
    check_version,
    sla_deadline,
)


def test_happy_lifecycle() -> None:
    path = [
        (CaseStatus.NEW, CaseStatus.TRIAGED),
        (CaseStatus.TRIAGED, CaseStatus.IN_PROGRESS),
        (CaseStatus.IN_PROGRESS, CaseStatus.WAITING_CUSTOMER),
        (CaseStatus.WAITING_CUSTOMER, CaseStatus.IN_PROGRESS),
        (CaseStatus.IN_PROGRESS, CaseStatus.RESOLVED),
        (CaseStatus.RESOLVED, CaseStatus.CLOSED),
    ]
    for current, target in path:
        check_transition(current, target)  # must not raise


def test_reopen_path() -> None:
    check_transition(CaseStatus.CLOSED, CaseStatus.REOPENED)
    check_transition(CaseStatus.RESOLVED, CaseStatus.REOPENED)
    check_transition(CaseStatus.REOPENED, CaseStatus.IN_PROGRESS)


def test_illegal_shortcuts_rejected() -> None:
    with pytest.raises(TransitionNotAllowed):
        check_transition(CaseStatus.NEW, CaseStatus.RESOLVED)  # must triage first
    with pytest.raises(TransitionNotAllowed):
        check_transition(CaseStatus.CLOSED, CaseStatus.IN_PROGRESS)  # must reopen
    with pytest.raises(TransitionNotAllowed):
        check_transition(CaseStatus.WAITING_VENDOR, CaseStatus.WAITING_INTERNAL)
    with pytest.raises(TransitionNotAllowed):
        check_transition(CaseStatus.RESOLVED, CaseStatus.RESOLVED)


def test_optimistic_version_check() -> None:
    check_version(7, 7)
    with pytest.raises(VersionConflict):
        check_version(8, 7)
    # expected_version=None skips the check (read paths)
    check_version(8, None)


def test_sla_priority_multipliers() -> None:
    opened = 1_000_000
    # p0 first response: 60min * 0.25 = 15min = 900s
    p0 = sla_deadline(
        DEFAULT_SLA, priority="p0", opened_at=opened, elapsed_running_seconds=0, first_response=True
    )
    assert p0 == opened + 900
    # p2 default: 60min
    p2 = sla_deadline(
        DEFAULT_SLA, priority="p2", opened_at=opened, elapsed_running_seconds=0, first_response=True
    )
    assert p2 == opened + 3600
    # p3 resolution: 8h * 2 = 16h
    p3 = sla_deadline(
        DEFAULT_SLA,
        priority="p3",
        opened_at=opened,
        elapsed_running_seconds=0,
        first_response=False,
    )
    assert p3 == opened + 16 * 3600


def test_sla_accounts_for_elapsed_running_time() -> None:
    opened = 1_000_000
    # Already ran 30 minutes (1800s); p2 first-response target is 3600s.
    deadline = sla_deadline(
        DEFAULT_SLA,
        priority="p2",
        opened_at=opened,
        elapsed_running_seconds=1800,
        first_response=True,
    )
    assert deadline == opened + 1800


def test_sla_never_in_past_due_to_elapsed_overrun() -> None:
    opened = 1_000_000
    # Ran longer than the whole window: deadline floors at now, not past.
    deadline = sla_deadline(
        DEFAULT_SLA,
        priority="p2",
        opened_at=opened,
        elapsed_running_seconds=999_999,
        first_response=True,
    )
    assert deadline == opened


def test_pause_states_do_not_run_clock() -> None:
    assert DEFAULT_SLA.paused(CaseStatus.WAITING_CUSTOMER) is True
    assert DEFAULT_SLA.paused(CaseStatus.WAITING_VENDOR) is True
    assert DEFAULT_SLA.paused(CaseStatus.IN_PROGRESS) is False
    assert DEFAULT_SLA.paused(CaseStatus.REOPENED) is False
