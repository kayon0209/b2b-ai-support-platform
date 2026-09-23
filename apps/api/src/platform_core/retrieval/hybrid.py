"""Hybrid retrieval service (ticket 14; iteration plan 1.3/1.5/1.7/4.5).

Pipeline (docs/architecture.md search architecture):
  ACL/tenant/metadata pre-filter -> FTS + vector + trigram + alias-expanded
  candidates -> rank fusion (RRF) -> optional authority boost -> optional
  reranker -> relative score floor -> citations

Scores are ranking diagnostics, never confidence probabilities
(docs/api-contracts.md). Only active, unexpired, authorized versions are
retrievable (docs/domain-model.md knowledge rule).

Why four paths
--------------
No single lexical or dense path covers the query distribution:

- FTS matches whole tokens: misses typos, fault codes with an OCR'd letter,
  and every CJK query (`'simple' tsquery` has no CJK segmentation);
- vector recall is semantic but weak on exact identifiers, where a hash of
  the wrong token still lands near neighbours;
- trigram similarity is character-level, which covers typos, fault codes
  and CJK bigrams, but ranks poorly on long natural-language questions;
- the alias path injects tenant vocabulary ("GC-500" == "GateWay 500")
  that no generic embedding can know.

Each path can be disabled independently (`enabled_paths`), and its
per-candidate ranks are recorded on every result's `ranking` dict, so the
contribution of one path is measurable by turning it off and diffing the
recall report — not by arguing about it.
"""

import json
import struct
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

RRF_K = 60  # standard reciprocal-rank-fusion constant; config may override

# The retrieval paths, as a closed vocabulary: ranking dicts, metrics and the
# enabled-paths config all key on these, and a typo in a config value must be
# a validation error rather than a silently disabled path.
RETRIEVAL_PATHS: tuple[str, ...] = ("fts", "vector", "trigram", "alias")

# Metadata keys the filter is allowed to constrain on (plan 1.5). Shared with
# ingest.extract_front_matter's vocabulary: a key not listed here is
# free-form document metadata, never a retrieval constraint, and a client
# cannot invent a new one by sending it.
METADATA_FILTER_KEYS: frozenset[str] = frozenset(
    {
        "product",
        "model",
        "hardware_version",
        "firmware_version",
        "region",
        "language",
        "doc_type",
        "classification",
        "authority",
    }
)


@dataclass(frozen=True)
class MetadataFilter:
    """Equality constraints matched against chunk metadata via JSONB
    containment (`metadata @> {...}`), applied in SQL before scoring.

    Built only from allowlisted keys; values are server-side entities
    (intent extraction, front matter), never raw client filters."""

    conditions: Mapping[str, str]

    def is_empty(self) -> bool:
        return not self.conditions

    def as_metadata(self) -> dict[str, str]:
        return dict(self.conditions)


def build_metadata_filter(values: Mapping[str, Any] | None) -> MetadataFilter:
    """Validate and build a filter; unknown keys raise.

    Raising (not silently dropping) is deliberate: a caller passing an
    unallowlisted key is a code bug that would otherwise turn a targeted
    retrieval into an unfiltered one with nothing in the logs."""
    if not values:
        return MetadataFilter({})
    unknown = sorted(set(values) - METADATA_FILTER_KEYS)
    if unknown:
        raise ValueError(f"metadata filter keys not allowed: {unknown}")
    conditions = {str(k): str(v).strip().lower() for k, v in values.items() if str(v).strip()}
    return MetadataFilter(conditions)


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
    # None for tool-receipt pseudo-chunks (plan 3.4): the receipt's provenance
    # is its source_uri, not a document.
    document_version_id: uuid.UUID | None
    title: str
    section_path: list[str]
    excerpt: str
    source_uri: str
    score: float
    ranking: dict[str, float] = field(default_factory=dict)


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


async def load_aliases(session: AsyncSession, tenant_id: uuid.UUID) -> list[tuple[str, str, float]]:
    """Tenant alias rows as (alias, term, weight). Small table, read whole.

    Expansion happens in Python: matching aliases against query tokens here
    is one query and deterministic, whereas doing it in SQL per token is N
    queries to arrive at the same answer.
    """
    rows = await session.execute(
        text(
            "SELECT alias, term, weight FROM knowledge_aliases WHERE tenant_id = CAST(:tid AS uuid)"
        ),
        {"tid": str(tenant_id)},
    )
    return [(r.alias.lower(), r.term.lower(), float(r.weight)) for r in rows.fetchall()]


def expand_with_aliases(query: str, aliases: list[tuple[str, str, float]]) -> tuple[str, list[str]]:
    """Append canonical terms for aliases present in the query.

    Additive, like `conversation.rewrite_query`: the customer's surface
    string is kept untouched and canonical terms are appended, so anything
    that matched before still matches. Returns (expanded_query, applied_terms).
    """
    lowered = query.lower()
    applied: list[str] = []
    for alias, term, _weight in aliases:
        if alias and alias in lowered and term not in lowered:
            applied.append(term)
    if not applied:
        return query, []
    return f"{query} {' '.join(sorted(applied))}", applied


def _embedder_identity(embedder: Embedder) -> str:
    """Which embedder produced a vector, for the cache key (11.3).

    Two embedders produce vectors that are not comparable, so serving one's
    vector to the other would corrupt every similarity downstream without
    raising anything. Type name plus the model the instance reports is enough
    to keep them apart, and it is read defensively because `Embedder` is a
    protocol - a test double legitimately has no model attribute.
    """
    model = getattr(embedder, "_model", None) or getattr(embedder, "model", None)
    return f"{type(embedder).__name__}:{model or ''}"


async def _cached_embed_query(embedder: Embedder, query: str) -> list[float]:
    """Embed a query, reusing the vector when the same text came before.

    Only the embedding is cached, never the result set: an embedding depends on
    the text alone, while a result set also depends on tenant and ACL, so it is
    the one thing here that is safe to key on a string. See
    `retrieval.cache` for the full reasoning.
    """
    from platform_core.retrieval.cache import embedding_cache, embedding_key

    key = embedding_key(query, model=_embedder_identity(embedder))
    cached = embedding_cache.get(key)
    if cached is not None:
        return cached
    vector = await embedder.embed_query(query)
    # Stored only after a successful call: a failed embed must be retried, not
    # remembered.
    embedding_cache.put(key, vector)
    return vector


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
    trigram_candidates: int = 40,
    alias_candidates: int = 20,
    metadata_filter: MetadataFilter | None = None,
    enabled_paths: tuple[str, ...] | None = None,
    rrf_k: int = RRF_K,
    aliases: list[tuple[str, str, float]] | None = None,
    authority_boost: Mapping[str, float] | None = None,
    embedder: Embedder | None = None,
) -> list[RetrievedChunk]:
    """Run the enabled retrieval paths under tenant/ACL/metadata filter,
    fuse with RRF, optionally boost by document authority.

    The tenant filter is applied in SQL before any candidate is scored
    (pre-filter, not post-filter), per docs/security.md retrieval rules.
    When `principal` is given, knowledge ACLs narrow results further:
    spaces without any ACL rows stay open; a space/document with ACL rows
    requires a matching principal entry (fail closed per resource).

    `embedder` selects the query vector source. Omitting it falls back to
    the deterministic test embedder so existing call sites and tests keep
    working; production callers must pass a provider-backed embedder.

    `enabled_paths` defaults to every path; passing a subset is how a path's
    contribution to recall is measured (plan 1.3). An unknown path name
    raises rather than being ignored — a typo'd config must not silently
    narrow retrieval.
    """
    import time

    paths = enabled_paths or RETRIEVAL_PATHS
    unknown = sorted(set(paths) - set(RETRIEVAL_PATHS))
    if unknown:
        raise ValueError(f"unknown retrieval paths: {unknown}")

    now = now_ts or int(time.time())
    params: dict[str, Any] = {
        "tid": str(tenant_id),
        "q": query,
        "fts_k": fts_candidates,
        "now": now,
    }

    # space_filter is built from a code-owned constant, never user
    # input; all values flow through bound parameters (S608 suppressed
    # for this file in pyproject).
    space_filter = ""
    if knowledge_space_ids:
        space_filter = "AND d.space_id = ANY(:space_ids)"
        params["space_ids"] = [str(s) for s in knowledge_space_ids]

    # Metadata filter (plan 1.5): JSONB containment on the chunk row, bound
    # as a parameter. The filter object was validated at construction, so no
    # key here is client-invented.
    metadata_predicate = ""
    if metadata_filter and not metadata_filter.is_empty():
        # Match on the chunk's own metadata AND its version's front matter:
        # the filter's data source is document front matter (ingested into
        # version metadata), while chunk-level tags (content_kind, ...) live
        # on the chunk. Either carrying the value makes the chunk match.
        metadata_predicate = (
            "AND (c.metadata @> CAST(:meta_filter AS jsonb) "
            "OR dv.metadata @> CAST(:meta_filter AS jsonb))"
        )
        params["meta_filter"] = json.dumps(metadata_filter.as_metadata())

    if principal is not None:
        # Fail closed: any resource carrying ACL entries must include a
        # principal match. Resources without entries remain accessible.
        #
        # The predicate itself lives in `knowledge/acl_service.py`, because the
        # document download path evaluates the same rule for a single document.
        # Two copies would drift, and a document that is invisible to search
        # but downloadable is still a leak.
        from platform_core.knowledge.acl_service import acl_filter_fragment

        params["p_types"] = list(principal.principal_types)
        params["p_ids"] = list(principal.principal_ids)
        acl = acl_filter_fragment()
    else:
        acl = ""

    base_where = f"""
        WHERE c.tenant_id = CAST(:tid AS uuid)
          AND dv.status = 'active'
          AND (dv.effective_at IS NULL OR dv.effective_at <= :now)
          AND (dv.expires_at IS NULL OR dv.expires_at > :now)
          {space_filter}
          {metadata_predicate}
          {acl}
    """
    # `authority` (plan 4.5) travels with every row so the post-fusion boost
    # reads it from the representative row rather than issuing another query.
    select_cols = (
        "c.id, c.document_version_id, c.section_path, c.text, "
        "d.title, dv.object_uri AS source_uri, dv.metadata->>'authority' AS authority"
    )

    fts_sql = text(
        f"""
        SELECT {select_cols},
               ts_rank(c.search_vector, websearch_to_tsquery('simple', :q)) AS fts_score
        FROM chunks c
        JOIN document_versions dv ON dv.id = c.document_version_id
        JOIN documents d ON d.id = dv.document_id
        {base_where}
          AND c.search_vector @@ websearch_to_tsquery('simple', :q)
        ORDER BY fts_score DESC
        LIMIT :fts_k
        """
    )

    trigram_sql = text(
        f"""
        SELECT {select_cols},
               similarity(c.text, :q) AS trigram_score
        FROM chunks c
        JOIN document_versions dv ON dv.id = c.document_version_id
        JOIN documents d ON d.id = dv.document_id
        {base_where}
          AND similarity(c.text, :q) > 0.02
        ORDER BY trigram_score DESC
        LIMIT :tri_k
        """
    )
    params["tri_k"] = trigram_candidates

    vec_sql = text(
        f"""
        SELECT {select_cols},
               1 - (c.embedding <=> CAST(:vec AS vector)) AS vec_score
        FROM chunks c
        JOIN document_versions dv ON dv.id = c.document_version_id
        JOIN documents d ON d.id = dv.document_id
        {base_where}
          AND c.embedding IS NOT NULL
        ORDER BY c.embedding <=> CAST(:vec AS vector)
        LIMIT :vec_k
        """
    )
    params["vec_k"] = vector_candidates

    from sqlalchemy.engine import RowMapping

    results: dict[str, list[RowMapping]] = {}

    if "fts" in paths:
        results["fts"] = list((await session.execute(fts_sql, params)).mappings().all())

    if "vector" in paths:
        active_embedder: Embedder = embedder or DeterministicEmbedder()
        vec = await _cached_embed_query(active_embedder, query)
        params["vec"] = _vector_literal(vec)
        results["vector"] = list((await session.execute(vec_sql, params)).mappings().all())

    if "trigram" in paths:
        results["trigram"] = list((await session.execute(trigram_sql, params)).mappings().all())

    if "alias" in paths:
        alias_rows = aliases if aliases is not None else await load_aliases(session, tenant_id)
        expanded, applied = expand_with_aliases(query, alias_rows)
        if applied:
            alias_sql = text(
                f"""
                SELECT {select_cols},
                       ts_rank(c.search_vector, websearch_to_tsquery('simple', :aq)) AS alias_score
                FROM chunks c
                JOIN document_versions dv ON dv.id = c.document_version_id
                JOIN documents d ON d.id = dv.document_id
                {base_where}
                  AND c.search_vector @@ websearch_to_tsquery('simple', :aq)
                ORDER BY alias_score DESC
                LIMIT :alias_k
                """
            )
            alias_params = dict(params)
            alias_params["aq"] = expanded
            alias_params["alias_k"] = alias_candidates
            results["alias"] = list(
                (await session.execute(alias_sql, alias_params)).mappings().all()
            )

    # Reciprocal rank fusion over the enabled ranked lists.
    # chunk id -> accumulated RRF + per-path scores + representative row.
    scores: dict[uuid.UUID, dict[str, Any]] = {}
    path_score_keys = {
        "fts": "fts_score",
        "vector": "vec_score",
        "trigram": "trigram_score",
        "alias": "alias_score",
    }
    for path in RETRIEVAL_PATHS:
        rows = results.get(path)
        if not rows:
            continue
        score_key = path_score_keys[path]
        for rank, row in enumerate(rows):
            cid = row["id"]
            entry = scores.get(cid)
            if entry is None:
                entry = scores[cid] = {"row": row, "rrf": 0.0}
                for key in path_score_keys.values():
                    entry[key] = None
            entry["rrf"] += 1.0 / (rrf_k + rank + 1)
            if entry[score_key] is None and row.get(score_key) is not None:
                entry[score_key] = float(row[score_key])

    ranked = sorted(scores.items(), key=lambda kv: kv[1]["rrf"], reverse=True)

    # Authority boost (plan 4.5): a post-fusion multiplier, off by default.
    # Rank order and visibility are untouched — the boost only reorders
    # already-authorized candidates, so a misconfigured weight can rank a
    # document wrongly but never surface an unauthorized one.
    if authority_boost:

        def _boost(entry: dict[str, Any]) -> float:
            authority = (entry["row"].get("authority") or "").lower()
            return authority_boost.get(authority, 1.0)

        ranked = sorted(
            ranked,
            key=lambda kv: kv[1]["rrf"] * _boost(kv[1]),
            reverse=True,
        )

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
                "trigram": entry["trigram_score"],
                "alias": entry["alias_score"],
            },
        )
        for cid, entry in ranked[:top_k]
    ]
