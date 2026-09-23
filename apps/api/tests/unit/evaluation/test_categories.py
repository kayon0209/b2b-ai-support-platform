"""Unit tests: issue categories, the strict definition, and the state machine.

Four things here are worth a test rather than a comment, because each one is a
place the dashboard would otherwise lie:

1. **A re-asked question is not an automation.** Without the follow-up rule the
   automation rate measures how often the platform *spoke*, and a bot that
   answers every question twice scores 100%. That is the single most important
   assertion in this file.
2. **An unknown abstention reason goes to `routing`, never to `content`.**
   Guessing the cheap fix puts an unexplained reason on the "write a document"
   list and produces a document nobody needed.
3. **`routing` and `policy` are never proposable.** This is the guard against
   the trap every reference product names: chasing 100% self-service by
   automating complaints.
4. **A rate over an empty category is `None`, not `0.0`.** "Nobody asked" and
   "everybody escalated" are opposite facts and invite opposite actions.
"""

from __future__ import annotations

import time
import uuid

import pytest

from platform_core.evaluation.categories import (
    FOLLOW_UP_SECONDS,
    CategoryReport,
    CategoryStat,
    FixType,
    derive_category,
    fix_type_for,
)
from platform_core.evaluation.category_models import CategoryState
from platform_core.evaluation.category_service import (
    ALLOWED_TRANSITIONS,
    CategoryStateError,
    set_category_state,
)
from platform_core.evaluation.metrics import UNRECORDED


def _run(
    *,
    status: str,
    conversation: uuid.UUID | None = None,
    started_at: int | None = None,
    abstain_reason: str | None = None,
    route: str = "knowledge_qa",
) -> object:
    """A bare AgentRun. No session: these assertions are about the shape."""
    from platform_core.agent_runtime.models import AgentRun, RunStatus

    assert status in {s.value for s in RunStatus}
    return AgentRun(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        conversation_ref_id=conversation or uuid.uuid4(),
        route=route,
        status=status,
        started_at=started_at if started_at is not None else int(time.time()),
        abstain_reason=abstain_reason,
    )


# --- the category key -------------------------------------------------------


def test_the_key_is_deterministic() -> None:
    intent = {"business_line": "pcb", "scene": "order", "primary_kind": "order_status"}
    assert derive_category(intent) == derive_category(dict(intent))
    assert derive_category(intent) == "pcb|order|order_status"


def test_a_run_with_no_snapshot_lands_in_an_explicit_category() -> None:
    """A category that silently disappears is one nobody can fix."""
    assert derive_category(None) == f"{UNRECORDED}|{UNRECORDED}|{UNRECORDED}"
    assert derive_category({}) == f"{UNRECORDED}|{UNRECORDED}|{UNRECORDED}"


def test_a_value_containing_the_separator_cannot_collide_two_categories() -> None:
    """`|` is the key's grammar; a value carrying it must not merge keys."""
    a = derive_category({"business_line": "pcb", "scene": "a|b", "primary_kind": "k"})
    b = derive_category({"business_line": "pcb|a", "scene": "b", "primary_kind": "k"})
    assert a != b, "two different classifications collapsed onto one category"


def test_the_business_line_separates_the_same_question_in_two_lines() -> None:
    """Feature 3.2: one sentence means two things in two lines."""
    pcb = derive_category({"business_line": "pcb", "scene": "delivery", "primary_kind": "eta"})
    components = derive_category(
        {"business_line": "components", "scene": "delivery", "primary_kind": "eta"}
    )
    assert pcb != components


# --- what is missing --------------------------------------------------------


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("NO_AUTHORIZED_EVIDENCE", FixType.CONTENT),
        ("EVIDENCE_BELOW_THRESHOLD", FixType.CONTENT),
        ("CONFLICTING_SOURCES", FixType.CONTENT),
        ("TOOL_UNAVAILABLE", FixType.DATA),
        ("BUSINESS_READ_FLAG_OFF", FixType.DATA),
        ("ACTION_REQUEST", FixType.ACTION),
        ("WRITE_INTENT_UNCERTAIN", FixType.ACTION),
        ("REDLINE_COMMERCIAL_COMMITMENT", FixType.POLICY),
        ("STRATEGIC_ACCOUNT_REQUIRES_HUMAN", FixType.POLICY),
        ("COMPLAINT_REQUIRES_HUMAN", FixType.ROUTING),
        ("RESTRICTED_REQUEST", FixType.ROUTING),
    ],
)
def test_each_reason_maps_to_the_fix_it_needs(reason: str, expected: FixType) -> None:
    """The whole point of the object: volume alone does not say what to do."""
    assert fix_type_for(abstain_reason=reason) is expected


def test_an_answered_run_has_no_gap() -> None:
    assert fix_type_for(abstain_reason=None) is None
    assert fix_type_for(abstain_reason="") is None


def test_an_unknown_reason_is_sent_to_a_person_not_to_a_document() -> None:
    """Guessing `content` would produce a document nobody needed."""
    assert fix_type_for(abstain_reason="SOME_NEW_REASON_WE_HAVE_NOT_SEEN") is FixType.ROUTING


# --- the strict definition of "automated" -----------------------------------


def test_a_customer_who_asked_again_is_not_an_automation() -> None:
    """The assertion that makes the automation rate mean anything.

    Two COMPLETED runs on the same category: the platform answered twice, and
    the customer had to ask twice. Scoring both as automations is how a bot
    that never resolves anything reports 100%.
    """
    from platform_core.evaluation.categories import _followed_up, _is_automated

    conversation = uuid.uuid4()
    now = int(time.time())
    first = _run(status="completed", conversation=conversation, started_at=now)
    second = _run(
        status="completed", conversation=conversation, started_at=now + FOLLOW_UP_SECONDS - 1
    )
    keys = {first.id: "pcb|order|eta", second.id: "pcb|order|eta"}

    followed = _followed_up([first, second], keys)
    assert first.id in followed
    assert _is_automated(first, followed_up=True) is False
    # The second run resolved it: nothing came after.
    assert _is_automated(second, followed_up=False) is True


def test_a_later_question_on_another_category_is_not_a_failure() -> None:
    """ "Do you ship to Suzhou?" after an order question is a new question."""
    from platform_core.evaluation.categories import _followed_up

    conversation = uuid.uuid4()
    now = int(time.time())
    first = _run(status="completed", conversation=conversation, started_at=now)
    second = _run(status="completed", conversation=conversation, started_at=now + 60)
    keys = {first.id: "pcb|order|eta", second.id: "pcb|shipping|coverage"}

    assert _followed_up([first, second], keys) == set()


def test_a_run_that_did_not_complete_is_never_automated() -> None:
    from platform_core.evaluation.categories import _is_automated

    for status in ("abstained", "handed_off", "failed", "abandoned", "queued"):
        assert _is_automated(_run(status=status), followed_up=False) is False


def test_a_completed_run_that_recorded_an_abstention_is_not_automated() -> None:
    from platform_core.evaluation.categories import _is_automated

    run = _run(status="completed", abstain_reason="NO_AUTHORIZED_EVIDENCE")
    assert _is_automated(run, followed_up=False) is False


# --- the candidate list -----------------------------------------------------


def _stat(**kwargs: object) -> CategoryStat:
    base = {
        "category_key": "pcb|order|eta",
        "business_line": "pcb",
        "scene": "order",
        "primary_kind": "eta",
    }
    base.update(kwargs)
    return CategoryStat(**base)  # type: ignore[arg-type]


def test_a_category_with_no_runs_has_no_rate() -> None:
    """None, not 0.0 - see the module docstring, decision 3."""
    assert _stat().automation_rate is None
    assert CategoryReport(window_seconds=3600).automation_rate is None


@pytest.mark.parametrize("fix", [FixType.ROUTING, FixType.POLICY])
def test_a_decision_a_person_makes_is_never_proposed(fix: FixType) -> None:
    """The guard against chasing 100% self-service."""
    stat = _stat(runs=50, fix_type_counts={fix.value: 50}, leak_volume=50)
    assert stat.is_proposable is False


@pytest.mark.parametrize("fix", [FixType.CONTENT, FixType.DATA, FixType.ACTION])
def test_a_fixable_gap_is_proposed(fix: FixType) -> None:
    stat = _stat(runs=50, fix_type_counts={fix.value: 50}, leak_volume=50)
    assert stat.is_proposable is True


def test_human_only_is_excluded_even_when_the_gap_looks_fixable() -> None:
    """A marked decision outranks a derived classification."""
    stat = _stat(
        state=CategoryState.HUMAN_ONLY.value,
        runs=50,
        fix_type_counts={FixType.CONTENT.value: 50},
        leak_volume=50,
    )
    assert stat.is_proposable is False


def test_the_list_ranks_by_how_many_questions_it_would_stop_escalating() -> None:
    """Leak volume before rate: 0% of 200 beats 0% of 2."""
    big = _stat(category_key="a|b|c", runs=200, fix_type_counts={"content": 200}, leak_volume=200)
    small = _stat(category_key="d|e|f", runs=2, fix_type_counts={"content": 2}, leak_volume=2)
    report = CategoryReport(window_seconds=3600, categories=[small, big])
    assert [c.category_key for c in report.candidates] == ["a|b|c", "d|e|f"]


# --- the state machine ------------------------------------------------------


class _Session:
    """The smallest thing `set_category_state` needs: a session-ish object.

    `set_category_state` only calls `execute` and `flush`, and it `add`s the row
    the first time. Asserting on the state machine must not require a database.
    """

    def __init__(self) -> None:
        self.existing: object | None = None
        self.added: list[object] = []
        self.flushed = 0

    async def execute(self, _stmt: object) -> object:
        class _Result:
            def __init__(self, row: object | None) -> None:
                self._row = row

            def scalar_one_or_none(self) -> object | None:
                return self._row

        return _Result(self.existing)

    def add(self, row: object) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        self.flushed += 1


def _mark(session: _Session, *, state: str, fix_type: str = "") -> object:
    import asyncio

    return asyncio.run(
        set_category_state(
            session,  # type: ignore[arg-type]
            tenant_id=uuid.uuid4(),
            category_key="pcb|order|eta",
            state=state,
            actor_id=uuid.uuid4(),
            fix_type=fix_type,
        )
    )


def test_the_work_must_be_recorded_before_a_category_is_automated() -> None:
    """`observed -> automated` is a claim, not a state change."""
    with pytest.raises(CategoryStateError, match="before it was worked on"):
        _mark(_Session(), state="automated")


def test_a_human_only_category_cannot_be_promoted() -> None:
    """How a red line gets automated by a dropdown."""
    from platform_core.evaluation.category_models import IssueCategory

    session = _Session()
    session.existing = IssueCategory(
        tenant_id=uuid.uuid4(),
        category_key="pcb|order|eta",
        state=CategoryState.HUMAN_ONLY.value,
        created_at=0,
    )
    with pytest.raises(CategoryStateError, match="cannot move"):
        _mark(session, state="automated")


def test_illegal_transitions_are_named_rather_than_normalised() -> None:
    from platform_core.evaluation.category_models import IssueCategory

    session = _Session()
    session.existing = IssueCategory(
        tenant_id=uuid.uuid4(),
        category_key="pcb|order|eta",
        state=CategoryState.OBSERVED.value,
        created_at=0,
    )
    with pytest.raises(CategoryStateError, match="cannot move"):
        _mark(session, state="automating")


def test_an_unknown_state_is_rejected() -> None:
    with pytest.raises(CategoryStateError, match="unknown category state"):
        _mark(_Session(), state="heroic")


def test_an_unknown_fix_type_is_rejected_even_when_the_state_is_fine() -> None:
    """A bad fix type must fail here, not be stored and misread by the ranking."""
    with pytest.raises(CategoryStateError, match="unknown fix type"):
        _mark(_Session(), state="candidate", fix_type="vibes")


def test_leaving_automated_clears_the_baseline() -> None:
    """A stale stamp would report a regression that never happened."""
    from platform_core.evaluation.category_models import IssueCategory

    session = _Session()
    session.existing = IssueCategory(
        tenant_id=uuid.uuid4(),
        category_key="pcb|order|eta",
        state=CategoryState.AUTOMATED.value,
        created_at=0,
        automated_at=1,
    )
    row = _mark(session, state="automating")
    assert row.automated_at is None
    assert row.state == CategoryState.AUTOMATING.value


def test_every_state_has_a_declared_set_of_successors() -> None:
    """A machine with a hole in it is a text field with extra steps."""
    for state in CategoryState:
        assert state in ALLOWED_TRANSITIONS, f"{state} has no declared successors"
        # Every declared successor is a real state, and no state points at
        # itself - a self-transition would silently do nothing while looking
        # like it recorded something.
        for successor in ALLOWED_TRANSITIONS[state]:
            assert isinstance(successor, CategoryState)
            assert successor is not state
