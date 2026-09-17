# ADR 0004: Permission-Aware Hybrid Retrieval

- Status: Accepted
- Date: 2026-09-17

## Context

The product answers enterprise support questions from the customer's own
documents. Two properties are non-negotiable, and they pull in opposite
directions:

1. **Recall.** A question phrased in the customer's own words must find the
   policy paragraph written in formal language.
2. **Isolation.** A retrieved chunk must never come from a document the caller
   may not read, and must never come from another tenant. In this product a
   leaked chunk is not a bad answer — it is an internal policy document
   disclosed to the wrong party.

Pure vector search fails (1) on exact identifiers: error codes, SKUs, ticket
numbers and quoted phrases have weak or misleading embeddings. Pure full-text
search fails (1) on paraphrase. So both are needed.

Every candidate source is also a place isolation can fail: FTS, vectors, the
fusion step, and any reranking. A filter applied after fusion is a filter that
has already read the wrong data.

## Decision

**Hybrid retrieval with pre-filtering, reciprocal rank fusion, and an ACL
predicate that lives in exactly one place.**

Pipeline (`retrieval/hybrid.py`):

```
tenant + ACL pre-filter (SQL, before scoring)
      ├── FTS candidates   ─┐
      └── vector candidates ─┴─▶ RRF fusion ─▶ optional reranker ─▶ citations
```

### 1. Filter before scoring, in SQL

The tenant predicate and the ACL predicate are part of the candidate queries,
not applied to their results. Concretely, every candidate row must satisfy:

- `tenant_id = CAST(:tid AS uuid)` — from `TenantContext`, never the request;
- `document_versions.status = 'active'` — superseded and expired versions are
  not retrievable;
- the effective/expiry time window;
- the ACL predicate.

Post-filtering was rejected outright. Beyond the obvious leak risk, a
post-filter destroys the candidate budget: if 40 candidates are fetched and 35
are then discarded, the caller gets 5 results and no signal that the budget was
consumed by unauthorized rows.

### 2. Reciprocal rank fusion, not score averaging

`RRF_K = 60`, fused as `Σ 1 / (RRF_K + rank)` over both rankings.

Fusing by *rank* rather than by *score* is the decision that matters. FTS
reports `ts_rank`, which is unbounded and corpus-dependent; vector search
reports a cosine distance. There is no calibrated mapping between them, and
normalising them into a common scale invents a relationship that does not
exist — min-max normalisation makes the top hit of each list equal regardless of
whether it was strong or marginal.

RRF sidesteps this: it only uses ordering, so it needs no comparability
assumption. A document ranked highly by either method surfaces, and a document
ranked highly by both strongly surfaces.

**Consequence: retrieval scores are ranking diagnostics, never confidence
probabilities** (`docs/api-contracts.md`). The fusion score is a sum of
reciprocals with no probabilistic meaning. Treating it as confidence would make
the abstention threshold a number nobody can justify, which is why abstention is
driven by *evidence presence* rather than by a score cutoff.

### 3. The ACL predicate has exactly one implementation

`knowledge/acl_service.py` owns it, in two forms that must agree:

- `acl_filter_fragment()` — the SQL fragment retrieval splices into its queries.
  A code-owned constant; every value is a bound parameter.
- `can_read_document()` — the same rule in Python, for single-document callers.

Semantics:

> A resource carrying ACL entries is readable only if one of those entries
> matches the caller's principal set. A resource carrying **no** ACL entries is
> readable by anyone in the tenant.

The default-open clause is deliberate: documents are already tenant-scoped by
RLS, and requiring an explicit grant per document would make every upload
invisible until someone remembered to grant it. The first clause is what makes a
grant meaningful — adding an entry *narrows* a resource. The rule is
**fail-closed per resource**, not fail-closed globally.

This module exists because the predicate was originally inline in retrieval.
That was fine until a second path could hand out content — a download URL. At
that point "can this principal read this document" had two implementations, and
they would drift. Drift here is a security bug: a document invisible to search
but still downloadable is still a leak.

### 4. Structure-aware chunking feeds citation quality

Chunks carry `section_path` (from `knowledge/ingest.py` heading hierarchy) and
`ordinal`. This is a retrieval decision, not a formatting one: an excerpt
reading "within five days" is useless without knowing *of what*. Citations
without section context do not let a support agent verify the answer, which
defeats the point of citing at all.

Chunk boundaries are also constrained by the storage reality: `chunks.search_vector`
is `GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED` and
`UNIQUE (document_version_id, ordinal)`. Inserts must exclude the generated
column and be idempotent per version — the ingestion worker deletes before
inserting precisely so a reclaimed claim converges instead of colliding.

### 5. Embeddings are a boundary, not a dependency

`hybrid_search` depends on the narrow `Embedder` protocol
(`embed_query(query) -> list[float]`), not on the `llm` package. Two reasons:

- It keeps the retrieval module free of provider concerns, so tests can run with
  no network.
- It makes the batch/query split explicit. Ingestion needs
  `embed(texts) -> EmbeddingResult` (batch); retrieval needs a single query
  vector. These are deliberately different interfaces — retrieval must not be
  able to request a batch and ingest must not be able to ask one at a time.

A test double implementing only one of the two shapes will let one path look
green while the other is never exercised. This happened: a stub with only
`embed()` produced a passing worker test and a failing search path.

## Why not a dedicated search cluster

OpenSearch, Elasticsearch and similar were rejected for the same reason as in
ADR 0002: a new infrastructure component needs a benchmark, an operational need
and an ADR. PostgreSQL with pgvector and `tsvector` meets pilot scale, keeps
retrieval inside the same transaction and same RLS boundary as the data, and
therefore inherits tenant isolation instead of requiring a second implementation
of it. A separate cluster would need its own filter, its own sync path, and its
own way to be wrong.

## Consequences

### Positive

- Exact-match and paraphrase queries both work; neither method alone is a
  coverage hole.
- Isolation is enforced before any scoring, and via RLS independently of the
  application predicate.
- The ACL rule has one definition, so the search path and the download path
  cannot disagree.
- Citations carry section context a human can verify.

### Negative

- Two candidate queries and a fusion step per search: more SQL, more latency
  than either method alone.
- `RRF_K` is a fixed constant. It is the standard value, but it is a
  hyperparameter with no tuning evidence behind it yet — it should be revisited
  against the evaluation dataset rather than assumed.
- Vector recall is bounded by embedding quality, which is a provider property we
  do not control.

### Neutral

- Fusion scores are diagnostics, so any UI that shows them must not present them
  as confidence.

## Constraints

- Tenant and ACL filtering happen before scoring. No post-filtering.
- Never treat an RRF score as calibrated confidence (`AGENTS.md`: "Treating
  vector similarity as calibrated confidence" is a prohibited shortcut).
- The ACL predicate has one implementation; a second copy is a defect.
- `chunks.search_vector` is generated and must never be written.
- Embedding calls go through the narrow protocol; retrieval must not import
  `llm`.
- A retrieved citation must reference an `active`, in-window, authorized
  version.

## Revisit criteria

Revisit if any of the following becomes measurable:

- Recall or citation coverage on the evaluation dataset plateaus below the
  Phase 4 release gate (≥ 95% citation support) in a way that fusion or chunking
  explains;
- Candidate generation latency becomes the dominant contribution to first-token
  P95 (target < 2.5 s);
- The corpus outgrows what pgvector can serve within the latency budget at pilot
  concurrency — at which point a dedicated index is justified by a benchmark
  rather than by intuition.
