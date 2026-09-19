"""Evaluation dataset integrity and category reporting.

These are not "the evals pass" tests - the model that answers is pluggable,
so a passing run proves the harness works, not that a model is good. What
these tests *do* prove is that the dataset is complete and self-consistent,
and that the per-category reporter surfaces a regression instead of
averaging it away. Both are prerequisites for trusting a run.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable

import pytest

from platform_core.agent_runtime.qa_path import DraftAnswer
from platform_core.evaluation.runner import EvalReport, EvaluationRunner
from platform_core.retrieval.hybrid import PrincipalScope, RetrievedChunk

from .dataset import (
    ABSTENTION_REASONS,
    CATEGORY_CASES,
    CORPUS,
    EvalCategory,
    all_cases,
    case_ids,
    cases_for,
    corpus_by_key,
)
from .harness import chunk_id_for, corpus_key_of, make_retriever
from .report import (
    P0_REASON_CODES,
    build_category_report,
    failing_case_ids,
    metric_for,
    summarize,
)


def _run[T](awaitable: Awaitable[T]) -> T:
    """Run one coroutine to completion without requiring a real `Coroutine`.

    `make_retriever()` returns an `Awaitable`, not a `Coroutine`, and the
    runner methods are coroutines; accepting `Awaitable` covers both.
    """
    import asyncio

    async def _wrap() -> T:
        return await awaitable

    return asyncio.run(_wrap())


# --- Dataset integrity ---


def test_every_documented_category_has_at_least_one_case() -> None:
    """The doc lists twelve categories. A category with no cases silently
    reports as 100% healthy, which is the worst possible failure mode for a
    release gate."""
    for category in EvalCategory:
        assert cases_for(category), f"{category.value} has no cases"


def test_category_map_covers_every_enum_member() -> None:
    """Guards against adding a category to the enum and forgetting cases."""
    assert set(CATEGORY_CASES) == set(EvalCategory)


def test_case_ids_are_unique_and_stable() -> None:
    """A random or duplicated id makes two runs undiffable and a regression
    unnameable."""
    ids = case_ids()
    assert len(ids) == len(set(ids)), "duplicate case ids"
    assert all(cid and cid.strip() for cid in ids), "every case needs an id"
    assert all(cid == cid.strip() for cid in ids), "no padded ids"


def test_cases_without_an_id_are_rejected() -> None:
    """The dataset helper must not fall back to EvalCase's random id.

    `EvalCase` itself allows the default (the runner is generic), but a
    dataset case with a random id cannot be diffed across runs, so `_case`
    requires one explicitly.
    """
    from .dataset import _case

    with pytest.raises(ValueError, match="stable case_id"):
        _case("x", case_id="")

    # A missing id entirely is the same authoring error, reported clearly
    # rather than as a KeyError from inside the helper.
    with pytest.raises(ValueError, match="stable case_id"):
        _case("x")

    with pytest.raises(ValueError, match="stable case_id"):
        _case("x", case_id="   ")


def test_must_abstain_cases_never_require_claims() -> None:
    """A contradictory case is a dataset bug that would fail every model."""
    for case in all_cases():
        if case.must_abstain:
            assert not case.required_claims, f"{case.case_id} abstains but requires claims"


def test_injection_cases_forbid_more_than_they_require() -> None:
    """Prompt-injection cases are about what must NOT happen. A case with
    nothing forbidden cannot detect a successful injection."""
    for case in cases_for(EvalCategory.INDIRECT_INJECTION):
        assert case.forbidden_claims, f"{case.case_id} forbids nothing"
        assert not case.required_claims or case.forbidden_claims


def test_restricted_cases_are_all_marked_restricted() -> None:
    """Unauthorized content must go through the restricted path; otherwise
    it is testing retrieval quality, not authorization."""
    for category in (EvalCategory.UNAUTHORIZED,):
        for case in cases_for(category):
            assert case.restricted_query, f"{case.case_id} should be restricted"


def test_corpus_keys_are_unique() -> None:
    keys = [e.version_key for e in CORPUS]
    assert len(keys) == len(set(keys))


def test_corpus_is_non_trivial() -> None:
    """Cheap guard against a corpus being emptied during a refactor."""
    assert len(CORPUS) >= 8
    by_avail: dict[str, int] = {}
    for entry in CORPUS:
        by_avail[entry.availability] = by_avail.get(entry.availability, 0) + 1
    # The expired/unauthorized categories are only meaningful if such
    # passages actually exist in the corpus.
    assert by_avail.get("expired", 0) >= 2
    assert by_avail.get("unauthorized", 0) >= 1
    assert by_avail.get("active", 0) >= 5


# --- Harness: availability is enforced, not assumed ---


def test_retriever_withholds_expired_passages() -> None:
    """The 90-day policy must be invisible, so `expired-refund-90-day`
    cannot pass by luck."""
    scope = PrincipalScope(principal_types=("role",), principal_ids=("support_agent",))
    found = _run(make_retriever()("How long is the refund window on an annual plan?", scope))
    keys = {c.source_uri for c in found}
    assert "minio://eval/refund-policy-v3" in keys
    assert "minio://eval/refund-policy-v2" not in keys, "expired policy leaked into evidence"


def test_retriever_withholds_unauthorized_passages_from_normal_principals() -> None:
    scope = PrincipalScope(principal_types=("role",), principal_ids=("support_agent",))
    found = _run(make_retriever()("What is the negotiated annual price for Acme Corp?", scope))
    assert all("pricing-confidential" not in c.source_uri for c in found)


def test_retriever_returns_nothing_for_an_unrelated_question() -> None:
    """Drives abstention instead of feeding the model an irrelevant excerpt."""
    scope = PrincipalScope(principal_types=("role",), principal_ids=("support_agent",))
    assert _run(make_retriever()("What will your stock price be next quarter?", scope)) == []


def test_retriever_ranks_the_relevant_passage_first() -> None:
    """If ranking is inverted, answerable cases fail for the wrong reason."""
    scope = PrincipalScope(principal_types=("role",), principal_ids=("support_agent",))
    found = _run(make_retriever()("What uptime commitment do enterprise customers get?", scope))
    assert found, "expected at least one passage"
    assert found[0].source_uri == "minio://eval/sla-enterprise-v1"


def test_cited_chunk_ids_are_stable_across_runs() -> None:
    """Citation assertions must survive a re-run, so ids are derived, not random."""
    assert chunk_id_for("refund-policy-v3") == chunk_id_for("refund-policy-v3")
    scope = PrincipalScope(principal_types=("role",), principal_ids=("agent",))
    first = _run(make_retriever()("enterprise uptime commitment", scope))
    second = _run(make_retriever()("enterprise uptime commitment", scope))
    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]


# --- Reporting: regressions get an address ---


def _oracle_runner() -> EvaluationRunner:
    """An ideal answerer, so a run isolates the harness from model quality.

    Answers by citing the first retrieved passage and echoing its text,
    which satisfies required claims whose wording comes from the corpus.
    """

    async def answer(question: str, evidence: list[RetrievedChunk]) -> DraftAnswer:
        if not evidence:
            raise AssertionError("the oracle should never be asked to answer without evidence")
        top = evidence[0]
        claims: dict[int, list[uuid.UUID]] = {0: [top.chunk_id]}
        return DraftAnswer(text=top.excerpt, claims=claims, route="knowledge_qa")

    return EvaluationRunner(answer, make_retriever(), key_of=corpus_key_of)


def test_oracle_run_reports_every_category() -> None:
    report = _run(_oracle_runner().run(all_cases()))
    category_report = build_category_report(report)
    assert len(category_report.categories) == len(EvalCategory)
    assert sum(c.total for c in category_report.categories) == len(all_cases())


def test_report_refuses_a_partial_run() -> None:
    """Mis-attributing a failure to the wrong category is worse than refusing."""
    report = EvalReport(
        run_id="r",
        started_at=0,
        finished_at=1,
        total=1,
        passed=1,
        failed=0,
        abstention_correct=1,
        abstention_false=0,
        citation_violations=0,
        forbidden_claim_hits=0,
        results=[],
    )
    with pytest.raises(ValueError, match="cannot be reported by category"):
        build_category_report(report)


def test_a_category_collapse_is_visible_even_when_the_average_hides_it() -> None:
    """The doc's exact concern: one small category at zero, overall fine."""
    total = len(all_cases())
    answerable = len(cases_for(EvalCategory.ANSWERABLE))
    assert answerable < total, "precondition: one category is a minority of cases"

    # Every answerable case fails; nothing else does.
    healthy = _run(_oracle_runner().run(all_cases()))
    results = list(healthy.results)
    for index in range(answerable):
        results[index].passed = False
        results[index].citation_ok = False
        results[index].reason_codes = ["UNSUPPORTED_CLAIM"]

    degraded = EvalReport(
        run_id="r",
        started_at=0,
        finished_at=1,
        total=total,
        passed=total - answerable,
        failed=answerable,
        abstention_correct=healthy.abstention_correct,
        abstention_false=healthy.abstention_false,
        citation_violations=answerable,
        forbidden_claim_hits=0,
        results=results,
    )

    overall = degraded.passed / degraded.total
    assert overall > 0.8, "precondition: the overall average still looks healthy"

    category_report = build_category_report(degraded)
    answerable_result = next(
        c for c in category_report.categories if c.category is EvalCategory.ANSWERABLE
    )
    assert answerable_result.pass_rate == 0.0
    assert answerable_result.has_p0_failure, "citation failures are P0"
    assert category_report.p0_regressions, "the collapse must be reported as a P0 regression"
    worst = category_report.worst()
    assert worst is not None, "a report with a failing category must name a worst"
    assert worst.category is EvalCategory.ANSWERABLE


def test_p0_reasons_are_the_safety_critical_ones() -> None:
    assert "UNSUPPORTED_CLAIM" in P0_REASON_CODES
    assert "FORBIDDEN_CLAIM_PRESENT" in P0_REASON_CODES
    # A missing required claim is a quality miss, not a safety breach.
    assert "REQUIRED_CLAIM_MISSING" not in P0_REASON_CODES


def test_reasons_map_to_documented_metrics() -> None:
    assert metric_for("UNSUPPORTED_CLAIM") == "unsupported_claim_rate"
    assert metric_for("REQUIRED_CLAIM_MISSING") == "answer_relevance"
    assert metric_for("SOMETHING_NEW") == "unclassified", (
        "an unmapped reason must be visible, not silently dropped"
    )


def test_summarize_names_the_failing_cases() -> None:
    """A category line without case ids sends a reviewer back to the runner.

    The oracle run is not all-green - the dataset deliberately records
    known system gaps - so the extra failure is added on top of whatever
    the oracle already misses, and the expectation is derived from the
    report rather than assumed.
    """
    healthy = _run(_oracle_runner().run(all_cases()))
    results = list(healthy.results)
    target = case_ids()[0]
    results[0].passed = False
    results[0].reason_codes = ["FORBIDDEN_CLAIM_PRESENT"]

    degraded = EvalReport(
        run_id="r",
        started_at=0,
        finished_at=1,
        total=healthy.total,
        passed=healthy.passed - 1,
        failed=healthy.failed + 1,
        abstention_correct=healthy.abstention_correct,
        abstention_false=healthy.abstention_false,
        citation_violations=0,
        forbidden_claim_hits=1,
        results=results,
    )
    category_report = build_category_report(degraded)
    text = summarize(category_report)

    assert target in text
    assert "FAILED=" in text
    assert target in failing_case_ids(category_report)
    assert category_report.p0_regressions, "a forbidden-claim hit is a P0 regression"


def test_abstention_reasons_come_from_the_documented_vocabulary() -> None:
    assert "NO_AUTHORIZED_EVIDENCE" in ABSTENTION_REASONS
    assert "RESTRICTED_REQUEST" in ABSTENTION_REASONS
    assert "CONFLICTING_SOURCES" in ABSTENTION_REASONS
    assert corpus_by_key()["refund-policy-v3"].availability == "active"
