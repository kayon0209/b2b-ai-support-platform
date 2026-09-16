"""Model boundary package.

`provider` holds vendor-neutral Protocols and error types; `gitee_ai` is the
concrete adapter; `factory` is the composition root that builds the shared
client. Business modules import the Protocols only.
"""

from platform_core.llm.factory import (
    get_chat_provider,
    get_embedding_provider,
    get_model_bundle,
    get_rerank_provider,
    reset_model_bundle,
)
from platform_core.llm.gitee_ai import GiteeAiClient
from platform_core.llm.provider import (
    ChatMessage,
    ChatProvider,
    ChatResult,
    ConcreteModelBundle,
    EmbeddingProvider,
    EmbeddingResult,
    ModelBundle,
    ModelError,
    ModelNotConfigured,
    ModelRejected,
    ModelUnavailable,
    ProviderRole,
    RerankHit,
    RerankProvider,
)

__all__ = [
    "ChatMessage",
    "ChatProvider",
    "ChatResult",
    "ConcreteModelBundle",
    "EmbeddingProvider",
    "EmbeddingResult",
    "GiteeAiClient",
    "ModelBundle",
    "ModelError",
    "ModelNotConfigured",
    "ModelRejected",
    "ModelUnavailable",
    "ProviderRole",
    "RerankHit",
    "RerankProvider",
    "get_chat_provider",
    "get_embedding_provider",
    "get_model_bundle",
    "get_rerank_provider",
    "reset_model_bundle",
]
