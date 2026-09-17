"""Ingestion consumer: DocumentVersion -> chunks (tickets 12-14).

The gap this closes
-------------------
`knowledge/router.py` accepts an upload and leaves the version at
`ingestion_status = 'uploaded'`. `ingest.py` owns the state machine and the
chunking functions, `retrieval/hybrid.py` can search `chunks`, and the
`chunks` table exists with its tsvector and pgvector columns. Nothing drove
the middle: no row was ever parsed, split, embedded or indexed, so
`hybrid_search` returned an empty list for every query in every deployment.
A knowledge base that silently answers nothing is worse than one that is
down, because the orchestrator treats "no evidence" as an abstention and the
failure is indistinguishable from a genuinely unanswerable question.

What this module does, in the order the state machine allows:

    UPLOADED -> PARSING -> CHUNKING -> EMBEDDING -> INDEXING -> READY

Claiming follows `inbox_consumer` exactly: `SELECT ... FOR UPDATE SKIP
LOCKED` plus an in-place status change as the claim token, so two workers
cannot process the same version, and a crashed worker's claim is reclaimed
after a timeout instead of stranding the document forever.

Three decisions worth stating
-----------------------------

1. **`status` moves to `active` only at the very end.** `hybrid_search`
   filters `dv.status = 'active'`, so promoting the version before its
   chunks exist would make a partially-indexed document visible - retrieval
   would return whichever chunks landed first, and a citation could point at
   a document whose text is only half present. READY and active are set in
   one transaction, after the chunks are written.

2. **Failure is terminal and explicit.** A version that cannot be parsed or
   embedded goes to FAILED with the reason recorded in `metadata`, not
   silently re-queued. The transition table allows `FAILED ->
   QUEUED_FOR_RETRY`, so a retry is a deliberate operator action rather than
   something a crash loop performs invisibly. The one exception is a
   transient storage/model outage, which is classified as retryable and left
   claimable (see `_Retryable`).

3. **Re-indexing is idempotent by deleting first.** Chunks carry
   `UNIQUE (document_version_id, ordinal)`; a retry after a partial write
   would otherwise fail on the duplicate and look like a parse error. The
   delete and the inserts are in the same transaction, so a crash mid-write
   leaves the previous chunk set intact rather than an empty one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from observability import JsonLogger
from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant
from platform_core.knowledge import ingest
from platform_core.knowledge.models import Chunk, DocumentVersion, IngestionStatus

logger = JsonLogger("platform.worker")

# How long a version may sit in an in-progress state before another worker
# may take it. Embedding a 25 MiB document in batches is the slow path;
# 15 minutes leaves headroom while still bounding how long a document stays
# invisible after a crash.
STALE_INGESTION_SECONDS = 900

# States that mean "a worker is on it". A row in one of these is either being
# processed right now or was abandoned by a dead worker.
IN_PROGRESS_STATES = frozenset(
    {
        IngestionStatus.PARSING.value,
        IngestionStatus.CHUNKING.value,
        IngestionStatus.EMBEDDING.value,
        IngestionStatus.INDEXING.value,
    }
)

# Claimable states. UPLOADED is the fresh path; QUEUED_FOR_RETRY is the
# operator-triggered retry path (FAILED -> QUEUED_FOR_RETRY -> PARSING).
CLAIMABLE_STATES = frozenset(
    {
        IngestionStatus.UPLOADED.value,
        IngestionStatus.QUEUED_FOR_RETRY.value,
    }
)

# Provider batch size. Embedding endpoints reject very large inputs, and
# batching bounds the memory a single cycle can hold.
EMBED_BATCH_SIZE = 32

# Bound on how much text a single chunk may carry into the model. Chunks are
# already capped by ingest.MAX_CHUNK_CHARS; this is a second ceiling for
# pathological inputs (one giant paragraph survives un-split by design).
MAX_EMBED_CHARS = 8000


class _Retryable(Exception):
    """A fault that is expected to clear on its own.

    Storage and model outages must not burn the document: parking it in
    FAILED means a human has to notice and requeue, for a failure that is
    about the dependency rather than the document. The claim is released
    (status back to UPLOADED) so the next cycle picks it up.
    """


class IngestionError(Exception):
    """A fault attributable to the document itself: bad bytes, bad markup."""


@dataclass
class ClaimedVersion:
    version_id: uuid.UUID
    tenant_id: uuid.UUID
    object_uri: str
    from_status: str
    # The state the claim moved the row to (PARSING). Carried so `_advance`
    # knows what the row currently holds without re-reading it, and so the
    # claim's own write is the single source of that fact.
    claimed_status: str = IngestionStatus.PARSING.value


@dataclass
class IngestStats:
    """Per-cycle outcome, returned so callers and tests can assert on it."""

    claimed: int = 0
    ready: int = 0
    failed: int = 0
    deferred: int = 0
    reclaimed: int = 0

    @property
    def processed(self) -> int:
        return self.ready + self.failed + self.deferred


async def claim_versions(session: AsyncSession, *, batch: int = 5) -> list[ClaimedVersion]:
    """Claim versions awaiting ingestion.

    Claiming goes through `claim_ingestion_versions` (migration 0018) rather
    than a plain SELECT, and that is not a style choice. The worker connects
    as `platform_app` (NOBYPASSRLS) so the database - not application code -
    enforces tenant isolation, but `document_versions` is RLS'd on a tenant
    the worker does not yet know. Measured with 20 rows present:

        platform (superuser)      visible = 20
        platform_app (unbound)    visible = 0

    Zero is indistinguishable from "the queue is empty", so a plain SELECT
    here returns nothing forever and the worker reports an idle queue while
    every uploaded document stays unindexed. The function is SECURITY DEFINER,
    returns only (id, tenant_id, object_uri, status), takes nothing but a
    batch size, and does its own `FOR UPDATE SKIP LOCKED` - so the escalation
    is confined to a claim and cannot be pointed at a chosen row.

    `FOR UPDATE SKIP LOCKED` is what makes this safe with more than one
    worker: a row locked by another transaction is skipped rather than waited
    on, so two workers get disjoint batches and neither blocks.

    The status transition that makes the claim durable happens next, in the
    caller's transaction: a committed row write, not an in-memory reservation,
    so a crash mid-ingest is recoverable by `reclaim_stale_ingestion` instead
    of losing the document.
    """
    rows = (
        await session.execute(
            text("SELECT * FROM claim_ingestion_versions(CAST(:batch AS integer))"),
            {"batch": batch},
        )
    ).mappings().all()
    if not rows:
        return []

    claimed: list[ClaimedVersion] = []
    for row in rows:
        # The function filters to claimable states, so every returned row can
        # legally move to PARSING. The transition is validated anyway rather
        # than assumed: the transition table is the authority, and if the two
        # ever disagree the failure must be loud instead of a row that claims
        # to be indexing while nothing ran.
        target = ingest.transition(str(row["ingestion_status"]), IngestionStatus.PARSING.value)
        claimed.append(
            ClaimedVersion(
                version_id=row["version_id"],
                tenant_id=row["tenant_id"],
                object_uri=str(row["object_uri"]),
                from_status=str(row["ingestion_status"]),
                claimed_status=target,
            )
        )

    # The status write needs a tenant binding, and this is not optional.
    # `document_versions` is FORCE-RLS'd, so an unbound `platform_app` UPDATE
    # matches **zero rows and reports success** - measured:
    #
    #     unbound UPDATE ... WHERE id = <known id>   rowcount = 0
    #     bound   UPDATE ... WHERE id = <known id>   rowcount = 1
    #
    # The rowcount is the only signal; nothing raises. Without this the claim
    # would be read but never recorded, so the next poll would hand the same
    # version to another worker, and the duplicate insert would surface as a
    # `UNIQUE (document_version_id, ordinal)` violation on a document that
    # looked fine.
    #
    # Binding a row's *own* tenant is not an escalation: the tenant came from
    # the row, not from the caller.
    for version in claimed:
        await apply_rls_tenant(
            session,
            TenantContext(tenant_id=version.tenant_id, actor_id=None, actor_kind="system"),
        )
        await session.execute(
            update(DocumentVersion)
            .where(DocumentVersion.id == version.version_id)
            .values(ingestion_status=version.claimed_status)
        )
    await session.flush()
    return claimed


async def reclaim_stale_ingestion(
    session: AsyncSession, *, timeout_seconds: int = STALE_INGESTION_SECONDS
) -> int:
    """Return abandoned in-progress versions to a claimable state.

    A worker that claims a version and then dies leaves it in PARSING (or
    later) forever - nothing else ever moves it, so the document is
    permanently invisible and no alert fires: the state is a legitimate one
    to be in. Reclaiming is safe because ingestion is idempotent (chunks are
    replaced wholesale, not appended).

    The row goes back to UPLOADED rather than QUEUED_FOR_RETRY because it
    was never declared failed; UPLOADED is the state that means "not started".

    Runs through `reclaim_stale_ingestions` (migration 0019) for the same
    structural reason the claim does: this sweep is tenant-unbound by
    definition - it is looking for rows nobody is tracking - and an unbound
    `platform_app` UPDATE against an RLS'd table matches zero rows and
    reports success. A sweep that silently reclaims nothing is indistinguishable
    from a healthy queue, so the abandoned document would sit in PARSING
    forever while the worker reported an idle backlog.
    """
    result = await session.execute(
        text("SELECT reclaim_stale_ingestions(CAST(:timeout AS integer))"),
        {"timeout": timeout_seconds},
    )
    return int(result.scalar_one() or 0)


def _batch_texts(texts: list[str], size: int) -> list[list[str]]:
    return [texts[i : i + size] for i in range(0, len(texts), size)]


async def _embed_chunks(texts: list[str], embedder: Any) -> list[list[float]]:
    """Embed every chunk text, preserving ordinal alignment.

    Alignment is the whole point: vectors are written back by index, so a
    provider that returned them out of order would attach every chunk's
    embedding to the wrong text. The provider contract already sorts by
    `index` and raises when the count disagrees, and the assertion here
    makes the *caller* fail loudly rather than write a shifted vector.

    Every provider error is re-raised as `_Retryable`, and that mapping is
    the load-bearing part. `llm.provider` raises a taxonomy
    (`ModelUnavailable`, `ModelRejected`, `ModelNotConfigured`), but a
    transport library can also escape with a raw exception, and an outage
    must not be recorded as a defect in the *document*: parking a perfectly
    good upload in FAILED because the provider was briefly down means a
    human has to notice and requeue, for a fault that clears by itself.

    `ModelRejected` is the one genuine exception - a 4xx means the request
    itself is wrong (bad model name, oversized input, revoked key), which no
    amount of retrying fixes. It is re-raised as `IngestionError` so the row
    fails loudly and someone reads the reason.
    """
    from platform_core.llm.provider import ModelRejected

    vectors: list[list[float]] = []
    for batch in _batch_texts(texts, EMBED_BATCH_SIZE):
        try:
            result = await embedder.embed(batch)
        except ModelRejected as exc:
            raise IngestionError(f"embedding request rejected: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - provider boundary
            raise _Retryable(f"embedding provider unavailable: {type(exc).__name__}") from exc
        got = list(result.vectors)
        if len(got) != len(batch):
            raise _Retryable(f"embedder returned {len(got)} vectors for {len(batch)} inputs")
        vectors.extend(got)
    if len(vectors) != len(texts):
        raise _Retryable(f"embedding count mismatch: {len(vectors)} for {len(texts)} chunks")
    return vectors


def _decode(raw: bytes) -> str:
    """Decode an uploaded object as text.

    The pilot's ingestible formats are text-shaped (markdown, plain text,
    JSON). A binary PDF raises rather than being decoded with replacement
    characters, because a mangled extraction that embeds as plausible-looking
    text is undetectable downstream while a hard failure is not. PDF parsing
    is a real feature with a real dependency; it is not this function.
    """
    for encoding in ("utf-8", "utf-8-sig"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise IngestionError(
        "object is not decodable text (binary format not supported by this pipeline)"
    )


async def ingest_version(
    session: AsyncSession,
    version: ClaimedVersion,
    *,
    embedder: Any,
) -> int:
    """Run the full pipeline for one claimed version. Returns the chunk count.

    Raises `_Retryable` when the failure is the dependency's, `IngestionError`
    when it is the document's. The caller decides what each means for the
    row's final state.
    """
    from platform_core.knowledge import service

    if not version.object_uri:
        raise IngestionError("version has no stored object")

    # The claim already moved the row to PARSING, so the pipeline's own first
    # transition is PARSING -> CHUNKING. Starting from `version.from_status`
    # here would re-apply PARSING and raise InvalidTransition (PARSING ->
    # PARSING is not in the table) - the row is already past that step.
    await _advance(session, version.version_id, IngestionStatus.CHUNKING)

    try:
        raw = service.get_object(version.object_uri)
    except Exception as exc:  # noqa: BLE001 - storage boundary
        raise _Retryable(f"object storage unavailable: {type(exc).__name__}") from exc

    text = _decode(raw)
    sections = ingest.parse_markdown_sections(text)
    if not sections:
        raise IngestionError("no sections found in the document")

    chunks = ingest.chunk_sections(sections)
    if not chunks:
        raise IngestionError("chunking produced no chunks")

    await _advance(session, version.version_id, IngestionStatus.EMBEDDING)
    texts = [c.text[:MAX_EMBED_CHARS] for c in chunks]
    vectors = await _embed_chunks(texts, embedder)

    await _advance(session, version.version_id, IngestionStatus.INDEXING)
    # Replace, do not append: the unique constraint on (version, ordinal)
    # would reject a retry that inserted a second time, and mixing two
    # pipeline runs' chunks in one version is never what a caller wants.
    await session.execute(
        delete(Chunk).where(
            Chunk.document_version_id == version.version_id,
            Chunk.tenant_id == version.tenant_id,
        )
    )
    embeddings: list[tuple[int, list[float]]] = []
    for ordinal, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True)):
        session.add(
            Chunk(
                tenant_id=version.tenant_id,
                document_version_id=version.version_id,
                section_path=list(chunk.path),
                ordinal=ordinal,
                text=chunk.text,
                text_hash=service.content_hash(chunk.text.encode()),
                metadata_json={"embedding_model": getattr(embedder, "model_name", None)},
            )
        )
        # `search_vector` is GENERATED ALWAYS AS ... STORED, so it is
        # deliberately absent from this insert. Naming it would be rejected
        # by the server with "cannot insert a non-DEFAULT value into column".
        #
        # `embedding` has no ORM type (platform_core has no pgvector type
        # registered), so it is written with an explicit UPDATE below, after
        # the rows exist. Doing it here as a second statement keeps the whole
        # write in one transaction: a failure on chunk 30 rolls back chunks
        # 1-29 rather than committing a half-embedded version.
        embeddings.append((ordinal, vector))

    await session.flush()
    await _write_embeddings(session, version, embeddings)

    # --- INDEXING -> READY, and only now is the version retrievable ---
    await _advance(session, version.version_id, IngestionStatus.READY)
    await session.execute(
        update(DocumentVersion)
        .where(DocumentVersion.id == version.version_id)
        .values(status="active", metadata_json=_merged_metadata(version, chunks=len(chunks)))
    )
    return len(chunks)


async def _write_embeddings(
    session: AsyncSession, version: ClaimedVersion, embeddings: list[tuple[int, list[float]]]
) -> None:
    """Attach vectors to the chunk rows just inserted.

    `platform_core` registers no pgvector column type, so the vector cannot
    go through the ORM insert. The literal format below (`[a,b,c]`) is the
    same one `retrieval/hybrid.py` reads, so both sides agree on the wire
    form rather than depending on a driver cast that one of them would have
    to guess.

    Bound as a parameter, never interpolated: the vector comes from a model
    response, which is external data.
    """
    from sqlalchemy import bindparam, text


    for ordinal, vector in embeddings:
        literal = "[" + ",".join(f"{x:.6f}" for x in vector) + "]"
        await session.execute(
            text(
                "UPDATE chunks SET embedding = CAST(:vec AS vector) "
                "WHERE document_version_id = :vid AND ordinal = :ord"
            ).bindparams(
                # Explicit CAST keeps the parameter a string on the way in;
                # the server does the parse, so a malformed literal is a
                # database error rather than a silently NULL column.
                bindparam("vec"),
                bindparam("vid"),
                bindparam("ord"),
            ),
            {"vec": literal, "vid": version.version_id, "ord": ordinal},
        )


async def _advance(session: AsyncSession, version_id: uuid.UUID, target: IngestionStatus) -> None:
    """Persist one legal transition, validating it against the state machine.

    Reads the current state from the row rather than trusting the caller's
    idea of it, so a concurrent actor that already moved the version cannot
    cause an illegal jump to be written silently. That read is also what
    caught the off-by-one this pipeline originally had: after the claim sets
    PARSING, asking for PARSING again is `InvalidTransition`, not a no-op.

    The session must already be tenant-scoped for this row; otherwise the
    SELECT returns nothing and the transition is reported as a lost version.
    """
    row = (
        await session.execute(select(DocumentVersion).where(DocumentVersion.id == version_id))
    ).scalar_one_or_none()
    if row is None:
        raise IngestionError(
            "version is not visible to this session during ingestion "
            "(missing tenant binding, or it was deleted)"
        )
    row.ingestion_status = ingest.transition(str(row.ingestion_status), target.value)
    await session.flush()


def _merged_metadata(version: ClaimedVersion, *, chunks: int) -> dict[str, Any]:
    return {"ingested_chunks": chunks, "pipeline_version": ingest.PIPELINE_VERSION}


async def mark_failed(session: AsyncSession, version_id: uuid.UUID, error: str) -> None:
    """Record a terminal ingestion failure.

    The version is left in FAILED and is not retrievable. That is the point:
    a version that claims to be indexed while its chunks are absent would
    produce citations to nothing.

    The caller must already have `app.tenant_id` bound for this row -
    `drain_ingestion_once` does so before dispatching, and `claim_versions`
    binds per claimed row. An unbound write here would match zero rows and
    silently leave the document in PARSING with no recorded reason.
    """
    row = (
        await session.execute(select(DocumentVersion).where(DocumentVersion.id == version_id))
    ).scalar_one_or_none()
    if row is None:
        # Unreadable row: either it does not exist or the session's tenant
        # does not match it. Reporting is the caller's job (it logs an error
        # either way); silently pretending to have written is not.
        return
    row.ingestion_status = ingest.transition(
        str(row.ingestion_status), IngestionStatus.FAILED.value
    )
    row.status = "failed"
    metadata = dict(row.metadata_json or {})
    metadata["ingestion_error"] = error[:2000]
    row.metadata_json = metadata
    await session.flush()


async def release_claim(session: AsyncSession, version: ClaimedVersion) -> None:
    """Release a claim so the next cycle retries.

    Returning to UPLOADED (not FAILED) is what distinguishes a dependency
    outage from a document defect; the operator sees the document stay
    "uploaded" and pending rather than failed.

    Read-modify-write through the ORM rather than a bare UPDATE: the session
    is tenant-scoped by the time this runs, and the ORM path fails loudly if
    the row is not visible, whereas an UPDATE would return zero rows and
    leave the claim stuck in PARSING.

    The assignment is direct rather than via `ingest.transition`, and that is
    deliberate: `_TRANSITIONS` describes the pipeline's forward progress, and
    PARSING -> UPLOADED is the *undoing* of a claim, not a step in it. Routing
    it through the table would raise `InvalidTransition` on the one path that
    exists to recover from a failure.
    """
    row = (
        await session.execute(
            select(DocumentVersion).where(DocumentVersion.id == version.version_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise IngestionError("cannot release a claim on a version this session cannot see")
    row.ingestion_status = IngestionStatus.UPLOADED.value
    await session.flush()


async def drain_ingestion_once(
    session: AsyncSession,
    *,
    embedder: Any,
    batch: int = 5,
    reclaim_timeout_seconds: int = STALE_INGESTION_SECONDS,
) -> IngestStats:
    """Claim and ingest one batch. Returns what happened.

    Per-version isolation mirrors `inbox_consumer.drain_once`: one poison
    document is marked FAILED and the batch continues, so a single bad
    upload cannot stall the queue.
    """
    stats = IngestStats()
    reclaimed = await reclaim_stale_ingestion(session, timeout_seconds=reclaim_timeout_seconds)
    if reclaimed:
        # A nonzero count means a worker died mid-ingest and documents sat
        # invisible until now. Worth surfacing rather than absorbing.
        logger.warning("stale_ingestions_reclaimed", count=reclaimed)
        stats.reclaimed = reclaimed

    versions = await claim_versions(session, batch=batch)
    stats.claimed = len(versions)
    if not versions:
        return stats

    for version in versions:
        # Scope the session to the claimed version's tenant before touching
        # any content. `apply_rls_tenant` sets `app.tenant_id` transaction-
        # locally, so a missing scope returns zero rows rather than every
        # tenant's rows - and setting it per version is what lets one worker
        # process a batch spanning tenants without ever mixing them.
        ctx = TenantContext(tenant_id=version.tenant_id, actor_id=None, actor_kind="system")
        await apply_rls_tenant(session, ctx)

        try:
            count = await ingest_version(session, version, embedder=embedder)
        except _Retryable as exc:
            await release_claim(session, version)
            logger.warning(
                "ingestion_deferred",
                version_id=str(version.version_id),
                reason_code=type(exc).__name__,
                detail=str(exc)[:200],
            )
            stats.deferred += 1
            continue
        except Exception as exc:  # noqa: BLE001 - per-version isolation
            # Includes IngestionError and anything unforeseen: a document
            # that cannot be ingested must be visible as failed, not
            # retried forever behind a log line nobody reads.
            await mark_failed(session, version.version_id, f"{type(exc).__name__}: {exc}")
            logger.error(
                "ingestion_failed",
                version_id=str(version.version_id),
                error_code=type(exc).__name__,
            )
            stats.failed += 1
            continue

        logger.info(
            "ingestion_completed",
            version_id=str(version.version_id),
            tenant_id=str(version.tenant_id),
            chunks=count,
        )
        stats.ready += 1

    return stats
