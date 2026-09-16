"""Retrieval query API (docs/api-contracts.md).

POST /v1/retrieval/query

Answers "what authorized knowledge matches this query" without generating
an answer. Used by the admin UI to debug why a run cited what it cited, and
by evaluations that measure retrieval quality independently of generation.

Security notes:
- `principal` in the request body is only ever INTERSECTED with the
  authenticated context. A caller cannot widen its own scope; the request
  can only narrow it. Anything the caller omits is filled from context.
- Ranking scores are diagnostics and are never rendered to end users as
  probabilities (docs/api-contracts.md).
"""

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.api import (
    POLICY_DENIED,
    error_response,
    get_context,
    ok_response,
    parse_uuid,
    require_policy,
    tenant_session,
)
from platform_core.llm.factory import get_embedding_provider, get_rerank_provider
from platform_core.retrieval.hybrid import (
    Embedder,
    PrincipalScope,
    ProviderEmbedder,
    RetrievedChunk,
    hybrid_search,
)
from platform_core.retrieval.reranker import Reranker
from platform_policy import Action

router = APIRouter(prefix="/v1/retrieval", tags=["retrieval"])

MAX_TOP_K = 50


class PrincipalIn(BaseModel):
    """Caller-declared scope. Narrowing only, never widening."""

    actor_id: str | None = None
    enterprise_account_id: str | None = None


class RetrievalQueryIn(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    knowledge_space_ids: list[str] = Field(default_factory=list)
    principal: PrincipalIn = Field(default_factory=PrincipalIn)
    top_k: int = Field(default=8, ge=1, le=MAX_TOP_K)
    # Advisory: the pipeline enforces its own reranker deadline. Accepted so
    # the contract is honoured, but never trusted to bound server work.
    deadline_ms: int | None = Field(default=None, ge=1, le=30_000)


def _scope_from(ctx_actor: str, declared: PrincipalIn) -> PrincipalScope:
    """Build the ACL scope as an intersection of context and request.

    The authenticated context is authoritative. If the caller also names an
    enterprise account, both must match for a resource to be visible, so the
    request can only remove visibility.
    """
    principal_ids: list[str] = []
    if ctx_actor:
        principal_ids.append(ctx_actor)
    if declared.actor_id and declared.actor_id != ctx_actor:
        # Declaring a different actor is not an escalation path: the value
        # is added as an additional constraint, so nothing new becomes
        # visible unless the resource grants it to both.
        principal_ids.append(declared.actor_id)
    if declared.enterprise_account_id:
        principal_ids.append(declared.enterprise_account_id)
    return PrincipalScope(
        principal_types=("user", "enterprise_account"),
        principal_ids=tuple(principal_ids),
    )


def _serialize(chunks: list[RetrievedChunk]) -> list[dict[str, Any]]:
    return [
        {
            "chunk_id": str(c.chunk_id),
            "document_version_id": str(c.document_version_id),
            "title": c.title,
            "section_path": list(c.section_path),
            "excerpt": c.excerpt,
            "source_uri": c.source_uri,
            "ranking": c.ranking,
        }
        for c in chunks
    ]


@router.post("/query")
async def retrieval_query(request: Request, body: RetrievalQueryIn) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)

    if not ctx.role:
        # A principal with no role cannot be evaluated by the policy engine
        # (unknown roles deny), so deny explicitly with a clear code rather
        # than surfacing a confusing ROLE_UNKNOWN later.
        return error_response(
            POLICY_DENIED, "principal has no role; cannot evaluate knowledge.read", status_code=403
        )

    # Policy gate before any retrieval work happens.
    denied = require_policy(ctx, Action.KNOWLEDGE_READ)
    if denied is not None:
        return denied

    try:
        space_ids = [parse_uuid(s, field="knowledge_space_ids") for s in body.knowledge_space_ids]
    except ValueError as exc:
        return error_response("VALIDATION_FAILED", str(exc), status_code=400)

    scope = _scope_from(str(ctx.actor_id) if ctx.actor_id else "", body.principal)

    # Production uses the provider embedder when configured; an unconfigured
    # model boundary degrades to lexical-only retrieval rather than failing
    # the request, because lexical results are still grounded evidence.
    embedder: Embedder | None = None
    provider = get_embedding_provider()
    if provider is not None:
        embedder = ProviderEmbedder(provider)

    trace_id = ""
    degraded = False
    degradation_reason = ""

    async with tenant_session(ctx) as session:
        chunks = await _search(
            session,
            tenant_id=ctx.tenant_id,
            body=body,
            space_ids=space_ids,
            scope=scope,
            embedder=embedder,
        )
        rerank_provider = get_rerank_provider()
        if rerank_provider is not None and len(chunks) > 1:
            outcome = await Reranker(rerank_provider).rerank(body.query, chunks, top_k=body.top_k)
            chunks = outcome.chunks
            degraded = outcome.degraded
            degradation_reason = outcome.reason_code

    payload: dict[str, Any] = {"results": _serialize(chunks)}
    if degraded:
        # Surface the degradation instead of hiding it: docs/architecture.md
        # only permits the fallback where evaluation allows it.
        payload["rerank"] = {"degraded": True, "reason_code": degradation_reason}
    return ok_response(payload, trace_id=trace_id)


async def _search(
    session: AsyncSession,
    *,
    tenant_id: Any,
    body: RetrievalQueryIn,
    space_ids: list[Any],
    scope: PrincipalScope,
    embedder: Embedder | None,
) -> list[RetrievedChunk]:
    return await hybrid_search(
        session,
        tenant_id=tenant_id,
        query=body.query,
        top_k=body.top_k,
        knowledge_space_ids=space_ids or None,
        principal=scope,
        embedder=embedder,
    )
