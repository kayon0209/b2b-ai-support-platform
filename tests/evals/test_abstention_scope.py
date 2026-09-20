"""The abstention rate's scope, and the four ways it must not be widened.

`abstention_correct_rate` asks "did the pipeline abstain when it should have,
and answer when it should have". That question is only well-posed when the
corpus *could* have answered an answerable case. A Chinese question against an
English corpus breaks that premise: the content exists, the retriever cannot
reach it, and counting the abstention as a failure measures the language gap
with a metric about abstention judgement.

ADR 0009 carves that one case out. The tests here exist mainly to prove the
carve-out cannot grow: an exemption from a gate is far more dangerous than a
missing one, because it fails silently and in the direction of looking better.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable

import pytest

from platform_core.agent_runtime.qa_path import DraftAnswer
from platform_core.evaluation.gates import (
    ReadToolOutcome,
    evaluate_release_gates,
)
from platform_core.evaluation.runner import EvalCase, EvalReport, EvaluationRunner
from platform_core.retrieval.hybrid import PrincipalScope, RetrievedChunk

pytestmark = pytest.mark.eval


def _run[T](awaitable: Awaitable[T]) -> T:
    import asyncio

    async def _wrap() -> T:
        return await awaitable

    return asyncio.run(_wrap())


async def _empty_retriever(question: str, scope: PrincipalScope) -> list[RetrievedChunk]:
    """No evidence for anything - the English-corpus/Chinese-question shape."""
    return []


async def _oracle_answer(question: str, evidence: list[RetrievedChunk]) -> DraftAnswer:
    return DraftAnswer(text=" ".join(c.excerpt for c in evidence), claim_texts={})


def _chunk(text: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid5(uuid.NAMESPACE_URL, "scope:c1"),
        document_version_id=uuid.uuid5(uuid.NAMESPACE_URL, "scope:dv1"),
        title="policy",
        section_path=[],
        excerpt=text,
        source_uri="minio://eval/scope",
        score=1.0,
    )


async def _refund_retriever(question: str, scope: PrincipalScope) -> list[RetrievedChunk]:
    return [_chunk("The refund window is fourteen days from delivery.")]


def _runner(retrieve_fn) -> EvaluationRunner:  # type: ignore[no-untyped-def]
    return EvaluationRunner(
        answer_fn=_oracle_answer,
        retrieve_fn=retrieve_fn,
        key_of=lambda chunk: chunk.title,
    )


def _case(question: str = "把订单退掉", **kwargs) -> EvalCase:  # type: ignore[no-untyped-def]
    """A Chinese question against the English corpus: answerable in principle,
    unreachable in practice."""
    return EvalCase(question=question, role="support_agent", **kwargs)


# --- The exemption does what it claims ---


def test_a_declared_cross_lingual_abstention_leaves_the_rate_alone() -> None:
    """The case that motivated ADR 0009, asserted end to end.

    Without the declaration this abstention is a false abstention and drags
    the rate to 0.0. With it, the rate is computed over the cases the rate
    actually applies to, and the exclusion is counted instead.
    """
    report = _run(_runner(_empty_retriever).run([_case(cross_lingual=True)]))
    assert report.abstention_false == 0
    assert report.abstention_correct == 0
    assert report.cross_lingual_unreachable == 1
    assert report.exemptible_cross_lingual == 1
    assert report.abstention_correct_rate == 1.0


def test_the_same_case_without_the_declaration_is_still_a_false_abstention() -> None:
    """The control. Same utterance, same empty retriever, no declaration.

    This is the test that would catch the declaration being ignored, and it
    is deliberately the *same* case object plus one flag, so the only
    difference between the two tests is the thing under test.
    """
    report = _run(_runner(_empty_retriever).run([_case()]))
    assert report.abstention_false == 1
    assert report.cross_lingual_unreachable == 0
    assert report.abstention_correct_rate == 0.0


def test_a_cross_lingual_case_that_abstains_for_another_reason_is_not_exempt() -> None:
    """Retrieved-but-irrelevant is a retriever problem, not a language gap.

    A case whose evidence came back and scored too low is a case the
    retriever could reach, so exempting it would hide the more actionable of
    the two failures.
    """

    async def _irrelevant(question: str, scope: PrincipalScope) -> list[RetrievedChunk]:
        return [_chunk("Completely unrelated text about bicycles.")]

    report = _run(_runner(_irrelevant).run([_case(cross_lingual=True)]))
    assert report.cross_lingual_unreachable == 0
    assert report.abstention_false == 1


# --- The exemption must not cover a real failure ---


def test_a_restricted_request_is_never_exempt_even_when_declared_cross_lingual() -> None:
    """`restricted_query` abstains for a security reason, and that is a
    different reason code. Cross-lingual must not launder it."""
    case = _case(cross_lingual=True, restricted_query=True)
    report = _run(_runner(_empty_retriever).run([case]))
    assert report.cross_lingual_unreachable == 0
    assert report.abstention_false == 1


def test_a_must_abstain_case_is_never_exempt() -> None:
    """A case that SHOULD abstain and abstains is already counted correct;
    exempting it would remove a passing case from the denominator."""
    case = _case(cross_lingual=True, must_abstain=True, expected_handoff=True)
    report = _run(_runner(_empty_retriever).run([case]))
    assert report.cross_lingual_unreachable == 0
    assert report.abstention_correct == 1


def test_an_answered_case_is_never_exempt() -> None:
    """The flag only ever suppresses a false abstention. A case that answered
    normally must still be counted, or `cross_lingual` would become a way to
    stop a failing answer from being measured at all.

    Asked in English so it clears the overlap floor and genuinely answers -
    the point is that retrieval *succeeded*, which a Chinese question could
    not demonstrate against this corpus.
    """
    case = _case(question="What is the refund window?", cross_lingual=True)
    report = _run(_runner(_refund_retriever).run([case]))
    assert report.cross_lingual_unreachable == 0
    assert report.abstention_correct == 1


def test_the_declaration_does_not_excuse_the_failure_itself() -> None:
    """The most important one. The exemption is about the *rate*, not the
    verdict: a declared cross-lingual case that abstains is still a failing
    case, and still appears in `failed`."""
    report = _run(_runner(_empty_retriever).run([_case(cross_lingual=True)]))
    assert report.failed == 1
    assert report.passed == 0
    assert [r.case_id for r in report.results if not r.passed] == [
        r.case_id for r in report.results
    ]


# --- The gates that keep the exemption honest ---


def _report_with_exclusions(*, excluded: int, exemptible: int, total: int) -> EvalReport:
    return EvalReport(
        run_id="r",
        started_at=0,
        finished_at=1,
        total=total,
        passed=total,
        failed=0,
        abstention_correct=total - excluded,
        abstention_false=0,
        citation_violations=0,
        forbidden_claim_hits=0,
        cross_lingual_unreachable=excluded,
        declared_cross_lingual=exemptible,
        exemptible_cross_lingual=exemptible,
        results=[],
    )


def _read_tools() -> ReadToolOutcome:
    return ReadToolOutcome(succeeded=100, failed=0, third_party_failures=0)


def _gate(report: EvalReport, name: str):  # type: ignore[no-untyped-def]
    gates = evaluate_release_gates(report, security={}, read_tools=_read_tools())
    return next(g for g in gates if g.gate == name)


def _load_report(payload: dict[str, object]) -> EvalReport:
    """Deserialize a report the way `release_check` does, from a real file.

    Going through the file rather than the constructor is the point: the
    "field not reported" sentinel lives in the loader, so a hand-built
    `EvalReport` would silently get the dataclass defaults and hide the bug
    these tests exist to pin.
    """
    import json
    import tempfile
    from pathlib import Path

    from platform_core.evaluation.release_check import _report_from_artifact

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "eval_report.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        report = _report_from_artifact(str(path))
    assert report is not None
    return report


def test_the_gate_passes_when_declared_and_observed_agree() -> None:
    report = _report_with_exclusions(excluded=3, exemptible=3, total=40)
    assert _gate(report, "cross_lingual_exclusions_match").passed
    assert _gate(report, "cross_lingual_exclusions_bounded").passed


def test_the_gate_fails_when_a_declared_case_did_not_abstain_that_way() -> None:
    """A case that declares itself cross-lingual but abstained for another
    reason (or answered) is a silent disagreement between intent and outcome,
    and nobody would see it without this gate."""
    report = _report_with_exclusions(excluded=2, exemptible=3, total=40)
    gate = _gate(report, "cross_lingual_exclusions_match")
    assert not gate.passed
    assert gate.observed == 2
    assert gate.threshold == 3


def test_the_gate_fails_when_exclusions_swallow_half_the_dataset() -> None:
    """Because `abstention_correct_rate` returns 1.0 on an empty denominator,
    a wide enough exemption reads as a perfect score. This is the guard that
    makes that impossible."""
    report = _report_with_exclusions(excluded=20, exemptible=20, total=40)
    gate = _gate(report, "cross_lingual_exclusions_bounded")
    assert not gate.passed
    assert gate.threshold == 0.5


def test_a_single_exclusion_in_a_small_dataset_is_still_allowed() -> None:
    """The bound must not be so tight that the first honest exclusion trips
    it - otherwise the guard just becomes a ban and the original bug returns."""
    report = _report_with_exclusions(excluded=1, exemptible=1, total=3)
    assert _gate(report, "cross_lingual_exclusions_bounded").passed


def test_an_artifact_without_the_new_fields_fails_the_match_gate() -> None:
    """An old `eval_report.json` carries neither field, so there is nothing to
    compare.

    The first version of this change defaulted both to 0, which made the gate a
    `0 == 0` comparison that reported green while having measured nothing -
    reproduced against the real pre-ADR artifact on disk (`observed=0
    threshold=0`). A report that cannot answer the question must not be read as
    answering it, so the gate now fails.

    The sentinel is applied where the artifact is *deserialized*, not on the
    dataclass, so this goes through `release_check`'s loader rather than
    constructing an `EvalReport` by hand - the hand-built form has the field
    defaults and would not reproduce the bug.
    """
    loaded = _load_report(
        {  # no cross-lingual keys at all
            "run_id": "r",
            "started_at": 0,
            "finished_at": 1,
            "total": 40,
            "passed": 40,
            "failed": 0,
            "abstention_correct": 40,
            "abstention_false": 0,
            "citation_violations": 0,
            "forbidden_claim_hits": 0,
        }
    )
    assert loaded.cross_lingual_unreachable < 0, "not reported, not zero"
    assert loaded.exemptible_cross_lingual < 0, "not reported, not zero"
    assert not _gate(loaded, "cross_lingual_exclusions_match").passed
    assert not _gate(loaded, "cross_lingual_exclusions_bounded").passed


def test_a_must_abstain_cross_lingual_case_is_declared_but_not_exemptible() -> None:
    """The distinction the first version of this change got wrong.

    A `must_abstain` cross-lingual case abstains and is therefore already
    counted *correct*, so no exemption applies to it. Counting it as
    "exemptible" makes the match gate unsatisfiable on a correct run - which
    is exactly what happened, and what the gate reported as `0 vs 10`.
    """
    report = _run(
        _runner(_empty_retriever).run(
            [_case(cross_lingual=True, must_abstain=True, expected_handoff=True)]
        )
    )
    assert report.cross_lingual_unreachable == 0
    assert report.declared_cross_lingual == 1, "it IS declared"
    assert report.exemptible_cross_lingual == 0, "but there is nothing to exempt"
    assert report.abstention_correct == 1


def test_the_gate_stays_green_when_only_must_abstain_cases_are_declared() -> None:
    """The end-to-end form of the case above: a real run over a declared
    cross-lingual must_abstain case must not fail the match gate."""
    report = _run(
        _runner(_empty_retriever).run(
            [_case(cross_lingual=True, must_abstain=True, expected_handoff=True)]
        )
    )
    assert _gate(report, "cross_lingual_exclusions_match").passed


def test_no_case_can_vanish_from_the_abstention_accounting() -> None:
    """The identity that makes the exemption auditable.

    Every case must be either counted (correct or false) or explicitly
    excluded. An exclusion that removed a case from the denominator without
    appearing in `cross_lingual_unreachable` would be invisible - the rate
    would improve and no number would say why. This is the assertion that
    turns "we excluded some cases" into "we excluded exactly these, and here
    they are".
    """
    cases = [
        _case(cross_lingual=True),  # excluded
        _case(),  # counted, false abstention
        _case(question="What is the refund window?", cross_lingual=True),  # counted, correct
    ]

    async def _english_only(question: str, scope: PrincipalScope) -> list[RetrievedChunk]:
        """Evidence only for English questions - the corpus is English, so a
        Chinese question retrieves nothing."""
        if any("\u4e00" <= ch <= "\u9fff" for ch in question):
            return []
        return [_chunk("The refund window is fourteen days from delivery.")]

    report = _run(_runner(_english_only).run(cases))
    counted = report.abstention_correct + report.abstention_false
    assert counted + report.cross_lingual_unreachable == report.total
    assert report.total == 3
    assert report.cross_lingual_unreachable == 1
    assert report.abstention_false == 1
    assert report.abstention_correct == 1
