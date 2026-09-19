"""Unit tests: judge rubric parsing, kappa, reliability rule (plan 4.3)."""

from __future__ import annotations

from platform_core.evaluation.judge import (
    LlmJudge,
    cohens_kappa,
    judge_reliable,
    parse_scores,
)


def test_parse_scores_extracts_the_three_axes() -> None:
    scores = parse_scores('{"correctness": 4, "relevance": 5, "faithfulness": 3}')
    assert scores is not None
    assert scores.as_dict() == {"correctness": 4, "relevance": 5, "faithfulness": 3}


def test_parse_scores_rejects_malformed_output() -> None:
    assert parse_scores("no json here") is None
    assert parse_scores('{"correctness": 9, "relevance": 5, "faithfulness": 3}') is None
    assert parse_scores('{"correctness": 4, "relevance": 5}') is None


def test_kappa_perfect_and_worst() -> None:
    assert cohens_kappa([1, 2, 3, 4], [1, 2, 3, 4]) == 1.0
    assert cohens_kappa([1, 1, 2, 2], [2, 2, 1, 1]) < 0.0


def test_kappa_unmeasurable_is_none_not_zero() -> None:
    assert cohens_kappa([], []) is None
    assert cohens_kappa([1, 2], [1]) is None
    assert cohens_kappa([1, 1], [1, 1]) is None  # no disagreement possible


def test_reliability_threshold_is_point_six() -> None:
    assert judge_reliable(0.7) is True
    assert judge_reliable(0.6) is True
    assert judge_reliable(0.59) is False
    assert judge_reliable(None) is False


def test_judge_degrades_when_provider_fails() -> None:
    import asyncio

    class _BrokenProvider:
        async def complete(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("provider down")

    judge = LlmJudge(_BrokenProvider())

    async def run() -> object:
        return await judge.judge("c1", "q", "a", "e")

    verdict = asyncio.run(run())
    assert verdict.scores is None
    assert verdict.case_id == "c1"


def test_judge_parses_a_well_formed_response() -> None:
    import asyncio

    class _FakeResult:
        text = '{"correctness": 5, "relevance": 4, "faithfulness": 5}'

    class _Provider:
        async def complete(self, *args: object, **kwargs: object) -> _FakeResult:
            return _FakeResult()

    judge = LlmJudge(_Provider())

    async def run() -> object:
        return await judge.judge("c1", "q", "a", "e")

    verdict = asyncio.run(run())
    assert verdict.scores is not None
    assert verdict.scores.faithfulness == 5
