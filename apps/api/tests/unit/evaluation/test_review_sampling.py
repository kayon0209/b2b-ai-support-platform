"""Feature list 9.5: the sample is worth reviewing and can be drawn again.

The two properties that decide whether anyone keeps using this:

- **Risky outcomes come first.** A uniform sample of a healthy platform is
  nearly all correct answers, and a reviewer who finds nothing fifty times
  stops looking. Abstentions, handoffs, failures and low-confidence answers are
  where "was that the right call?" can actually be answered "no".
- **Reproducible.** Same seed, same rows. Two weeks must be comparable, and
  "the sample changed" has to mean the platform changed, not the dice.

Also pinned: the sample is *filled*. Asking for ten and getting eight would
silently undersize every review precisely when the platform is healthy enough
to have few risky rows - which is when a large sample matters most.
"""

from __future__ import annotations

from collections import Counter

from platform_core.evaluation.review_sampling import (
    STRATUM_ABSTAINED,
    STRATUM_FAILED,
    STRATUM_HANDOFF,
    STRATUM_LOW_CONFIDENCE,
    STRATUM_ROUTINE,
    select_review_sample,
)


def _completed(count: int, *, prefix: str = "c", confidence: float = 0.9) -> list[dict]:
    return [
        {
            "id": f"{prefix}{i}",
            "route": "knowledge_qa",
            "status": "completed",
            "confidence": confidence,
        }
        for i in range(count)
    ]


def _runs(*, completed: int = 20, abstained: int = 0, failed: int = 0, low: int = 0) -> list[dict]:
    rows = _completed(completed)
    rows += [
        {"id": f"a{i}", "route": "business_read", "status": "abstained", "abstain_reason": "X"}
        for i in range(abstained)
    ]
    rows += [{"id": f"f{i}", "route": "knowledge_qa", "status": "failed"} for i in range(failed)]
    rows += _completed(low, prefix="l", confidence=0.2)
    return rows


def test_a_healthy_platform_still_gets_a_full_sample() -> None:
    """Few risky rows must not silently shrink the review."""
    sample = select_review_sample(_runs(completed=30, abstained=2), size=10, seed="s")
    assert len(sample) == 10


def test_risky_outcomes_are_oversampled() -> None:
    sample = select_review_sample(_runs(completed=40, abstained=5), size=10, seed="s")
    counts = Counter(item.stratum for item in sample)
    # All 5 abstentions are drawn even though they are 11% of the population.
    # "Oversampled" means a larger *share* than they hold, not a larger count
    # than every other stratum - with only 5 risky rows the rest of the budget
    # legitimately goes to routine answers.
    assert counts[STRATUM_ABSTAINED] == 5
    assert counts[STRATUM_ABSTAINED] / len(sample) > 5 / 45


def test_routine_answers_are_still_included() -> None:
    """A sample of only failures cannot see good answers getting worse."""
    sample = select_review_sample(_runs(completed=40, abstained=5), size=10, seed="s")
    assert STRATUM_ROUTINE in Counter(item.stratum for item in sample)


def test_failures_and_handoffs_get_their_own_strata() -> None:
    rows = _runs(completed=10, failed=3)
    rows += [{"id": f"h{i}", "route": "knowledge_qa", "status": "handed_off"} for i in range(2)]
    sample = select_review_sample(rows, size=10, seed="s")
    counts = Counter(item.stratum for item in sample)
    assert counts[STRATUM_FAILED] + counts[STRATUM_HANDOFF] == 5


def test_low_confidence_answers_are_sampled_before_routine() -> None:
    sample = select_review_sample(_runs(completed=30, low=4), size=10, seed="s")
    counts = Counter(item.stratum for item in sample)
    assert counts[STRATUM_LOW_CONFIDENCE] == 4


def test_the_same_seed_draws_the_same_sample() -> None:
    rows = _runs(completed=30, abstained=5)
    first = select_review_sample(rows, size=10, seed="week-1")
    second = select_review_sample(rows, size=10, seed="week-1")
    assert [item.run_id for item in first] == [item.run_id for item in second]


def test_a_different_seed_draws_a_different_sample() -> None:
    rows = _runs(completed=30)
    first = select_review_sample(rows, size=10, seed="week-1")
    second = select_review_sample(rows, size=10, seed="week-2")
    assert [item.run_id for item in first] != [item.run_id for item in second]


def test_each_row_says_why_it_was_drawn() -> None:
    sample = select_review_sample(_runs(completed=5, abstained=3), size=5, seed="s")
    assert all(item.stratum for item in sample)
    abstained = [item for item in sample if item.stratum == STRATUM_ABSTAINED]
    assert abstained and all(item.reason for item in abstained)


def test_no_duplicate_rows_in_one_sample() -> None:
    sample = select_review_sample(_runs(completed=30, abstained=5), size=15, seed="s")
    ids = [item.run_id for item in sample]
    assert len(ids) == len(set(ids))


def test_zero_size_or_empty_population_yields_nothing() -> None:
    assert select_review_sample([], size=10, seed="s") == []
    assert select_review_sample(_runs(completed=5), size=0, seed="s") == []


def test_a_sample_never_exceeds_the_population() -> None:
    sample = select_review_sample(_runs(completed=3), size=10, seed="s")
    assert len(sample) == 3
