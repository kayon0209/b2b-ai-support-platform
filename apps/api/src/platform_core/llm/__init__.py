"""Model boundary package.

`provider` holds vendor-neutral Protocols and error types; `gitee_ai` is the
concrete adapter. Business modules import the Protocols only.
"""

from platform_core.llm.gitee_ai import GiteeAiClient
from platform_core.llm.provider import (
    ChatMessage,
    ChatProvider,
    ChatResult,
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
]
