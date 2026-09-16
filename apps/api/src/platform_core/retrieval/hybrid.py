"""Hybrid retrieval service (ticket 14).

Pipeline (docs/architecture.md search architecture):
  ACL/tenant pre-filter -> FTS candidates + vector candidates -> rank
  fusion (RRF) -> optional reranker -> citations

Scores are ranking diagnostics, never confidence probabilities
(docs/api-contracts.md). Only active, unexpired, authorized versions are
retrievable (docs/domain-model.md knowledge rule).
"""

import struct
import uuid
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

RRF_K = 60  # standard reciprocal-rank-fusion constant


class Embedder(Protocol):
    """Query-embedding boundary.

    Implemented by the provider-backed embedder in production and by
    `DeterministicEmbedder` in tests that must not depend on a provider.
    """

    async def embed_query(self, query: str) -> list[float]: ...

    @property
    def dimensions(self) -> int: ...


@dataclass(frozen=True)
class PrincipalScope:
    """Caller identity for ACL narrowing. Built server-side from the
    authenticated context; callers cannot grant themselves scope."""

    principal_types: tuple[str, ...]
    principal_ids: tuple[str, ...]


@dataclass
class RetrievedChunk:
    chunk_id: uuid.UUID
    document_version_id: uuid.UUID
    title: str
    section_path: list
    excerpt: str
    source_uri: str
    score: float
    ranking: dict = field(default_factory=dict)


def _vector_literal(vec: list[float]) -> str:
    """Render a float list as pgvector literal text."""
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def embed_deterministic(text_input: str, dim: int = 1536) -> list[float]:
    """Deterministic hash-based vector, for tests only.

    NOT a semantic embedding: it encodes no meaning and must never be used
    for production retrieval. `DeterministicEmbedder` wires it behind the
    Embedder protocol so tests exercise the vector path without a provider
    dependency or a credential.
    """
    out: list[float] = []
    counter = 0
    while len(out) < dim:
        digest = hashlib_sha256(f"{text_input}:{counter}".encode())
        for i in range(0, len(digest), 4):
            if len(out) >= dim:
                break
            word = digest[i : i + 4]
            out.append(struct.unpack(">I", word)[0] / 2**32 - 0.5)
        counter += 1
    norm = sum(x * x for x in out) ** 0.5 or 1.0
    return [x / norm for x in out]


def hashlib_sha256(data: bytes) -> bytes:
    import hashlib

    return hashlib.sha256(data).digest()


@dataclass
class DeterministicEmbedder:
    """Test double satisfying the Embedder protocol."""

    _dimensions: int = 1536

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed_query(self, query: str) -> list[float]:
        return embed_deterministic(query, self._dimensions)


@dataclass
class ProviderEmbedder:
    """Provider-backed embedder (production path)."

    Wraps a ChatProvider-style embedding capability behind the retrieval
    module's narrow interface so `hybrid_search` does not depend on the
    llm package directly.
    """

    provider: object  # EmbeddingProvider; typed loosely to keep layering clean
    _dimensions: int = 1536

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed_query(self, query: str) -> list[float]:
        result = await self.provider.embed([query])  # type: ignore[attr-defined]
        return list(result.vectors[0])


async def hybrid_search(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    query: str,
    top_k: int = 8,
    knowledge_space_ids: list[uuid.UUID] | None = None,
    principal: PrincipalScope | None = None,
    now_ts: int | None = None,
    fts_candidates: int = 40,
    vector_candidates: int = 40,
    embedder: Embedder | None = None,
) -> list[RetrievedChunk]:
    """Run FTS + vector search under tenant/ACL filter, fuse with RRF.

    The tenant filter is applied in SQL before any candidate is scored
    (pre-filter, not post-filter), per docs/security.md retrieval rules.
    When `principal` is given, knowledge ACLs narrow results further:
    spaces without any ACL rows stay open; a space/document with ACL rows
    requires a matching principal entry (fail closed per resource).

    `embedder` selects the query vector source. Omitting it falls back to
    the deterministic test embedder so existing call sites and tests keep
    working; production callers must pass a provider-backed embedder.
    """
    import time

    now = now_ts or int(time.time())
    active_embedder: Embedder = embedder or DeterministicEmbedder()
    vec = await active_embedder.embed_query(query)
    space_filter = ""
    params: dict = {
        "tid": str(tenant_id),
        "q": query,
        "vec": _vector_literal(vec),
        "fts_k": fts_candidates,
        "vec_k": vector_candidates,
        "now": now,
    }
    # space_filter is built from a code-owned constant, never user
    # input; all values flow through bound parameters (S608 suppressed
    # for this file in pyproject).
    if knowledge_space_ids:
        space_filter = "AND d.space_id = ANY(:space_ids)"
        params["space_ids"] = [str(s) for s in knowledge_space_ids]

    if principal is not None:
        # Fail closed: any resource carrying ACL entries must include a
        # principal match. Resources without entries remain accessible.
        params["p_types"] = list(principal.principal_types)
        params["p_ids"] = list(principal.principal_ids)
        acl = """
          AND NOT EXISTS (
            SELECT 1 FROM knowledge_acls a
            WHERE a.tenant_id = CAST(:tid AS uuid)
              AND (
                (a.resource_type = 'space' AND a.resource_id = d.space_id)
                OR (a.resource_type = 'document' AND a.resource_id = d.id)
                OR (a.resource_type = 'version' AND a.resource_id = dv.id)
              )
              AND NOT (
                a.principal_type = ANY(CAST(:p_types AS text[]))
                AND a.principal_id = ANY(CAST(:p_ids AS text[]))
              )
          )
        """
    else:
        acl = ""

    fts_sql = text(
        f"""
        SELECT c.id, c.document_version_id, c.section_path, c.text,
               d.title, dv.object_uri AS source_uri,
               ts_rank(c.search_vector, websearch_to_tsquery('simple', :q)) AS fts_score
        FROM chunks c
        JOIN document_versions dv ON dv.id = c.document_version_id
        JOIN documents d ON d.id = dv.document_id
        WHERE c.tenant_id = CAST(:tid AS uuid)
          AND dv.status = 'active'
          AND (dv.effective_at IS NULL OR dv.effective_at <= :now)
          AND (dv.expires_at IS NULL OR dv.expires_at > :now)
          {space_filter}
          {acl}
          AND c.search_vector @@ websearch_to_tsquery('simple', :q)
        ORDER BY fts_score DESC
        LIMIT :fts_k
        """
    )
    vec_sql = text(
        f"""
        SELECT c.id, c.document_version_id, c.section_path, c.text,
               d.title, dv.object_uri AS source_uri,
               1 - (c.embedding <=> CAST(:vec AS vector)) AS vec_score
        FROM chunks c
        JOIN document_versions dv ON dv.id = c.document_version_id
        JOIN documents d ON d.id = dv.document_id
        WHERE c.tenant_id = CAST(:tid AS uuid)
          AND dv.status = 'active'
          AND (dv.effective_at IS NULL OR dv.effective_at <= :now)
          AND (dv.expires_at IS NULL OR dv.expires_at > :now)
          {space_filter}
          {acl}
          AND c.embedding IS NOT NULL
        ORDER BY c.embedding <=> CAST(:vec AS vector)
        LIMIT :vec_k
        """
    )

    fts_rows = (await session.execute(fts_sql, params)).mappings().all()
    vec_rows = (await session.execute(vec_sql, params)).mappings().all()

    # Reciprocal rank fusion over the two ranked lists.
    scores: dict[uuid.UUID, dict] = {}
    for rank, row in enumerate(fts_rows):
        cid = row["id"]
        entry = scores.setdefault(
            cid,
            {
                "row": row,
                "rrf": 0.0,
                "fts_score": float(row["fts_score"]),
                "vec_score": None,
            },
        )
        entry["rrf"] += 1.0 / (RRF_K + rank + 1)
    for rank, row in enumerate(vec_rows):
        cid = row["id"]
        entry = scores.setdefault(
            cid,
            {
                "row": row,
                "rrf": 0.0,
                "fts_score": None,
                "vec_score": float(row["vec_score"]),
            },
        )
        entry["rrf"] += 1.0 / (RRF_K + rank + 1)
        if entry["fts_score"] is None:
            entry["vec_score"] = float(row["vec_score"])

    ranked = sorted(scores.items(), key=lambda kv: kv[1]["rrf"], reverse=True)[:top_k]
    return [
        RetrievedChunk(
            chunk_id=cid,
            document_version_id=entry["row"]["document_version_id"],
            title=entry["row"]["title"],
            section_path=entry["row"]["section_path"] or [],
            excerpt=entry["row"]["text"][:280],
            source_uri=entry["row"]["source_uri"],
            score=entry["rrf"],
            ranking={
                "lexical": entry["fts_score"],
                "vector": entry["vec_score"],
            },
        )
        for cid, entry in ranked
    ]
