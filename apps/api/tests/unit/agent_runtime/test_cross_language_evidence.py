"""Feature list 3.7: cross-language evidence is not refused by a term count.

The single assertion this file exists for: an English question against a
Chinese passage shares *no term*, so the word-overlap floor scores it 0.0 and
refuses evidence that retrieval ranked first and rerank scored 0.509. Measured
on this stack, the same question in its own language scores 0.942 - so the
floor was silently making the platform Chinese-only.

Equally important, and asserted just as firmly: **the gate is not loosened**.
A rerank score is only present when the reranker ran, so an unreranked result
set (rerank off, degraded, or timed out) must behave exactly as it did before.
A change that made unreranked evidence pass would be trading a language bug for
a hallucination bug.
"""

from __future__ import annotations

import uuid

from platform_core.agent_runtime.qa_path import (
    ABSTAIN_LOW_RELEVANCE,
    MIN_EXCERPT_OVERLAP,
    RERANK_EVIDENCE_FLOOR,
    decide_abstention,
)
from platform_core.retrieval.hybrid import RetrievedChunk

CHINESE_PASSAGE = "加急打样需要额外费用，交期可缩短至 3 个工作日。"


def _chunk(*, excerpt: str, rerank: float | None = None) -> RetrievedChunk:
    ranking: dict[str, float] = {}
    if rerank is not None:
        ranking["rerank_score"] = rerank
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_version_id=uuid.uuid4(),
        title="加急与交期",
        section_path=["生产", "加急"],
        excerpt=excerpt,
        source_uri="kb://pricing/v3.2",
        score=rerank if rerank is not None else 0.5,
        ranking=ranking,
    )


def test_an_english_question_can_use_a_chinese_passage() -> None:
    """The bug: no shared terms, but rerank says the passage is on topic."""
    decision = decide_abstention(
        "Can you expedite the sample order?",
        [_chunk(excerpt=CHINESE_PASSAGE, rerank=0.509)],
    )
    assert decision.abstain is False


def test_the_same_passage_in_chinese_still_passes() -> None:
    """No regression on the language the platform already handled."""
    decision = decide_abstention(
        "能不能加急打样",
        [_chunk(excerpt=CHINESE_PASSAGE, rerank=0.942)],
    )
    assert decision.abstain is False


def test_unreranked_evidence_still_obeys_the_term_floor() -> None:
    """The guard: no rerank score means the old behaviour, unchanged."""
    decision = decide_abstention(
        "Can you expedite the sample order?",
        [_chunk(excerpt=CHINESE_PASSAGE, rerank=None)],
    )
    assert decision.abstain is True
    assert decision.reason_code == ABSTAIN_LOW_RELEVANCE


def test_a_low_rerank_score_does_not_open_the_gate() -> None:
    """An unrelated passage scores ~0.00003; it must stay refused."""
    decision = decide_abstention(
        "Can you expedite the sample order?",
        [_chunk(excerpt=CHINESE_PASSAGE, rerank=0.00003)],
    )
    assert decision.abstain is True
    assert decision.reason_code == ABSTAIN_LOW_RELEVANCE


def test_the_floor_sits_between_noise_and_a_cross_language_match() -> None:
    """The constant is a measurement, so pin what it was measured against."""
    assert 0.00003 < RERANK_EVIDENCE_FLOOR < 0.509


def test_one_confident_candidate_is_enough() -> None:
    """Retrieval returns a set; the best candidate is the one that answers."""
    weak = _chunk(excerpt=CHINESE_PASSAGE, rerank=0.01)
    strong = _chunk(excerpt=CHINESE_PASSAGE, rerank=0.44)
    decision = decide_abstention("Can you expedite the sample order?", [weak, strong])
    assert decision.abstain is False


def test_the_term_floor_is_unchanged_for_this_language() -> None:
    """MIN_EXCERPT_OVERLAP must not have been quietly lowered to fix 3.7."""
    assert MIN_EXCERPT_OVERLAP == 0.12
