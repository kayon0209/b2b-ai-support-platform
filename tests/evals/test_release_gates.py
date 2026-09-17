"""Release gates over the evaluation dataset (docs/testing-and-evaluation.md).

The doc requires that a prompt, model, retrieval or policy change ship only
if the gates pass, and adds the rule that matters most here:

    "failures are reviewed by category, not hidden in an average score"

Two things this file does that a plain "assert report.passed == total"
would not:

1. It runs the dataset through a **deterministic oracle** answerer. That
   isolates the pipeline (retrieval -> abstention -> citation validation)
   from model quality, so a red gate means the plumbing broke, not that a
   model had a bad day.
2. It records the two known gaps as an explicit, failing-visible baseline.
   If someone fixes them the test tells them to tighten the baseline; if a
   third appears, the test goes red. A baseline nobody can see is just a
   lie in a spreadsheet.
"""

from __future__ import annotations

from collections.abc import Awaitable

import pytest

from platform_core.agent_runtime.qa_path import DraftAnswer
from platform_core.evaluation.gates import evaluate_release_gates, release_allowed
from platform_core.evaluation.runner import EvalCase, EvalReport, EvaluationRunner
from platform_core.retrieval.hybrid import PrincipalScope, RetrievedChunk

from .dataset import EvalCategory, all_cases, cases_for
from .harness import make_retriever
from .report import build_category_report, summarize

pytestmark = pytest.mark.eval


def _run[T](awaitable: Awaitable[T]) -> T:
    """Run one coroutine to completion without requiring a real `Coroutine`.

    `make_retriever()` returns an `Awaitable`, not a `Coroutine`, and the
    runner methods are coroutines; accepting `Awaitable` covers both.
    """
    import asyncio

    async def _wrap() -> T:
        return await awaitable

    return asyncio.run(_wrap())


async def _oracle_answer(question: str, evidence: list[RetrievedChunk]) -> DraftAnswer:
    """Answer strictly from the top retrieved passage.

    Not a model: it echoes the passage and cites it, so citations always
    resolve. Any failure is therefore attributable to retrieval, the
    abstention gate, or the validator - never to generation.
    """
    if not evidence:
        raise AssertionError("the oracle must never be asked to answer without evidence")
    top = evidence[0]
    return DraftAnswer(text=top.excerpt, claims={0: [top.chunk_id]}, route="knowledge_qa")


def _oracle_runner() -> EvaluationRunner:
    return EvaluationRunner(_oracle_answer, make_retriever())


def _run_dataset() -> EvalReport:
    return _run(_oracle_runner().run(all_cases()))


# Known, tracked gaps in the current pipeline. Each is asserted to fail so
# that fixing one produces a visible prompt to update this set, and so that
# a new gap cannot silently join them.
KNOWN_GAPS: dict[str, str] = {
    "ambiguous-refund-eligibility": (
        "account identity is not resolved before retrieval, so a question "
        "whose answer depends on the caller's plan is answered from "
        "whichever policy ranks first instead of abstaining"
    ),
}


def test_oracle_run_produces_per_category_results() -> None:
    report = _run_dataset()
    category_report = build_category_report(report)
    assert len(category_report.categories) == len(EvalCategory)
    assert sum(c.total for c in category_report.categories) == len(all_cases())


def test_the_only_failures_are_the_tracked_gaps() -> None:
    """The regression detector. A new failing case id fails this test."""
    report = _run_dataset()
    category_report = build_category_report(report)

    failing: set[str] = set()
    for category in category_report.categories:
        failing.update(category.failed_case_ids)

    unexpected = failing - set(KNOWN_GAPS)
    assert not unexpected, (
        "these cases regressed and are not tracked as known gaps: "
        f"{sorted(unexpected)}\n\nfull report:\n{summarize(category_report)}"
    )


def test_tracked_gaps_are_still_failing() -> None:
    """If a gap is fixed, delete it from KNOWN_GAPS in the same change.

    Otherwise the baseline drifts into a list of things that used to be
    broken, and reviewers stop reading it.
    """
    report = _run_dataset()
    category_report = build_category_report(report)
    failing: set[str] = set()
    for category in category_report.categories:
        failing.update(category.failed_case_ids)

    fixed = set(KNOWN_GAPS) - failing
    assert not fixed, (
        f"these cases now pass but are still listed as known gaps: {sorted(fixed)}. "
        "Remove them from KNOWN_GAPS in this change."
    )


def test_no_safety_category_is_wholly_broken() -> None:
    """A category at zero is a P0 even if the headline rate looks fine."""
    report = _run_dataset()
    category_report = build_category_report(report)
    for category in category_report.categories:
        assert category.pass_rate > 0, (
            f"{category.category.value} passes nothing: {category.failed_case_ids}"
        )


def test_answerable_and_policy_categories_are_fully_green() -> None:
    """The baseline quality floor: the pipeline must answer what it can."""
    report = _run_dataset()
    category_report = build_category_report(report)
    for category in category_report.categories:
        if category.category in (EvalCategory.ANSWERABLE, EvalCategory.POLICY_CONTRACT):
            assert category.passed == category.total, (
                f"{category.category.value}: {category.failed_case_ids}"
            )


def test_expired_and_unauthorized_sources_never_reach_an_answer() -> None:
    """The two categories that exist purely to catch a leak."""
    report = _run_dataset()
    category_report = build_category_report(report)
    for category in category_report.categories:
        if category.category in (EvalCategory.EXPIRED, EvalCategory.UNAUTHORIZED):
            assert category.passed == category.total, (
                f"{category.category.value}: {category.failed_case_ids}"
            )


def test_release_gates_block_a_simulated_cross_tenant_violation() -> None:
    """Wires the dataset report into the real gate implementation.

    The doc's first gate is zero cross-tenant violations. The dataset
    cannot produce one (it never touches the database), so the count is
    injected from the negative suite - which is exactly how the gate is
    meant to be used in CI.
    """
    report = _run_dataset()
    clean = evaluate_release_gates(
        report,
        security={
            "cross_tenant_violations": 0,
            "unauthorized_writes": 0,
            "duplicate_replies": 0,
        },
    )
    assert release_allowed(clean), "\n".join(
        f"{g.gate}: {g.observed} vs {g.threshold}" for g in clean if not g.passed
    )

    dirty = evaluate_release_gates(
        report,
        security={
            "cross_tenant_violations": 1,
            "unauthorized_writes": 0,
            "duplicate_replies": 0,
        },
    )
    assert not release_allowed(dirty)
    assert any(not g.passed and g.gate == "zero_cross_tenant" for g in dirty)


def test_dataset_covers_each_documented_category_at_least_once() -> None:
    """A category with no cases is a gate that always passes."""
    for category in EvalCategory:
        assert cases_for(category), f"{category.value} has no cases"
    assert len(all_cases()) >= 20, "a release gate needs more than a handful of cases"


def test_oracle_is_deterministic_across_runs() -> None:
    """Two runs of the same dataset must agree, or the gate is noise."""
    first = build_category_report(_run_dataset())
    second = build_category_report(_run_dataset())
    assert summarize(first) == summarize(second)


def test_each_case_id_appears_exactly_once_in_the_report() -> None:
    """Guards against a category being registered twice, which would
    double-count its failures and skew the rate."""
    report = _run_dataset()
    ids = [r.case_id for r in report.results]
    assert len(ids) == len(set(ids))
    assert set(ids) == {c.case_id for c in all_cases()}


def test_a_case_with_no_evidence_never_reaches_the_oracle() -> None:
    """The abstention gate must run before generation, so whether a case
    abstains never depends on the answerer behaving itself.

    No evidence means handoff, so the case must declare `expected_handoff`:
    abstaining without handing off would leave the customer with silence.
    """
    called = {"n": 0}

    async def counting_answer(question: str, evidence: list[RetrievedChunk]) -> DraftAnswer:
        called["n"] += 1
        return await _oracle_answer(question, evidence)

    async def empty_retrieve(question: str, scope: PrincipalScope) -> list[RetrievedChunk]:
        return []

    runner = EvaluationRunner(counting_answer, empty_retrieve)
    report = _run(
        runner.run(
            [
                EvalCase(
                    question="anything",
                    must_abstain=True,
                    expected_handoff=True,
                    case_id="no-evidence",
                )
            ]
        )
    )
    assert report.passed == 1
    assert report.abstention_correct == 1
    assert called["n"] == 0, "generation must not run when there is no evidence"


def test_abstaining_without_handoff_is_not_a_pass() -> None:
    """The reason/handoff pair is part of the contract, not decoration."""

    async def answer(question: str, evidence: list[RetrievedChunk]) -> DraftAnswer:
        raise AssertionError("must not generate")

    async def empty_retrieve(question: str, scope: PrincipalScope) -> list[RetrievedChunk]:
        return []

    runner = EvaluationRunner(answer, empty_retrieve)
    report = _run(
        runner.run([EvalCase(question="q", must_abstain=True, expected_handoff=False, case_id="x")])
    )
    assert report.failed == 1, "abstention that withholds help must not pass as correct"
