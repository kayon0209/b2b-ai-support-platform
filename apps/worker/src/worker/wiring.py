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

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.agent_runtime.orchestrator import OrchestratorDeps
from platform_core.config import get_settings
from platform_core.llm.factory import get_model_bundle
from platform_core.retrieval.hybrid import ProviderEmbedder
from platform_core.retrieval.reranker import Reranker


class WorkerConfigurationError(SystemExit):
    """Raised when the worker cannot be assembled into a working state.

    Subclasses SystemExit so `main()` exits non-zero with a readable
    message instead of a traceback, which is what an operator needs when a
    container fails to start.
    """


@dataclass(frozen=True)
class IngestionDeps:
    """Capabilities the ingestion pipeline needs. Deliberately narrow.

    A separate container from `OrchestratorDeps` so the ingestion worker
    cannot accidentally reach for a chat provider or a Chatwoot sender:
    parsing, chunking and embedding have no business sending anything.
    """

    embedder: Any

    @property
    def can_embed(self) -> bool:
        return self.embedder is not None


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
    # still grounded evidence.
    embedder = ProviderEmbedder(bundle.embedding) if bundle.embedding is not None else None

    # Reranking is built here but deliberately NOT switched on. The flag
    # `agent.rerank_enabled` decides per tenant, and an undefined flag means
    # the fused order is kept. Wiring it unconditionally would make a
    # quality-affecting change to every tenant's answers in one deploy, with
    # no way to observe it on one tenant first - which is what the flag exists
    # to prevent. Until this was added, `Reranker` was reachable only from the
    # retrieval diagnostics endpoint, so the production answer path never
    # reranked at all.
    reranker = Reranker(bundle.rerank, deadline_seconds=settings.rerank_timeout_seconds)

    # The orchestrator does not build a generator on its own; a run with no
    # generator abstains (orchestrator step: `if self._deps.generator is
    # None`). So the composition root must supply it, bound to the same
    # shared chat client.
    from platform_core.agent_runtime.generator import LlmAnswerGenerator

    settings = get_settings()
    generator = LlmAnswerGenerator(
        bundle.chat,
        fallback_model=settings.model_fallback_name if settings.model_fallback_enabled else None,
    )

    return OrchestratorDeps(
        embedder=embedder,
        generator=generator,
        reranker=reranker,
        sender=sender,
        reader=reader,
        extra={"chat": bundle.chat, "bundle": bundle},
    )


def app_role_url() -> str:
    """The non-bypass database URL.

    The bootstrap owner (`platform`) is a superuser, so a session opened with
    it bypasses RLS entirely - every tenant filter would be enforced by
    application code alone, and the documented third defence layer would be
    absent exactly where it matters most (a background job with no request
    context to derive a tenant from). `platform_app` has NOBYPASSRLS, which
    makes a missing `app.tenant_id` setting return zero rows instead of
    every tenant's rows.

    Delegates to `db.app_role_url`, the single statement of which role a
    session connects as. Re-derived here until now, which meant the API and the
    workers each owned a copy of an expression whose duplication is precisely
    how one of them ends up on the superuser.
    """
    from platform_core.db import app_role_url as _app_role_url

    return _app_role_url()


@asynccontextmanager
async def queue_bookkeeping_session() -> AsyncIterator[AsyncSession]:
    """The **owner** role, for claiming work off a queue. Nothing else.

    This is the one place in the worker that is allowed to connect as the
    bootstrap owner, and the reason is structural rather than convenient: a
    claim runs *before any tenant is known* - the worker discovers tenants from
    the rows it claims - and every queue table (`inbox_events`, `outbox_events`)
    is FORCE-RLS on `tenant_id = current_setting('app.tenant_id')`. Under the app
    role with no tenant bound, the claim SELECT returns zero rows, so the worker
    would sit idle forever with an empty-looking queue.

    What must NOT happen inside this scope is any read or write of tenant data.
    That includes reading a feature flag, loading conversation history, or
    writing a turn: on a bypassing connection the RLS binding is decoration, so
    those statements see every tenant's rows. Measured: a run on this role read
    **another tenant's** `agent.business_read_enabled` row, and every flag it
    evaluated resolved to a different tenant's answer.

    Tenant work belongs in `platform_core.identity.tenant_context.tenant_session`,
    which connects as `platform_app` and re-binds the tenant on every
    transaction (so a mid-scope COMMIT cannot silently unbind it).

    The alternative - a `SECURITY DEFINER` claim function, the way
    `claim_ingestion_versions` (migration 0018) solves the same problem - needs a
    migration, and the migration counter is currently coupled to an untracked
    revision in the shared worktree (`HANDOVER-2026-09-21-THREE-SURFACES.md` §5-E).
    Splitting the session achieves the same boundary with no schema change.
    """
    from platform_core.db import session_scope

    async with session_scope() as session:
        yield session


def build_ingestion_deps(*, require_embedding: bool = True) -> IngestionDeps:
    """Assemble the collaborators for the ingestion worker.

    The ingestion path needs exactly one model capability - embeddings - and
    must not be assembled from the interactive bundle, because that bundle
    requires a chat provider. Requiring an LLM key to index documents would
    mean a deployment that only wants retrieval-from-uploads cannot start,
    and a chat outage would stop ingestion.

    `require_embedding` defaults to True, and that is deliberate rather than
    convenient. Without an embedder the pipeline would still write chunks and
    mark the version READY, so `hybrid_search` would return lexical matches
    and the vector half of the fusion would silently contribute nothing -
    the exact "looks indexed, is half indexed" state the state machine exists
    to prevent. Failing at startup makes the misconfiguration visible.
    """
    bundle = get_model_bundle()
    if bundle is None or bundle.embedding is None:
        if require_embedding:
            raise WorkerConfigurationError(
                "APP_LLM_API_KEY is required to run the ingestion worker: "
                "documents must be embedded before retrieval can return them"
            )
        return IngestionDeps(embedder=None)
    return IngestionDeps(embedder=bundle.embedding)


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
    "IngestionDeps",
    "WorkerConfigurationError",
    "WorkerWiring",
    "app_role_url",
    "queue_bookkeeping_session",
    "audit_wiring",
    "build_ingestion_deps",
    "build_interactive_deps",
]
