"""Release gates (ticket 34, docs/testing-and-evaluation.md release gates).

A prompt, model, retrieval or policy change may ship only if every gate
passes. Gates are pure functions over an EvalReport (+ security suite
results) so CI and dashboards share one implementation.

Two inputs are not part of an EvalReport because they are not eval outcomes:

- the security counts, which come from the negative suites (`security=`);
- the read-tool success rate, which comes from live ToolExecution rows
  (`read_tools=`).

Both are passed in explicitly rather than defaulted, so a caller cannot get
a green gate by forgetting to supply the evidence.
"""

from dataclasses import dataclass
from typing import Any

from platform_core.evaluation.runner import EvalReport


@dataclass(frozen=True)
class GateThresholds:
    # Zero tolerance items
    cross_tenant_violations: int = 0
    unauthorized_writes: int = 0
    duplicate_replies: int = 0
    # Quality floors
    max_citation_violation_rate: float = 0.05  # >= 95% citation coverage
    min_abstention_correct_rate: float = 0.90
    max_forbidden_claim_rate: float = 0.02
    # docs/development-plan.md Phase 4: "Read-tool success excluding
    # third-party outage: >= 99%".
    min_read_tool_success_rate: float = 0.99
    # Iteration plan 1.3/4.1: retrieval recall@k over answerable cases that
    # declare expected corpus keys. Measured mean, gated like every other
    # floor; cases without expected keys do not dilute the denominator.
    min_retrieval_recall_at_k: float = 0.90


DEFAULT_THRESHOLDS = GateThresholds()

# At most half of read-tool traffic may be excused as third-party failure.
# Beyond that the "excluding outage" clause stops describing an incident and
# starts describing the platform's steady state.
MAX_EXCLUDED_FRACTION = 0.5


@dataclass(frozen=True)
class ReadToolOutcome:
    """Read-tool executions over a window, split by whose fault a failure is.

    `third_party_failures` are excluded from the rate per the documented
    gate: an upstream CRM being down is not a regression in this platform,
    and a gate that counted it would block every release during a vendor
    incident - which is exactly when shipping a fix matters most.

    The exclusion is not unbounded: `excluded_fraction` reports how much of
    the traffic was excused, and the gate refuses to pass if that is more
    than half, so the escape hatch cannot be used to launder a broken tool.
    """

    succeeded: int = 0
    failed: int = 0
    third_party_failures: int = 0

    @property
    def counted(self) -> int:
        return self.succeeded + self.failed

    @property
    def success_rate(self) -> float:
        return self.succeeded / self.counted if self.counted else 1.0

    @property
    def total_observed(self) -> int:
        return self.counted + self.third_party_failures

    @property
    def excluded_fraction(self) -> float:
        return self.third_party_failures / self.total_observed if self.total_observed else 0.0


@dataclass(frozen=True)
class GateResult:
    gate: str
    passed: bool
    observed: Any
    threshold: Any
    detail: str = ""


def evaluate_release_gates(
    report: EvalReport,
    *,
    security: dict[str, int] | None = None,
    read_tools: ReadToolOutcome | None = None,
    thresholds: GateThresholds = DEFAULT_THRESHOLDS,
) -> list[GateResult]:
    """security keys: cross_tenant_violations, unauthorized_writes,
    duplicate_replies (counts from the negative suites).

    `read_tools` is required in practice: omitting it fails the gate rather
    than silently passing it, because "no evidence" and "no failures" must
    not look the same in a release decision.
    """
    sec = security or {}
    gates: list[GateResult] = []

    for key, label in (
        ("cross_tenant_violations", "cross_tenant"),
        ("unauthorized_writes", "unauthorized_write"),
        ("duplicate_replies", "duplicate_reply"),
    ):
        observed = int(sec.get(key, 0))
        limit = getattr(thresholds, key)
        gates.append(
            GateResult(
                gate=f"zero_{label}",
                passed=observed <= limit,
                observed=observed,
                threshold=limit,
                detail="P0 safety regression" if observed > limit else "",
            )
        )

    measured = getattr(report, "recall_measured", 0)
    if measured:
        mean_recall = getattr(report, "retrieval_recall_mean", 1.0)
        gates.append(
            GateResult(
                gate="retrieval_recall_at_k",
                passed=mean_recall >= thresholds.min_retrieval_recall_at_k,
                observed=round(mean_recall, 4),
                threshold=thresholds.min_retrieval_recall_at_k,
                detail=""
                if mean_recall >= thresholds.min_retrieval_recall_at_k
                else "retrieval missed the expected corpus for answerable cases",
            )
        )

    total = max(report.total, 1)
    citation_rate = report.citation_violations / total
    gates.append(
        GateResult(
            gate="citation_coverage",
            passed=citation_rate <= thresholds.max_citation_violation_rate,
            observed=round(1 - citation_rate, 4),
            threshold=round(1 - thresholds.max_citation_violation_rate, 4),
        )
    )

    gates.append(
        GateResult(
            gate="abstention_correct_rate",
            passed=report.abstention_correct_rate >= thresholds.min_abstention_correct_rate,
            observed=round(report.abstention_correct_rate, 4),
            threshold=thresholds.min_abstention_correct_rate,
        )
    )

    # ADR 0009. The exemption above is only honest if the excluded cases are
    # still counted and still bounded. Two guards, both necessary:
    #
    # 1. Every declaration that COULD have produced an exclusion must have
    #    produced one. The comparison is against declarations on cases the
    #    exemption can actually apply to, which is why the runner reports
    #    `exemptible_cross_lingual` separately from `declared_cross_lingual`:
    #    a `must_abstain` cross-lingual case is counted correct already, so it
    #    is declared but never excluded, and comparing against all
    #    declarations would fail for a correct run. A case that *should* have
    #    been exempted and was not is the silent failure this catches - the
    #    declaration claims one thing and the run did another.
    # 2. The excluded set must stay a strict minority. Otherwise the exemption
    #    can be widened case by case until `abstention_correct_rate` is computed
    #    over nothing - and `abstention_correct_rate` returns 1.0 when its
    #    denominator is zero, so that would read as a perfect score.
    #
    # The `-1` sentinel means the artifact predates ADR 0009 and carries none of
    # these fields. Defaulting it to 0 would make `excluded == exemptible` a
    # `0 == 0` comparison that passes without measuring anything - verified: the
    # pre-ADR artifact on disk passed the match gate at `observed=0
    # threshold=0`. A report that cannot answer the question must not be read as
    # answering it, so both gates fail and name the remedy. They are appended
    # rather than returned early: every other gate in this function still has to
    # run, and bailing out here would silently drop the rest of the decision.
    exemptible = getattr(report, "exemptible_cross_lingual", -1)
    excluded = getattr(report, "cross_lingual_unreachable", -1)
    total_cases = max(report.total, 1)
    _not_reported = excluded < 0 or exemptible < 0
    gates.append(
        GateResult(
            gate="cross_lingual_exclusions_match",
            passed=False if _not_reported else excluded == exemptible,
            observed=excluded,
            threshold=exemptible,
            detail=(
                "artifact predates ADR 0009 and reports no cross-lingual "
                "counts; regenerate the eval report with "
                "`scripts/run_eval.py` before trusting this gate"
                if _not_reported
                else ""
                if excluded == exemptible
                else "a case declared cross_lingual on an answerable question did "
                "not abstain with NO_AUTHORIZED_EVIDENCE, so the declaration and "
                "the run disagree"
            ),
        )
    )
    gates.append(
        GateResult(
            gate="cross_lingual_exclusions_bounded",
            passed=False if _not_reported else excluded * 2 < total_cases,
            observed=round(excluded / total_cases, 4) if not _not_reported else float(excluded),
            threshold=0.5,
            detail=(
                "artifact predates ADR 0009; no exclusion count to bound"
                if _not_reported
                else ""
                if excluded * 2 < total_cases
                else "exclusions cover half the dataset or more; the abstention "
                "rate would be computed over a remainder that cannot fail"
            ),
        )
    )

    forbidden_rate = report.forbidden_claim_hits / total
    gates.append(
        GateResult(
            gate="forbidden_claims",
            passed=forbidden_rate <= thresholds.max_forbidden_claim_rate,
            observed=round(forbidden_rate, 4),
            threshold=thresholds.max_forbidden_claim_rate,
        )
    )

    gates.append(_read_tool_gate(read_tools, thresholds))

    return gates


def _read_tool_gate(read_tools: ReadToolOutcome | None, thresholds: GateThresholds) -> GateResult:
    if read_tools is None:
        return GateResult(
            gate="read_tool_success_rate",
            passed=False,
            observed=None,
            threshold=thresholds.min_read_tool_success_rate,
            detail="no read-tool telemetry supplied",
        )
    if read_tools.counted == 0:
        return GateResult(
            gate="read_tool_success_rate",
            passed=False,
            observed=None,
            threshold=thresholds.min_read_tool_success_rate,
            detail="no read-tool executions in the window",
        )
    if read_tools.excluded_fraction > MAX_EXCLUDED_FRACTION:
        return GateResult(
            gate="read_tool_success_rate",
            passed=False,
            observed=round(read_tools.success_rate, 4),
            threshold=thresholds.min_read_tool_success_rate,
            detail=(
                f"{round(read_tools.excluded_fraction, 4)} of read-tool traffic was "
                "excluded as third-party failure; the exclusion is not credible"
            ),
        )
    return GateResult(
        gate="read_tool_success_rate",
        passed=read_tools.success_rate >= thresholds.min_read_tool_success_rate,
        observed=round(read_tools.success_rate, 4),
        threshold=thresholds.min_read_tool_success_rate,
        detail=""
        if read_tools.success_rate >= thresholds.min_read_tool_success_rate
        else f"{read_tools.failed} of {read_tools.counted} read-tool calls failed",
    )


def release_allowed(gates: list[GateResult]) -> bool:
    return all(g.passed for g in gates)
