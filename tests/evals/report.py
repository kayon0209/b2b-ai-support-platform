"""Per-category evaluation reporting (docs/testing-and-evaluation.md).

The doc is specific about how failures must be surfaced:

    "failures are reviewed by category, not hidden in an average score"

`EvalReport` computes run-wide aggregates, which is exactly the shape that
hides a collapse: a category with five cases can go to zero while the
headline pass rate moves by a couple of points. This module re-groups the
same results by `EvalCategory` so a regression has an address.

It also adds the two things an operator needs that a bare count does not
give: which cases failed, and which documented metric each failure maps to.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from platform_core.evaluation.runner import CaseResult, EvalReport

from .dataset import EvalCategory, cases_for

# Documented metric -> the reason codes that feed it. Keeps the mapping in
# one place instead of scattering string comparisons through assertions.
_REASON_TO_METRIC: dict[str, str] = {
    "UNSUPPORTED_CLAIM": "unsupported_claim_rate",
    "NO_CLAIMS": "unsupported_claim_rate",
    "REQUIRED_CLAIM_MISSING": "answer_relevance",
    "FORBIDDEN_CLAIM_PRESENT": "unsafe_action_refusal_rate",
}

# Failure classes that are safety-critical. A regression here is a P0
# regardless of the overall pass rate.
P0_REASON_CODES: frozenset[str] = frozenset(
    {"FORBIDDEN_CLAIM_PRESENT", "UNSUPPORTED_CLAIM", "NO_CLAIMS"}
)


def metric_for(reason_code: str) -> str:
    """Map a failure reason to the documented metric it regresses."""
    return _REASON_TO_METRIC.get(reason_code, "unclassified")


@dataclass
class CategoryResult:
    category: EvalCategory
    total: int
    passed: int
    failed: int
    failed_case_ids: list[str] = field(default_factory=list)
    reason_codes: dict[str, int] = field(default_factory=dict)
    has_p0_failure: bool = False

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 1.0

    def regressed_metrics(self) -> dict[str, int]:
        """Metric -> failure count for this category."""
        out: dict[str, int] = {}
        for reason, count in self.reason_codes.items():
            metric = metric_for(reason)
            out[metric] = out.get(metric, 0) + count
        return out


@dataclass
class CategoryReport:
    categories: list[CategoryResult]

    @property
    def p0_regressions(self) -> list[CategoryResult]:
        return [c for c in self.categories if c.has_p0_failure]

    def worst(self) -> CategoryResult | None:
        """Lowest pass rate; ties broken by the larger run.

        Returns the category a reviewer should open first.
        """
        if not self.categories:
            return None
        return min(self.categories, key=lambda c: (c.pass_rate, -c.total))


def build_category_report(report: EvalReport) -> CategoryReport:
    """Re-group flat results by category.

    Results are matched back to categories by position: `run()` appends one
    result per case in the order it received them, and categories are the
    order the dataset defines. Mismatched lengths mean the caller passed a
    hand-built case list - refuse rather than mis-attribute a failure.
    """
    expected: list[tuple[EvalCategory, str]] = [
        (category, case.case_id) for category in EvalCategory for case in cases_for(category)
    ]
    if len(report.results) != len(expected):
        raise ValueError(
            f"report has {len(report.results)} results but the dataset defines "
            f"{len(expected)} cases; a partial run cannot be reported by category"
        )

    grouped: dict[EvalCategory, CategoryResult] = {
        category: CategoryResult(category=category, total=0, passed=0, failed=0)
        for category in EvalCategory
    }

    for (category, case_id), result in zip(expected, report.results, strict=True):
        bucket = grouped[category]
        bucket.total += 1
        if result.passed:
            bucket.passed += 1
            continue
        bucket.failed += 1
        bucket.failed_case_ids.append(case_id)
        for reason in result.reason_codes:
            bucket.reason_codes[reason] = bucket.reason_codes.get(reason, 0) + 1
            if reason in P0_REASON_CODES:
                bucket.has_p0_failure = True

    return CategoryReport(categories=[grouped[c] for c in EvalCategory])


def summarize(report: CategoryReport) -> str:
    """Human-readable, category-first summary for CI logs."""
    lines = []
    for category in report.categories:
        lines.append(
            f"{category.category.value}: "
            f"{category.passed}/{category.total} "
            f"(rate={category.pass_rate:.2f})"
            + (f" FAILED={','.join(category.failed_case_ids)}" if category.failed else "")
        )
    return "\n".join(lines)


def failing_case_ids(report: CategoryReport) -> list[str]:
    return [cid for c in report.categories for cid in c.failed_case_ids]


__all__ = [
    "P0_REASON_CODES",
    "CategoryReport",
    "CategoryResult",
    "build_category_report",
    "failing_case_ids",
    "metric_for",
    "summarize",
    "CaseResult",
]
