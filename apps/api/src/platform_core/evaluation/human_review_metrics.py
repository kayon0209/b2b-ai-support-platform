"""Design-aware, fail-closed metrics for stratified online human review."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
ReviewVerdict = Literal["agree", "override"]


@dataclass(frozen=True)
class ReviewSelection:
    run_id: str
    stratum: str


@dataclass(frozen=True)
class ReviewDecision:
    run_id: str
    verdict: ReviewVerdict


def summarize_stratified_review(
    population_by_stratum: Mapping[str, int],
    selections: Iterable[ReviewSelection],
    decisions: Iterable[ReviewDecision],
) -> dict[str, object]:
    """Estimate population override rate using the actual sampling strata.

    Weighted estimates are withheld unless every non-empty stratum was sampled
    and every selected item received a definitive review. This prevents a
    partial or selectively completed review from appearing representative.
    """
    if not isinstance(population_by_stratum, Mapping) or not population_by_stratum:
        raise ValueError("population counts are required")
    population: dict[str, int] = {}
    for stratum, count in population_by_stratum.items():
        if not isinstance(stratum, str) or not _SAFE_ID.fullmatch(stratum):
            raise ValueError("stratum names must be bounded identifiers")
        if type(count) is not int or count < 0:
            raise ValueError("population counts must be non-negative integers")
        population[stratum] = count
    if not any(population.values()):
        return {
            "status": "unavailable",
            "population_count": 0,
            "selected_count": 0,
            "reviewed_count": 0,
            "completion_rate": None,
            "weighted_override_rate": None,
            "by_stratum": {},
        }

    selected_rows = list(selections)
    decision_rows = list(decisions)
    selection_by_id: dict[str, str] = {}
    for selection in selected_rows:
        if (
            not isinstance(selection.run_id, str)
            or not _SAFE_ID.fullmatch(selection.run_id)
            or selection.run_id in selection_by_id
        ):
            raise ValueError("selected run ids must be safe and unique")
        if (
            not isinstance(selection.stratum, str)
            or selection.stratum not in population
            or population[selection.stratum] == 0
        ):
            raise ValueError("selection references a missing or empty population stratum")
        selection_by_id[selection.run_id] = selection.stratum

    selected_counts = Counter(selection_by_id.values())
    if any(selected_counts[name] > count for name, count in population.items()):
        raise ValueError("selected count exceeds the stratum population")

    reviewed: dict[str, str] = {}
    for decision in decision_rows:
        if (
            not isinstance(decision.run_id, str)
            or decision.run_id not in selection_by_id
            or decision.run_id in reviewed
        ):
            raise ValueError("review must uniquely reference a selected run")
        if not isinstance(decision.verdict, str) or decision.verdict not in {"agree", "override"}:
            raise ValueError("review verdict must be agree or override")
        reviewed[decision.run_id] = decision.verdict

    reviewed_counts = Counter(selection_by_id[run_id] for run_id in reviewed)
    override_counts = Counter(
        selection_by_id[run_id] for run_id, verdict in reviewed.items() if verdict == "override"
    )
    per_stratum: dict[str, dict[str, int | float | None]] = {}
    complete = True
    weighted_override_population = Decimal(0)
    population_total = sum(population.values())
    for stratum, population_count in sorted(population.items()):
        selected_count = selected_counts[stratum]
        reviewed_count = reviewed_counts[stratum]
        override_count = override_counts[stratum]
        if population_count and (not selected_count or reviewed_count != selected_count):
            complete = False
        override_rate = override_count / reviewed_count if reviewed_count else None
        per_stratum[stratum] = {
            "population": population_count,
            "selected": selected_count,
            "reviewed": reviewed_count,
            "completion_rate": (
                round(reviewed_count / selected_count, 4) if selected_count else None
            ),
            "override_count": override_count,
            "raw_override_rate": round(override_rate, 4) if override_rate is not None else None,
        }
        if population_count and reviewed_count:
            weighted_override_population += (
                Decimal(population_count) * Decimal(override_count) / Decimal(reviewed_count)
            )

    weighted_rate = (
        (weighted_override_population / Decimal(population_total)).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
        if complete
        else None
    )
    return {
        "status": "measured" if complete else "incomplete",
        "population_count": population_total,
        "selected_count": len(selected_rows),
        "reviewed_count": len(decision_rows),
        "completion_rate": (
            round(len(decision_rows) / len(selected_rows), 4) if selected_rows else None
        ),
        "weighted_override_rate": float(weighted_rate) if weighted_rate is not None else None,
        "sampling_weighting": "population_per_stratum / selected_per_stratum",
        "by_stratum": per_stratum,
    }


__all__ = [
    "ReviewDecision",
    "ReviewSelection",
    "summarize_stratified_review",
]
