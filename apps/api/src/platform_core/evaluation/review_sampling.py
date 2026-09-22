"""Feature list 9.5: sampling for human review.

Two properties decide whether this tool is used or ignored:

- **It oversamples what is likely to be wrong.** Uniform random sampling of a
  healthy platform returns overwhelmingly correct answers, and a reviewer who
  finds nothing wrong in fifty samples stops reviewing. Worth reviewing is not
  the same as random: an abstention, a handoff, a low-confidence answer and a
  failure are the outcomes where "was that the right call?" has a real chance
  of being "no".
- **It is reproducible.** `select_review_sample(seed=N)` returns the same rows
  every time, so two reviewers looking at different weeks are comparable, and
  "this sample changed" means the platform changed rather than the dice did.
  Sampling without a seed is a measurement you cannot repeat.

What it deliberately does not do: decide whether an answer was *correct*. That
is a human judgement recorded elsewhere (`answer_corrections`). This module
only picks what to look at, and says why each row was picked - a reviewer
handed a row with no stated reason has to guess what they are checking for.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

# Why a run was drawn, in the order the strata are filled. A reviewer reading
# "you are looking at this because it abstained" can judge the abstention; the
# same row with no reason forces them to reconstruct the whole decision.
STRATUM_ABSTAINED = "abstained"
STRATUM_HANDOFF = "handed_off"
STRATUM_FAILED = "failed"
STRATUM_LOW_CONFIDENCE = "low_confidence"
STRATUM_ROUTINE = "routine"

# Answers below this classification confidence get their own stratum. It is the
# same threshold the router uses to degrade to CLARIFY, reused because "the
# router was unsure" is exactly the signal a reviewer cannot see from the text.
LOW_CONFIDENCE = 0.5

# How much of a sample goes to the risky strata when they have enough rows to
# fill it. The remainder goes to routine answers, which is what keeps the
# sample honest: a sample of only failures cannot detect a regression that
# makes good answers worse.
RISKY_SHARE = 0.8


@dataclass(frozen=True)
class ReviewSample:
    """One run a human should look at, and why."""

    run_id: str
    stratum: str
    route: str
    status: str
    reason: str


def _risk_stratum(status: str) -> str | None:
    if status == "abstained":
        return STRATUM_ABSTAINED
    if status == "handed_off":
        return STRATUM_HANDOFF
    if status == "failed":
        return STRATUM_FAILED
    return None


def _stable_key(run_id: str, seed: str) -> str:
    """Deterministic shuffle key.

    A reviewer must be able to re-draw last week's sample, so the ordering
    cannot come from `random` without a seeded instance, and cannot come from
    insertion order (which would bias towards whatever the query returned
    first). Hashing the run id with the seed is stable across processes and
    has no state to lose.
    """
    return hashlib.sha256(f"{seed}:{run_id}".encode()).hexdigest()


def select_review_sample(
    runs: list[dict],
    *,
    size: int,
    seed: str,
) -> list[ReviewSample]:
    """Pick `size` runs to review, risky outcomes first.

    `runs` items are plain dicts with at least `id`, `route`, `status`, and
    optionally `confidence` and `abstain_reason` - the shape the caller already
    has, so this stays a pure function and testable without a database.
    """
    if size <= 0 or not runs:
        return []

    strata: dict[str, list[dict]] = {
        STRATUM_ABSTAINED: [],
        STRATUM_HANDOFF: [],
        STRATUM_FAILED: [],
        STRATUM_LOW_CONFIDENCE: [],
        STRATUM_ROUTINE: [],
    }
    for run in runs:
        status = str(run.get("status") or "")
        stratum = _risk_stratum(status)
        if stratum is None:
            confidence = run.get("confidence")
            try:
                value = float(confidence) if confidence is not None else 1.0
            except (TypeError, ValueError):
                value = 1.0
            stratum = STRATUM_LOW_CONFIDENCE if value < LOW_CONFIDENCE else STRATUM_ROUTINE
        strata[stratum].append(run)

    for rows in strata.values():
        rows.sort(key=lambda run: _stable_key(str(run.get("id") or ""), seed))

    risky = [
        STRATUM_ABSTAINED,
        STRATUM_HANDOFF,
        STRATUM_FAILED,
        STRATUM_LOW_CONFIDENCE,
    ]
    risky_budget = int(size * RISKY_SHARE)
    routine_budget = size - risky_budget

    picked: list[tuple[str, dict]] = []
    remaining_risky = risky_budget
    # Fill the risky strata in priority order, each taking what is left of the
    # budget; a stratum with fewer rows than its share simply yields them all
    # and the next one picks up the slack.
    for index, stratum in enumerate(risky):
        if remaining_risky <= 0:
            break
        rows = strata[stratum]
        still_to_come = sum(len(strata[s]) for s in risky[index + 1 :])
        take = min(len(rows), max(0, remaining_risky - still_to_come))
        picked.extend((stratum, row) for row in rows[:take])
        remaining_risky -= take

    # Whatever the risky strata could not fill goes to routine answers rather
    # than being dropped: asking for ten samples and getting eight would quietly
    # undersize every review on a healthy week, which is exactly when the
    # sample being large enough is what makes it mean anything.
    routine_need = routine_budget + max(0, remaining_risky)
    picked.extend((STRATUM_ROUTINE, row) for row in strata[STRATUM_ROUTINE][:routine_need])

    return [
        ReviewSample(
            run_id=str(row.get("id") or ""),
            stratum=stratum,
            route=str(row.get("route") or ""),
            status=str(row.get("status") or ""),
            reason=str(row.get("abstain_reason") or "") if stratum == STRATUM_ABSTAINED else "",
        )
        for stratum, row in picked[:size]
    ]


__all__ = ["LOW_CONFIDENCE", "RISKY_SHARE", "ReviewSample", "select_review_sample"]
