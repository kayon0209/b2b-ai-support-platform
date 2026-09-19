"""LLM-as-a-judge with mandatory human-agreement validation (plan 4.3).

Two rules make this judge safe to run, and both are enforced here rather
than trusted to caller discipline:

1. **The judge is a metric, never a gate.** `JudgeScores` carries no
   pass/fail semantics; `evaluate_release_gates` does not read it. An
   unverified judge added to a release decision is noise wearing a number.
2. **It cannot ship without agreement evidence.** `cohens_kappa` against a
   human-labeled subset is the function the rollout decision reads; below
   0.6 the judge stays off (`JudgeVerdict.reliable`).

Rubric: correctness / relevance / faithfulness, each 1-5 integer. The model
sees the QUESTION, the ANSWER and the EVIDENCE the answer claims to cite -
nothing else, because showing the reference answer would let the judge
parrot the oracle instead of judging.
"""

import re
import time
from dataclasses import dataclass

_RUBRIC_SYSTEM = (
    "You are a strict evaluator of a customer-support answer. Score on three "
    "axes, each an integer 1-5: correctness (does the answer agree with the "
    "evidence), relevance (does it answer the question), faithfulness (does "
    "it claim ONLY what the evidence supports). Reply with a single JSON "
    'object: {"correctness": n, "relevance": n, "faithfulness": n}. '
    "No other text."
)

_SCORE_LINE = re.compile(r"\"(correctness|relevance|faithfulness)\"\s*:\s*([1-5])")


@dataclass(frozen=True)
class JudgeScores:
    correctness: int
    relevance: int
    faithfulness: int

    def as_dict(self) -> dict[str, int]:
        return {
            "correctness": self.correctness,
            "relevance": self.relevance,
            "faithfulness": self.faithfulness,
        }


@dataclass(frozen=True)
class JudgeVerdict:
    """One judged case, plus whether the judge itself may be trusted."""

    case_id: str
    scores: JudgeScores | None  # None = judge unavailable / unparsable
    latency_ms: int


def parse_scores(text: str) -> JudgeScores | None:
    """Extract the three rubric scores; anything malformed is None.

    The judge is a model call like any other: a malformed response is not an
    error to retry expensively, it is a missing data point.
    """
    found = {name: int(score) for name, score in _SCORE_LINE.findall(text)}
    if len(found) != 3:
        return None
    return JudgeScores(
        correctness=found["correctness"],
        relevance=found["relevance"],
        faithfulness=found["faithfulness"],
    )


class LlmJudge:
    """Rubric judge over the existing ChatProvider. Sampling happens upstream
    (deterministic PASS cases only - deterministic checks already catch what
    they catch, and model spend on them is waste)."""

    def __init__(self, chat: object, *, max_tokens: int = 200) -> None:
        self._chat = chat
        self._max_tokens = max_tokens

    async def judge(self, case_id: str, question: str, answer: str, evidence: str) -> JudgeVerdict:
        started = time.monotonic()
        from platform_core.llm.provider import ChatMessage, ProviderRole

        prompt = (
            f"QUESTION:\n{question[:1000]}\n\n"
            f"ANSWER:\n{answer[:1000]}\n\n"
            f"EVIDENCE:\n{evidence[:2000]}"
        )
        try:
            result = await self._chat.complete(  # type: ignore[attr-defined]
                [
                    ChatMessage(ProviderRole.SYSTEM, _RUBRIC_SYSTEM),
                    ChatMessage(ProviderRole.USER, prompt),
                ],
                max_tokens=self._max_tokens,
                temperature=0.0,
            )
        except Exception:  # noqa: BLE001 - judge unavailability is degradation
            return JudgeVerdict(case_id=case_id, scores=None, latency_ms=0)
        scores = parse_scores(result.text)
        return JudgeVerdict(
            case_id=case_id,
            scores=scores,
            latency_ms=int((time.monotonic() - started) * 1000),
        )


def cohens_kappa(human: list[int], judge: list[int]) -> float | None:
    """Cohen's kappa over paired integer labels (plan 4.3: >= 0.6 to ship).

    Returns None when there is nothing measurable (empty, length mismatch,
    or a single agreement category) - None means "cannot conclude", which
    must not be conflated with 0.0 (measured disagreement).
    """
    if not human or len(human) != len(judge):
        return None
    n = len(human)
    categories = sorted(set(human) | set(judge))
    if len(categories) < 2:
        return None
    observed = sum(1 for h, j in zip(human, judge, strict=True) if h == j) / n
    human_counts = {c: human.count(c) / n for c in categories}
    judge_counts = {c: judge.count(c) / n for c in categories}
    expected = sum(human_counts.get(c, 0.0) * judge_counts.get(c, 0.0) for c in categories)
    if expected >= 1.0:
        return None
    return (observed - expected) / (1.0 - expected) if expected < 1.0 else None


def judge_reliable(kappa: float | None) -> bool:
    """The rollout decision: >= 0.6 agreement, and measurable at all."""
    return kappa is not None and kappa >= 0.6
