"""Unit tests: the escalation ladder, as pure decisions.

The rules here are the ones that decide whether a missed commitment produces a
notification, a duplicate notification, or silence - and all three outcomes
look identical from outside until someone checks the ledger. Hence a test per
rule rather than a comment.
"""

from __future__ import annotations

import uuid

from platform_core.cases.escalation import (
    CLOCK_FIRST_RESPONSE,
    CLOCK_RESOLUTION,
    DEFAULT_ESCALATION_TEAM,
    ESCALATION_LADDER_SECONDS,
    TERMINAL_STATUSES,
    level_for,
    route_for,
    targets_for_case,
)
from platform_core.cases.models import Case

TENANT = uuid.UUID("0190d000-0000-7000-8000-00000000000a")
NOW = 1_800_000_000
WINDOW = 3600


def _case(
    *,
    status: str = "in_progress",
    first_response_due_at: int | None = None,
    resolution_due_at: int | None = None,
    first_responded_at: int | None = None,
    resolved_at: int | None = None,
    team_ref: str | None = None,
) -> Case:
    case = Case(
        tenant_id=TENANT,
        subject="Refund not received",
    )
    case.id = uuid.uuid4()
    case.status = status
    case.opened_at = NOW - 10_000
    case.first_response_due_at = first_response_due_at
    case.resolution_due_at = resolution_due_at
    case.first_responded_at = first_responded_at
    case.resolved_at = resolved_at
    case.team_ref = team_ref
    case.assignee_ref = None
    return case


# --- level_for --------------------------------------------------------------


def test_a_deadline_that_has_not_passed_escalates_at_nothing() -> None:
    assert level_for(deadline=NOW, now=NOW) == 0
    assert level_for(deadline=NOW + 1, now=NOW) == 0


def test_the_first_level_fires_as_soon_as_the_deadline_is_past() -> None:
    """`now > deadline`, matching `is_breached`. Two definitions of "breached"
    that differ by a second is how a Case ends up escalated and not-breached at
    the same time."""
    assert level_for(deadline=NOW - 1, now=NOW) == 1


def test_the_second_level_is_measured_from_the_deadline_not_from_level_one() -> None:
    """So a scanner that was down for two hours lands on the right rung instead
    of walking the ladder one step per poll."""
    second_offset = ESCALATION_LADDER_SECONDS[1]
    assert level_for(deadline=NOW - second_offset, now=NOW) == 2
    # A single poll long after the breach reaches the top rung directly.
    assert level_for(deadline=NOW - (second_offset * 10), now=NOW) == 2


# --- targets_for_case -------------------------------------------------------


def test_a_breached_first_response_clock_is_a_target() -> None:
    case = _case(first_response_due_at=NOW - 30)
    targets = targets_for_case(case, now=NOW, already=set())
    assert [(t.clock, t.level) for t in targets] == [(CLOCK_FIRST_RESPONSE, 1)]
    assert targets[0].breach_seconds == 30
    assert targets[0].reason_code == "SLA_FIRST_RESPONSE_BREACHED_L1"


def test_an_answered_clock_is_not_a_target() -> None:
    """The two clocks are answered by different fields: a Case can have replied
    to the customer and still not be resolved, and vice versa."""
    case = _case(
        first_response_due_at=NOW - 30,
        first_responded_at=NOW - 20,
        resolution_due_at=NOW - 10,
    )
    targets = targets_for_case(case, now=NOW, already=set())
    assert [t.clock for t in targets] == [CLOCK_RESOLUTION]


def test_both_clocks_can_be_due_at_once() -> None:
    case = _case(first_response_due_at=NOW - 30, resolution_due_at=NOW - 10)
    targets = targets_for_case(case, now=NOW, already=set())
    assert {t.clock for t in targets} == {CLOCK_FIRST_RESPONSE, CLOCK_RESOLUTION}
    assert len(targets) == 2


def test_an_already_recorded_rung_is_not_returned_again() -> None:
    case = _case(first_response_due_at=NOW - 30)
    targets = targets_for_case(case, now=NOW, already={(CLOCK_FIRST_RESPONSE, 1)})
    assert targets == []


def test_a_missed_poll_catches_up_every_rung_at_once() -> None:
    """Level 1 was never recorded (the worker was down), and the Case is now
    past level 2. Both rungs are due, in order, so the ledger reads as a
    ladder rather than as a jump to the top."""
    case = _case(first_response_due_at=NOW - (ESCALATION_LADDER_SECONDS[1] + 5))
    targets = targets_for_case(case, now=NOW, already=set())
    assert [(t.clock, t.level) for t in targets] == [
        (CLOCK_FIRST_RESPONSE, 1),
        (CLOCK_FIRST_RESPONSE, 2),
    ]


def test_only_the_missing_rung_is_emitted_when_level_one_was_recorded() -> None:
    case = _case(first_response_due_at=NOW - (ESCALATION_LADDER_SECONDS[1] + 5))
    targets = targets_for_case(case, now=NOW, already={(CLOCK_FIRST_RESPONSE, 1)})
    assert [(t.clock, t.level) for t in targets] == [(CLOCK_FIRST_RESPONSE, 2)]


def test_a_resolved_or_closed_case_is_not_escalated() -> None:
    """A Case resolved late is history, not an open commitment. Reopening moves
    it out of this set, and the clocks are live again."""
    for status in sorted(TERMINAL_STATUSES):
        case = _case(status=status, first_response_due_at=NOW - 30)
        assert targets_for_case(case, now=NOW, already=set()) == []


def test_a_waiting_case_is_still_scanned() -> None:
    """`WAITING_*` pauses a clock rather than stopping it. A Case waiting on
    the customer past its resolution deadline is precisely the one worth
    escalating, so excluding the waiting states here would silence the case
    that matters most."""
    case = _case(status="waiting_customer", resolution_due_at=NOW - 30)
    assert [t.clock for t in targets_for_case(case, now=NOW, already=set())] == [CLOCK_RESOLUTION]


def test_a_clock_with_no_deadline_is_skipped() -> None:
    """A Case opened before deadlines were computed, or one whose policy left
    the clock unset. Nothing to compare against, so nothing to escalate."""
    case = _case(first_response_due_at=None, resolution_due_at=None)
    assert targets_for_case(case, now=NOW, already=set()) == []


# --- route_for --------------------------------------------------------------


def test_level_one_notifies_without_rerouting() -> None:
    case = _case(first_response_due_at=NOW - 30)
    target = targets_for_case(case, now=NOW, already=set())[0]
    assert route_for(target) == (None, None)


def test_level_two_nominates_the_escalation_queue() -> None:
    case = _case(first_response_due_at=NOW - (ESCALATION_LADDER_SECONDS[1] + 5))
    targets = targets_for_case(case, now=NOW, already={(CLOCK_FIRST_RESPONSE, 1)})
    assert route_for(targets[0]) == (DEFAULT_ESCALATION_TEAM, None)
