"""Unit tests: reranker degradation contract (ticket 14).

docs/architecture.md requires that a reranker failure fall back to fused
order rather than failing retrieval, and that the degradation is visible.
These tests pin both halves: the fallback works AND it is flagged.
"""

import asyncio
import uuid

from platform_core.integrations.resilience import CircuitBreaker
from platform_core.llm.provider import ModelUnavailable, RerankHit
from platform_core.retrieval.hybrid import RetrievedChunk
from platform_core.retrieval.reranker import Reranker


def _chunk(label: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_version_id=uuid.uuid4(),
        title="Doc",
        section_path=[],
        excerpt=label,
        source_uri="s3://k/doc.pdf",
        score=0.5,
    )


class _FakeRerank:
    def __init__(self, hits=None, error: Exception | None = None, delay: float = 0.0) -> None:
        self._hits = hits or []
        self._error = error
        self._delay = delay
        self.calls = 0

    async def rerank(self, query, documents, *, top_n=None):
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return self._hits


async def test_rerank_reorders_by_provider_score() -> None:
    chunks = [_chunk("a"), _chunk("b"), _chunk("c")]
    provider = _FakeRerank(
        hits=[
            RerankHit(index=2, relevance_score=0.9),
            RerankHit(index=0, relevance_score=0.4),
            RerankHit(index=1, relevance_score=0.1),
        ]
    )
    outcome = await Reranker(provider).rerank("q", chunks)

    assert outcome.degraded is False
    assert [c.excerpt for c in outcome.chunks] == ["c", "a", "b"]
    # Score provenance is retained for diagnostics.
    assert outcome.chunks[0].ranking["rerank_score"] == 0.9
    assert outcome.chunks[0].score == 0.9


async def test_rerank_timeout_degrades_to_fused_order() -> None:
    chunks = [_chunk("a"), _chunk("b")]
    provider = _FakeRerank(delay=0.5)
    outcome = await Reranker(provider, deadline_seconds=0.01).rerank("q", chunks)

    assert outcome.degraded is True
    assert outcome.reason_code == "RERANK_TIMEOUT"
    assert [c.excerpt for c in outcome.chunks] == ["a", "b"]


async def test_rerank_provider_error_degrades() -> None:
    chunks = [_chunk("a"), _chunk("b")]
    provider = _FakeRerank(error=ModelUnavailable("down"))
    outcome = await Reranker(provider).rerank("q", chunks)

    assert outcome.degraded is True
    assert outcome.reason_code == "MODEL_UNAVAILABLE"
    assert [c.excerpt for c in outcome.chunks] == ["a", "b"]


async def test_rerank_empty_response_degrades() -> None:
    chunks = [_chunk("a"), _chunk("b")]
    provider = _FakeRerank(hits=[])
    outcome = await Reranker(provider).rerank("q", chunks)

    assert outcome.degraded is True
    assert outcome.reason_code == "RERANK_EMPTY"


async def test_single_candidate_skips_provider() -> None:
    provider = _FakeRerank(hits=[RerankHit(index=0, relevance_score=0.9)])
    outcome = await Reranker(provider).rerank("q", [_chunk("only")])

    assert provider.calls == 0  # nothing to rerank
    assert outcome.degraded is False
    assert len(outcome.chunks) == 1


async def test_rerank_respects_top_k() -> None:
    chunks = [_chunk("a"), _chunk("b"), _chunk("c")]
    provider = _FakeRerank(
        hits=[RerankHit(index=i, relevance_score=1.0 - i / 10) for i in range(3)]
    )
    outcome = await Reranker(provider).rerank("q", chunks, top_k=2)
    assert len(outcome.chunks) == 2


async def test_out_of_range_index_is_ignored_safely() -> None:
    """A malformed provider response must not raise or mis-map chunks."""
    chunks = [_chunk("a"), _chunk("b")]
    provider = _FakeRerank(
        hits=[
            RerankHit(index=99, relevance_score=0.9),  # out of range
            RerankHit(index=1, relevance_score=0.5),
        ]
    )
    outcome = await Reranker(provider).rerank("q", chunks)

    assert outcome.degraded is False
    assert outcome.chunks[0].excerpt == "b"
    # The unranked candidate is retained after the ranked one.
    assert {c.excerpt for c in outcome.chunks} == {"a", "b"}


async def test_chunks_omitted_by_provider_are_retained() -> None:
    chunks = [_chunk("a"), _chunk("b"), _chunk("c")]
    provider = _FakeRerank(hits=[RerankHit(index=1, relevance_score=0.9)])
    outcome = await Reranker(provider).rerank("q", chunks)

    assert outcome.chunks[0].excerpt == "b"
    assert len(outcome.chunks) == 3


def test_breaker_open_is_importable_for_degradation_path() -> None:
    """The reranker catches CircuitOpen; keep the coupling explicit."""
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=60)
    breaker.on_failure()
    assert breaker.state.value == "open"
