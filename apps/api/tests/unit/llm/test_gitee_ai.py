"""Unit tests: Gitee AI provider adapter (tickets 17-19).

Covers the retry/breaker semantics shared with the Chatwoot client and the
provider-specific contracts the rest of the platform depends on:
- embeddings must come back at the configured width (a silent mismatch
  would corrupt the vector column),
- reasoning models must not leak their thinking channel into the answer,
- an unconfigured credential must fail closed rather than call anonymously.
"""

import httpx
import pytest

from platform_core.integrations.resilience import CircuitBreaker
from platform_core.llm.gitee_ai import GiteeAiClient
from platform_core.llm.provider import (
    ChatMessage,
    ModelNotConfigured,
    ModelRejected,
    ModelUnavailable,
    ProviderRole,
)


def _client(handler, monkeypatch, **kwargs) -> GiteeAiClient:
    """Build a client whose HTTP calls run through MockTransport.

    Patching is applied via monkeypatch so handlers never leak between
    tests (module-level patching caused cross-test pollution before).
    """
    original = httpx.AsyncClient

    def factory(*args, **inner_kwargs):
        inner_kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **inner_kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    kwargs.setdefault("api_key", "test-key")
    kwargs.setdefault("max_retries", 0)
    return GiteeAiClient(**kwargs)


# --- Chat ---


async def test_complete_returns_text_and_usage(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        return httpx.Response(
            200,
            json={
                "model": "qwen3.8-flash",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "OK",
                            "reasoning_content": "thinking...",
                        }
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 3},
            },
        )

    client = _client(handler, monkeypatch)
    result = await client.complete([ChatMessage(ProviderRole.USER, "hi")])
    assert result.text == "OK"
    assert result.model == "qwen3.8-flash"
    assert result.prompt_tokens == 11
    assert result.completion_tokens == 3
    # Reasoning stays separate from the customer-visible answer.
    assert result.reasoning == "thinking..."
    assert "thinking" not in result.text


async def test_complete_retries_then_succeeds(monkeypatch) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = _client(handler, monkeypatch, max_retries=2)
    result = await client.complete([ChatMessage(ProviderRole.USER, "hi")])
    assert result.text == "ok"
    assert calls["n"] == 2
    assert client.breaker.state.value == "closed"


async def test_complete_non_retryable_status_raises_rejected(monkeypatch) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"error": "unauthorized"})

    client = _client(handler, monkeypatch, max_retries=3)
    with pytest.raises(ModelRejected) as exc:
        await client.complete([ChatMessage(ProviderRole.USER, "hi")])
    assert exc.value.code == "MODEL_REJECTED_401"
    assert exc.value.retryable is False
    assert calls["n"] == 1  # 4xx must not be retried


async def test_complete_retryable_exhaustion_raises_unavailable(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = _client(handler, monkeypatch, max_retries=1)
    with pytest.raises(ModelUnavailable):
        await client.complete([ChatMessage(ProviderRole.USER, "hi")])


async def test_open_circuit_short_circuits_without_http(monkeypatch) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=60)
    breaker.on_failure()
    assert breaker.state.value == "open"
    client = _client(handler, monkeypatch, breaker=breaker)
    with pytest.raises(ModelUnavailable):
        await client.complete([ChatMessage(ProviderRole.USER, "hi")])
    assert calls["n"] == 0  # never reached the network


async def test_empty_choices_raises_unavailable(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    client = _client(handler, monkeypatch)
    with pytest.raises(ModelUnavailable):
        await client.complete([ChatMessage(ProviderRole.USER, "hi")])


# --- Missing credential ---


async def test_missing_api_key_fails_closed(monkeypatch) -> None:
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={})

    client = _client(handler, monkeypatch, api_key="")
    with pytest.raises(ModelNotConfigured):
        await client.complete([ChatMessage(ProviderRole.USER, "hi")])
    assert called["n"] == 0


# --- Embeddings ---


async def test_embed_requests_configured_dimensions(monkeypatch) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "Qwen3-Embedding-8B",
                "data": [
                    {"index": 0, "embedding": [0.1] * 1536},
                    {"index": 1, "embedding": [0.2] * 1536},
                ],
            },
        )

    client = _client(handler, monkeypatch, embedding_dimensions=1536)
    result = await client.embed(["a", "b"])
    # The width must be requested explicitly to match the vector column.
    assert seen["dimensions"] == 1536
    assert result.dimensions == 1536
    assert len(result.vectors) == 2
    assert client.dimensions == 1536


async def test_embed_rejects_width_mismatch(monkeypatch) -> None:
    """A silent mismatch would write vectors the column cannot hold."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"model": "m", "data": [{"index": 0, "embedding": [0.1] * 1024}]},
        )

    client = _client(handler, monkeypatch, embedding_dimensions=1536)
    with pytest.raises(ModelUnavailable) as exc:
        await client.embed(["a"])
    assert "1536" in str(exc.value)


async def test_embed_rejects_result_count_mismatch(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"model": "m", "data": [{"index": 0, "embedding": [0.1] * 1536}]},
        )

    client = _client(handler, monkeypatch, embedding_dimensions=1536)
    with pytest.raises(ModelUnavailable):
        await client.embed(["a", "b"])


async def test_embed_empty_input_skips_provider(monkeypatch) -> None:
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"data": []})

    client = _client(handler, monkeypatch)
    result = await client.embed([])
    assert result.vectors == []
    assert called["n"] == 0


# --- Rerank ---


async def test_rerank_sorts_best_first(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 1, "relevance_score": 0.02},
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 2, "relevance_score": 0.4},
                ]
            },
        )

    client = _client(handler, monkeypatch)
    hits = await client.rerank("q", ["a", "b", "c"])
    assert [h.index for h in hits] == [0, 2, 1]
    assert hits[0].relevance_score == 0.9


async def test_rerank_empty_documents_skips_provider(monkeypatch) -> None:
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"results": []})

    client = _client(handler, monkeypatch)
    assert await client.rerank("q", []) == []
    assert called["n"] == 0
