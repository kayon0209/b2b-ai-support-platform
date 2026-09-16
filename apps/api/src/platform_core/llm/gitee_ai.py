"""Gitee AI (模力方舟) provider adapter (tickets 17-18).

OpenAI-compatible surface at https://ai.gitee.com/v1. Three capabilities:
- chat completions  -> qwen3.8-flash (reasoning model; thinking channel is
  separated from the customer-visible answer)
- embeddings        -> Qwen3-Embedding-8B, requested at 1536 dimensions so
  the existing chunks.embedding vector(1536) column is reused unchanged
- rerank            -> bge-reranker-v2-m3 cross-encoder over fused candidates

Retry/breaker semantics mirror support_bridge/chatwoot_client.py so every
external boundary in the platform behaves identically.
"""

import asyncio
import time
from typing import Any

import httpx

from platform_core.config import get_settings
from platform_core.integrations.resilience import (
    CircuitBreaker,
    CircuitOpen,
    retry_delays,
)
from platform_core.llm.provider import (
    ChatMessage,
    ChatResult,
    EmbeddingResult,
    ModelError,
    ModelNotConfigured,
    ModelRejected,
    ModelUnavailable,
    RerankHit,
)

# Retryable HTTP statuses: transient provider/edge conditions only.
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class GiteeAiClient:
    """One client for the chat, embedding and rerank endpoints.

    A single CircuitBreaker guards all three: they share a provider, so a
    provider-wide outage should open every capability at once rather than
    letting each endpoint rediscover the failure independently.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        chat_model: str | None = None,
        embedding_model: str | None = None,
        rerank_model: str | None = None,
        embedding_dimensions: int | None = None,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        settings = get_settings()
        self._base_url = (base_url or settings.llm_base_url).rstrip("/")
        self._chat_model = chat_model or settings.llm_model
        self._embedding_model = embedding_model or settings.llm_embedding_model
        self._rerank_model = rerank_model or settings.llm_rerank_model
        self._dimensions = embedding_dimensions or settings.llm_embedding_dimensions
        self._timeout = timeout_seconds or settings.llm_timeout_seconds
        self._max_retries = max_retries if max_retries is not None else settings.llm_max_retries
        self._breaker = breaker or CircuitBreaker()

        key = api_key
        if key is None and settings.llm_api_key is not None:
            key = settings.llm_api_key.get_secret_value()
        # Held privately; never logged, never placed in a prompt.
        self._api_key = key or ""

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def _require_key(self) -> dict[str, str]:
        if not self._api_key:
            raise ModelNotConfigured()
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST with bounded retries, breaker accounting and error mapping.

        Every failure leaves the call site able to distinguish "retryable,
        no answer produced" from "rejected, fix the request" — the caller
        needs that to choose abstention or handoff correctly.
        """
        headers = self._require_key()
        url = f"{self._base_url}{path}"
        delays = retry_delays(self._max_retries)
        last_error: ModelError | None = None

        for attempt, delay in enumerate(delays):
            try:
                self._breaker.before_call()
            except CircuitOpen:
                raise ModelUnavailable("circuit breaker open") from None

            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(url, json=payload, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = ModelUnavailable(type(exc).__name__)
                self._breaker.on_failure()
            else:
                if resp.status_code < 300:
                    self._breaker.on_success()
                    body: dict[str, Any] = resp.json()
                    return body
                if resp.status_code in RETRYABLE_STATUS:
                    last_error = ModelUnavailable(f"status {resp.status_code}")
                    self._breaker.on_failure()
                else:
                    # 4xx: retrying cannot help; surface it immediately.
                    self._breaker.on_failure()
                    raise ModelRejected(resp.status_code, resp.text[:200])

            if attempt < len(delays) - 1:
                await asyncio.sleep(delay)

        assert last_error is not None
        raise last_error

    # --- Chat ---

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        model: str | None = None,
    ) -> ChatResult:
        """Chat completion. Defaults to temperature 0: the answer path wants
        reproducible, evidence-bound output, not creative variation."""
        started = time.monotonic()
        payload: dict[str, Any] = {
            "model": model or self._chat_model,
            # ProviderRole is a StrEnum, so `str()` normalises both members
            # and plain strings onto the wire without a .value lookup.
            "messages": [{"role": str(m.role), "content": m.content} for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        body = await self._post("/chat/completions", payload)
        choices = body.get("choices") or []
        if not choices:
            raise ModelUnavailable("provider returned no choices")
        message = choices[0].get("message") or {}
        usage = body.get("usage") or {}
        return ChatResult(
            text=str(message.get("content") or ""),
            model=str(body.get("model") or payload["model"]),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            reasoning=str(message.get("reasoning_content") or ""),
            latency_ms=int((time.monotonic() - started) * 1000),
            raw_usage=usage if isinstance(usage, dict) else {},
        )

    # --- Embeddings ---

    async def embed(self, texts: list[str], *, model: str | None = None) -> EmbeddingResult:
        """Dense embeddings. Empty input short-circuits to avoid a pointless
        provider round-trip and a malformed-request rejection."""
        if not texts:
            return EmbeddingResult(vectors=[], model=model or self._embedding_model)
        payload: dict[str, Any] = {
            "model": model or self._embedding_model,
            "input": texts,
            # Requested explicitly so the vector width matches the schema
            # rather than the model's native 1024.
            "dimensions": self._dimensions,
        }
        body = await self._post("/embeddings", payload)
        rows = sorted(body.get("data") or [], key=lambda row: row.get("index", 0))
        vectors = [list(row["embedding"]) for row in rows]
        if len(vectors) != len(texts):
            raise ModelUnavailable(f"expected {len(texts)} vectors, got {len(vectors)}")
        width = len(vectors[0]) if vectors else 0
        if width != self._dimensions:
            # A silent width mismatch would corrupt the vector column.
            raise ModelUnavailable(f"expected {self._dimensions} dims, got {width}")
        return EmbeddingResult(
            vectors=vectors,
            model=str(body.get("model") or payload["model"]),
            dimensions=width,
        )

    # --- Rerank ---

    async def rerank(
        self, query: str, documents: list[str], *, top_n: int | None = None
    ) -> list[RerankHit]:
        """Cross-encoder rerank of fused candidates, best first."""
        if not documents:
            return []
        payload: dict[str, Any] = {
            "model": self._rerank_model,
            "query": query,
            "documents": documents,
        }
        if top_n is not None:
            payload["top_n"] = top_n
        body = await self._post("/rerank", payload)
        hits = [
            RerankHit(
                index=int(row["index"]),
                relevance_score=float(row.get("relevance_score") or 0.0),
            )
            for row in (body.get("results") or [])
        ]
        return sorted(hits, key=lambda h: h.relevance_score, reverse=True)
