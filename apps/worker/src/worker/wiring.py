"""Worker dependency composition (docs/deployment-and-operations.md).

`runner.main()` used to build `OrchestratorDeps(embedder=None,
generator=None, sender=None)` — four `None`s. The orchestrator treats a
missing sender as "un-sent, not failed" and a missing generator as
abstention, so the worker would claim real customer messages, mark the
inbox rows COMPLETED, and never send anything. Nothing raised. That is the
worst possible failure shape: a silent no-op that looks like success in
every log line and dashboard.

This module is the single place where the worker's real collaborators are
assembled, and it is deliberately explicit:

- **Fail closed on a missing LLM key.** The interactive worker refuses to
  start rather than running as a silent no-op. `--outbox-only` exists for
  the deployment that genuinely does not want the AI path.
- **One shared model bundle.** The chat, embedding and rerank providers
  share a circuit breaker, so a provider outage degrades one capability
  rather than three independent failure domains.
- **The sender and reader are the same Chatwoot client.** Both are
  idempotent-by-command-id and share a breaker; splitting them would give
  the outbound path two different notions of "Chatwoot is down".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import SecretStr

from platform_core.agent_runtime.orchestrator import OrchestratorDeps
from platform_core.config import get_settings
from platform_core.llm.factory import get_model_bundle
from platform_core.retrieval.hybrid import ProviderEmbedder


class WorkerConfigurationError(SystemExit):
    """Raised when the worker cannot be assembled into a working state.

    Subclasses SystemExit so `main()` exits non-zero with a readable
    message instead of a traceback, which is what an operator needs when a
    container fails to start.
    """


def _has_chatwoot_token(token: SecretStr | None) -> bool:
    """True only for a token that could actually authenticate.

    Compose injects `APP_CHATWOOT_API_TOKEN: ${APP_CHATWOOT_API_TOKEN:-}`,
    so the variable being *set* proves nothing — an unset host variable
    becomes an empty string, which is `not None` and would bind a client
    whose every request 401s. Treating blank as absent keeps the outbound
    path honest: a token that cannot authenticate is not a sender.
    """
    if token is None:
        return False
    return bool(token.get_secret_value().strip())


@dataclass(frozen=True)
class WorkerWiring:
    """What was actually wired, for startup logging and health reporting."""

    has_chat: bool
    has_embedding: bool
    has_rerank: bool
    has_sender: bool
    has_reader: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "has_chat": self.has_chat,
            "has_embedding": self.has_embedding,
            "has_rerank": self.has_rerank,
            "has_sender": self.has_sender,
            "has_reader": self.has_reader,
        }

    @property
    def can_generate(self) -> bool:
        return self.has_chat

    @property
    def can_send(self) -> bool:
        """A run that cannot send must not be reported as answered."""
        return self.has_sender


def build_interactive_deps(
    *,
    require_chat: bool = True,
    require_sender: bool = False,
) -> OrchestratorDeps:
    """Assemble the collaborators for the interactive (customer-facing) worker.

    `require_chat` defaults to True: without a chat provider every run
    abstains, so starting such a worker only produces misleading inbox
    rows. `require_sender` defaults to False because a deployment may
    legitimately want to generate drafts for human review before the
    outbound path is enabled — the orchestrator records the run but does
    not pretend to have replied, and `WiringAudit` below makes that visible.
    """
    settings = get_settings()
    bundle = get_model_bundle()

    if bundle is None:
        if require_chat:
            raise WorkerConfigurationError(
                "APP_LLM_API_KEY is required to run the interactive worker; "
                "use `python -m worker.runner --outbox-only` to run only the "
                "outbox relay without the AI path"
            )
        # Draft-only mode: generation is unavailable, which the orchestrator
        # reports as abstention rather than fabricating an answer.
        return OrchestratorDeps(extra={"draft_only": True})

    sender: Any = None
    reader: Any = None
    if _has_chatwoot_token(settings.chatwoot_api_token):
        from platform_core.support_bridge.chatwoot_client import ChatwootClient

        # One client, two roles: both share connection config and the
        # circuit breaker, so the inbound read and outbound send agree on
        # whether Chatwoot is reachable.
        client = ChatwootClient()
        sender = client
        reader = client
    elif require_sender:
        raise WorkerConfigurationError(
            "APP_CHATWOOT_API_TOKEN is required to send customer replies; "
            "unset it or run with require_sender=False for draft-only mode"
        )

    # Embeddings are optional: retrieval degrades to lexical-only, which is
    # still grounded evidence. Reranking is likewise optional.
    embedder = ProviderEmbedder(bundle.embedding) if bundle.embedding is not None else None

    # The orchestrator does not build a generator on its own; a run with no
    # generator abstains (orchestrator step: `if self._deps.generator is
    # None`). So the composition root must supply it, bound to the same
    # shared chat client.
    from platform_core.agent_runtime.generator import LlmAnswerGenerator

    generator = LlmAnswerGenerator(bundle.chat)

    return OrchestratorDeps(
        embedder=embedder,
        generator=generator,
        sender=sender,
        reader=reader,
        extra={"chat": bundle.chat, "bundle": bundle},
    )


def audit_wiring(deps: OrchestratorDeps) -> WorkerWiring:
    """Report what a deps object can actually do.

    Exists so a deployment can assert on capability instead of discovering
    a missing collaborator from a silently-empty customer conversation.
    """
    extra = deps.extra or {}
    bundle = extra.get("bundle")
    return WorkerWiring(
        # Generation capability follows the generator, not the bundle: the
        # orchestrator abstains without one, so that is the honest signal.
        has_chat=deps.generator is not None,
        has_embedding=deps.embedder is not None,
        has_rerank=bundle is not None and getattr(bundle, "rerank", None) is not None,
        has_sender=deps.sender is not None,
        has_reader=deps.reader is not None,
    )


__all__ = [
    "WorkerConfigurationError",
    "WorkerWiring",
    "audit_wiring",
    "build_interactive_deps",
]
