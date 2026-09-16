"""Reranker stage with deadline and graceful degradation (ticket 14).

Pipeline position (docs/architecture.md search architecture):
  ... rank fusion -> reranker with deadline -> citation construction

Degradation contract: if the reranker fails or breaches its deadline we
return the fused order unchanged and flag `degraded=True`. Per
docs/architecture.md the fallback is only permitted when evaluation allows
it, so the flag is surfaced for the caller and for metrics rather than
being swallowed.
"""

import asyncio
import time
from dataclasses import dataclass

from platform_core.integrations.resilience import CircuitOpen
from platform_core.llm.provider import ModelError, RerankProvider
from platform_core.retrieval.hybrid import RetrievedChunk

DEFAULT_DEADLINE_SECONDS = 2.0


@dataclass
class RerankOutcome:
    """Reranked chunks plus whether the primary path actually ran."""

    chunks: list[RetrievedChunk]
    degraded: bool = False
    reason_code: str = ""
    latency_ms: int = 0


class Reranker:
    """Cross-encoder rerank over fused candidates, best first."""

    def __init__(
        self,
        provider: RerankProvider,
        *,
        deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    ) -> None:
        self._provider = provider
        self._deadline = deadline_seconds

    async def rerank(
        self,
        query: str,
        chunks: list[RetrievedChunk],
        *,
        top_k: int | None = None,
    ) -> RerankOutcome:
        """Rerank with a hard deadline; never raises to the caller.

        A single candidate cannot be meaningfully reranked, so we skip the
        provider call entirely and report the fused result as-is.
        """
        started = time.monotonic()
        limit = top_k or len(chunks)
        if len(chunks) <= 1:
            return RerankOutcome(chunks=chunks[:limit])

        documents = [c.excerpt for c in chunks]
        try:
            hits = await asyncio.wait_for(
                self._provider.rerank(query, documents, top_n=len(documents)),
                timeout=self._deadline,
            )
        except TimeoutError:
            return self._degraded(chunks, "RERANK_TIMEOUT", started)
        except CircuitOpen:
            return self._degraded(chunks, "RERANK_CIRCUIT_OPEN", started)
        except ModelError as exc:
            return self._degraded(chunks, exc.code, started)

        # Rebuild from provider ranking; ignore indices we cannot map so a
        # malformed response degrades to fusion order rather than crashing.
        ordered: list[RetrievedChunk] = []
        seen: set[int] = set()
        for hit in hits:
            if 0 <= hit.index < len(chunks) and hit.index not in seen:
                seen.add(hit.index)
                chunk = chunks[hit.index]
                chunk.ranking = {**chunk.ranking, "rerank_score": hit.relevance_score}
                chunk.score = hit.relevance_score
                ordered.append(chunk)
        if not ordered:
            return self._degraded(chunks, "RERANK_EMPTY", started)

        # Keep any candidate the provider omitted, after the ranked ones.
        for index, chunk in enumerate(chunks):
            if index not in seen:
                ordered.append(chunk)

        elapsed = int((time.monotonic() - started) * 1000)
        return RerankOutcome(chunks=ordered[:limit], latency_ms=elapsed)

    @staticmethod
    def _degraded(chunks: list[RetrievedChunk], reason_code: str, started: float) -> RerankOutcome:
        return RerankOutcome(
            chunks=chunks,
            degraded=True,
            reason_code=reason_code,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
