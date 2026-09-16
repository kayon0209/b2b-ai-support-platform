"""Provider-neutral model boundary (tickets 17-18, docs/agent.md).

Modules, not vendors. `agent_runtime` depends on these Protocols, never on
a concrete provider, so the answer path stays deterministic and testable
while the model stays swappable.

Rules enforced here (AGENTS.md / docs/architecture.md):
- External calls require timeouts, bounded retries, circuit breakers and
  structured error mapping.
- Credentials are resolved server-side and never logged or sent to the
  model (docs/security.md).
- Provider failure degrades explicitly: the caller must be able to choose
  abstain/handoff rather than fabricate an answer.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from platform_core.integrations.resilience import CircuitBreaker


class ModelError(Exception):
    """Base for mapped model-provider errors."""

    def __init__(self, code: str, retryable: bool, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.retryable = retryable


class ModelUnavailable(ModelError):
    """Transport-level or retryable provider failure (5xx, timeout, open circuit)."""

    def __init__(self, detail: str = "") -> None:
        super().__init__("MODEL_UNAVAILABLE", retryable=True, detail=detail)


class ModelRejected(ModelError):
    """Non-retryable provider rejection (4xx): bad request, auth, quota."""

    def __init__(self, status: int, detail: str = "") -> None:
        super().__init__(f"MODEL_REJECTED_{status}", retryable=False, detail=detail)


class ModelNotConfigured(ModelError):
    """No credential resolved. Fails closed instead of calling anonymously."""

    def __init__(self, detail: str = "llm_api_key is not configured") -> None:
        super().__init__("MODEL_NOT_CONFIGURED", retryable=False, detail=detail)


class ProviderRole(StrEnum):
    """Message roles on the OpenAI-compatible chat surface."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True)
class ChatMessage:
    role: ProviderRole
    content: str


@dataclass
class ChatResult:
    """Model output plus the usage facts every AgentRun must record."""

    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # Reasoning-capable models expose a separate thinking channel; kept out
    # of the customer-visible answer but retained for diagnostics.
    reasoning: str = ""
    latency_ms: int = 0
    raw_usage: dict[str, Any] = field(default_factory=dict)


@dataclass
class EmbeddingResult:
    vectors: list[list[float]]
    model: str
    dimensions: int = 0


@dataclass
class RerankHit:
    index: int
    relevance_score: float


class ChatProvider(Protocol):
    """Text generation boundary used by the answer path."""

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        model: str | None = None,
    ) -> ChatResult: ...


class EmbeddingProvider(Protocol):
    """Dense vector boundary used by retrieval."""

    async def embed(self, texts: list[str], *, model: str | None = None) -> EmbeddingResult: ...

    @property
    def dimensions(self) -> int: ...


class RerankProvider(Protocol):
    """Cross-encoder rerank boundary used after rank fusion."""

    async def rerank(
        self, query: str, documents: list[str], *, top_n: int | None = None
    ) -> list[RerankHit]: ...


class ModelBundle(Protocol):
    """Groups the three capabilities so callers inject one dependency."""

    chat: ChatProvider
    embedding: EmbeddingProvider
    rerank: RerankProvider
    breaker: CircuitBreaker


@dataclass
class ConcreteModelBundle:
    """Dataclass implementation of ModelBundle.

    A Protocol cannot be instantiated, so composition roots need a concrete
    carrier. `breaker` is exposed so health/metrics code can read circuit
    state without reaching into the client.
    """

    chat: ChatProvider
    embedding: EmbeddingProvider
    rerank: RerankProvider
    breaker: CircuitBreaker
