"""Unit tests: the flag-gated reranker on the answer path.

`Reranker` existed and was tested but was reachable from exactly one place -
the retrieval diagnostics endpoint - so the production answer path never
reranked. Wiring it matters because reranking changes which evidence a
customer-facing answer is built from, i.e. its blast radius is every answer;
gating it per tenant is how a rollout is observed on one tenant first.

No database here: the gate is a pure decision, and `retrieve_evidence` is
exercised against a stubbed `hybrid_search`.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from platform_core.agent_runtime import orchestrator as orch
from platform_core.agent_runtime.orchestrator import reranker_for_tenant, retrieve_evidence
from platform_core.retrieval.hybrid import PrincipalScope, RetrievedChunk


def _run(coro: Any) -> Any:
    # SelectorEventLoop to match the rest of the suite: psycopg async refuses
    # to run on Windows' default Proactor loop, and a test that runs on a
    # different loop from production is testing the wrong thing.
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def _chunk(name: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        document_version_id=uuid.uuid4(),
        title=f"doc-{name}",
        section_path=["Refund policy"],
        excerpt=f"excerpt {name}",
        source_uri=f"doc://{name}",
        score=0.5,
        ranking={"lexical": None, "vector": None},
    )


class _StubReranker:
    """Reverses the fused order, and can pretend to be degraded."""

    def __init__(self, *, degraded: bool = False, reason: str = "") -> None:
        self._degraded = degraded
        self._reason = reason
        self.calls = 0

    async def rerank(self, query: str, chunks: list[RetrievedChunk], *, top_k: int | None = None):
        self.calls += 1
        from platform_core.retrieval.reranker import RerankOutcome

        if self._degraded:
            return RerankOutcome(chunks=chunks, degraded=True, reason_code=self._reason)
        return RerankOutcome(chunks=list(reversed(chunks))[: (top_k or len(chunks))])


@pytest.fixture
def fused(monkeypatch: pytest.MonkeyPatch) -> list[RetrievedChunk]:
    """Make `hybrid_search` return a known fused order."""
    chunks = [_chunk("a"), _chunk("b"), _chunk("c")]

    async def _fake_hybrid_search(*args: Any, **kwargs: Any) -> list[RetrievedChunk]:
        return list(chunks)

    monkeypatch.setattr("platform_core.retrieval.hybrid.hybrid_search", _fake_hybrid_search)
    return chunks


def _principal() -> PrincipalScope:
    return PrincipalScope(principal_types=("role",), principal_ids=("ai_agent",))


# --- retrieve_evidence ----------------------------------------------------


def test_without_a_reranker_the_fused_order_is_kept(fused: list[RetrievedChunk]) -> None:
    result = _run(
        retrieve_evidence(
            None,  # type: ignore[arg-type] - the stub never touches the session
            tenant_id=uuid.uuid4(),
            query="refund window",
            principal=_principal(),
            embedder=None,
            reranker=None,
        )
    )

    assert [c.title for c in result] == [c.title for c in fused]


def test_a_reranker_reorders_the_fused_candidates(fused: list[RetrievedChunk]) -> None:
    reranker = _StubReranker()

    result = _run(
        retrieve_evidence(
            None,  # type: ignore[arg-type]
            tenant_id=uuid.uuid4(),
            query="refund window",
            principal=_principal(),
            embedder=None,
            reranker=reranker,  # type: ignore[arg-type]
        )
    )

    assert reranker.calls == 1
    assert [c.title for c in result] == list(reversed([c.title for c in fused]))


def test_a_degraded_rerank_keeps_the_fused_order(fused: list[RetrievedChunk]) -> None:
    """docs/architecture.md allows the fallback, but the caller has to be able
    to tell that the primary path did not run - otherwise a silent quality
    regression looks identical to a working reranker."""
    reranker = _StubReranker(degraded=True, reason="RERANK_TIMEOUT")

    result = _run(
        retrieve_evidence(
            None,  # type: ignore[arg-type]
            tenant_id=uuid.uuid4(),
            query="refund window",
            principal=_principal(),
            embedder=None,
            reranker=reranker,  # type: ignore[arg-type]
        )
    )

    assert [c.title for c in result] == [c.title for c in fused]


def test_a_single_candidate_is_not_sent_to_the_reranker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single candidate cannot be meaningfully reranked, so the provider
    call is pure latency."""

    async def _one(*args: Any, **kwargs: Any) -> list[RetrievedChunk]:
        return [_chunk("only")]

    monkeypatch.setattr("platform_core.retrieval.hybrid.hybrid_search", _one)
    reranker = _StubReranker()

    result = _run(
        retrieve_evidence(
            None,  # type: ignore[arg-type]
            tenant_id=uuid.uuid4(),
            query="q",
            principal=_principal(),
            embedder=None,
            reranker=reranker,  # type: ignore[arg-type]
        )
    )

    assert reranker.calls == 0
    assert [c.title for c in result] == ["doc-only"]


# --- the flag gate --------------------------------------------------------


class _Decision:
    def __init__(self, enabled: bool, reason: str) -> None:
        self.enabled = enabled
        self.reason = reason


@pytest.mark.parametrize(
    ("enabled", "reason", "expected"),
    [
        (True, "ROLLOUT", True),
        (True, "TENANT_TARGET", True),
        (False, "NOT_IN_ROLLOUT", False),
        (False, "DISABLED", False),
        (False, "UNKNOWN_FLAG", False),
    ],
)
def test_the_flag_decides(
    monkeypatch: pytest.MonkeyPatch, enabled: bool, reason: str, expected: bool
) -> None:
    async def _evaluate(session: Any, **kwargs: Any) -> _Decision:
        assert kwargs["flag_key"] == orch.RERANK_FLAG_KEY
        # Default False: an undefined flag means "keep the fused order", so a
        # missing flag is never a silent opt-in.
        assert kwargs["default"] is False
        return _Decision(enabled, reason)

    monkeypatch.setattr(orch.flag_service, "evaluate", _evaluate)
    reranker = _StubReranker()

    chosen, got_reason = _run(
        reranker_for_tenant(None, tenant_id=uuid.uuid4(), reranker=reranker)  # type: ignore[arg-type]
    )

    assert (chosen is not None) is expected
    assert got_reason == reason


def test_no_provider_does_not_consult_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no reranker configured the capability is absent, so calling the
    flag would report a rollout decision that was never made."""

    async def _evaluate(session: Any, **kwargs: Any) -> _Decision:  # pragma: no cover
        raise AssertionError("the flag must not be read with no reranker configured")

    monkeypatch.setattr(orch.flag_service, "evaluate", _evaluate)

    chosen, reason = _run(reranker_for_tenant(None, tenant_id=uuid.uuid4(), reranker=None))

    assert chosen is None
    assert reason == "NO_RERANKER"


def test_the_flag_key_is_namespaced() -> None:
    """A convention worth pinning: `agent.` scopes it to this surface, so an
    operator scanning the flag list can tell what it controls."""
    assert orch.RERANK_FLAG_KEY == "agent.rerank_enabled"
