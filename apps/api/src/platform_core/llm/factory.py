"""Provider construction (composition root for the model boundary).

Routers and workers ask this module for a model bundle rather than
constructing a vendor client themselves. That keeps three properties:

- one client per process, so the circuit breaker actually accumulates state
  across requests instead of restarting closed on every call;
- the vendor choice lives in configuration, not in call sites;
- tests can substitute a stub bundle without touching business code.

An unconfigured provider returns `None` rather than raising: callers decide
whether a missing model is fatal (answer generation) or degrading
(retrieval, which can fall back to lexical search).
"""

from enum import StrEnum
from functools import lru_cache

from platform_core.config import get_settings
from platform_core.llm.gitee_ai import GiteeAiClient
from platform_core.llm.provider import (
    ChatProvider,
    ConcreteModelBundle,
    EmbeddingProvider,
    ModelBundle,
    RerankProvider,
)


@lru_cache(maxsize=1)
def get_model_bundle() -> ModelBundle | None:
    """Build the configured model bundle, or None when unconfigured.

    Cached so every caller in the process shares one client and therefore
    one circuit breaker.
    """
    settings = get_settings()
    if settings.llm_api_key is None:
        # Fail closed: an unconfigured model boundary must not silently
        # produce answers.
        return None
    client = GiteeAiClient()
    return ConcreteModelBundle(chat=client, embedding=client, rerank=client, breaker=client.breaker)


def get_chat_provider() -> ChatProvider | None:
    bundle = get_model_bundle()
    return bundle.chat if bundle else None


def get_embedding_provider() -> EmbeddingProvider | None:
    bundle = get_model_bundle()
    return bundle.embedding if bundle else None


def get_rerank_provider() -> RerankProvider | None:
    bundle = get_model_bundle()
    return bundle.rerank if bundle else None


def reset_model_bundle() -> None:
    """Drop the cached bundle. Used by tests and on credential rotation."""
    get_model_bundle.cache_clear()


class ChatTask(StrEnum):
    """What the model call is for (feature list 11.2).

    Two tasks with genuinely different requirements, which is what makes
    routing worth having: classification runs on every message and only has to
    pick a label, so latency and cost dominate; answer generation runs once and
    has to reason over evidence, so capability dominates. Serving both with one
    model means either paying generation prices for classification or accepting
    classification-grade reasoning in customer-facing answers.
    """

    CLASSIFY = "classify"
    GENERATE = "generate"


def chat_model_for(task: ChatTask) -> str:
    """The model name to use for `task`.

    Falls back to `llm_model` when no per-task override is configured, so
    routing is opt-in per environment rather than something that must be
    configured before the platform works. Returning the configured default
    rather than raising keeps an unset override a no-op.
    """
    settings = get_settings()
    if task is ChatTask.CLASSIFY and settings.llm_model_classify:
        return settings.llm_model_classify
    return settings.llm_model
