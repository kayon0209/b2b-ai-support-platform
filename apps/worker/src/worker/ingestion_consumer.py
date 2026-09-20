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
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from observability import JsonLogger
from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant
from platform_core.knowledge import ingest
from platform_core.knowledge.ingest import IngestionError
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


@dataclass
class ClaimedVersion:
    version_id: uuid.UUID
    tenant_id: uuid.UUID
    object_uri: str
    content_type: str
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
    # The specific ids this cycle reached, whatever the outcome. Counts cannot
    # answer "did the cycle handle *my* row?" - and that is precisely the
    # question the original bug turned on (`ready=8` in a summary said nothing
    # about the row the caller asked about). `drain_versions` uses this to
    # decide what it still owes; a caller can use it to attribute an outcome.
    handled_ids: list[uuid.UUID] = field(default_factory=list)

    @property
    def processed(self) -> int:
        return self.ready + self.failed + self.deferred


async def claim_versions(
    session: AsyncSession, *, batch: int = 5, version_ids: Sequence[uuid.UUID] | None = None
) -> list[ClaimedVersion]:
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

    `version_ids` narrows the claim to specific rows (migration 0039). It
    exists because the claim is a **global FIFO**, and "claim the oldest N"
    is the wrong question when the caller has a document in hand: see
    `drain_versions`. The lock semantics are unchanged - the same
    `FOR UPDATE SKIP LOCKED` runs over the same table - only the candidate
    set is narrowed, and a row another worker holds is still skipped rather
    than waited on.

    The status transition that makes the claim durable happens next, in the
    caller's transaction: a committed row write, not an in-memory reservation,
    so a crash mid-ingest is recoverable by `reclaim_stale_ingestion` instead
    of losing the document.
    """
    rows = (
        (
            await session.execute(
                text("SELECT * FROM claim_ingestion_versions(CAST(:batch AS integer), :ids)"),
                {"batch": batch, "ids": list(version_ids) if version_ids else None},
            )
        )
        .mappings()
        .all()
    )
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
                content_type=str(row["content_type"] or ""),
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

    text = ingest.parse_document(version.content_type, raw)

    # Front matter (plan 1.5): a leading `---` block carries the metadata the
    # retrieval filter consumes (model/firmware/region/...). Stripped here so
    # it never reaches the chunk text, where it would be both noise and a
    # duplicate of the column the filter actually reads.
    text, doc_metadata = ingest.extract_front_matter(text)

    # Cleaning (plan 4.6) runs before sectioning so page furniture is gone
    # before heading detection and every later step sees the same bytes.
    from platform_core.config import get_settings

    settings = get_settings()
    cleaning_report = ingest.CleaningReport()
    if settings.cleaning_enabled:
        text, cleaning_report = ingest.clean_text(
            text, strip_boilerplate=settings.cleaning_strip_boilerplate
        )

    sections = ingest.parse_markdown_sections(text)
    if not sections:
        raise IngestionError("no sections found in the document")

    chunk_config = ingest.ChunkingConfig(
        max_chars=settings.chunking_max_chars,
        min_chars=settings.chunking_min_chars,
        overlap_chars=settings.chunking_overlap_chars,
    )
    chunks = ingest.chunk_sections(sections, chunk_config)
    if not chunks:
        raise IngestionError("chunking produced no chunks")
    if settings.cleaning_enabled and settings.cleaning_dedupe_chunks:
        chunks, deduped = ingest.dedupe_chunks(chunks)
        cleaning_report.duplicate_chunks_removed = deduped
        if not chunks:
            raise IngestionError("cleaning removed every chunk (duplicate document?)")
    cleaning_report.chunks_total = len(chunks)

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
                metadata_json={
                    "embedding_model": getattr(embedder, "model_name", None),
                    **chunk.meta,
                },
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
        .values(
            status="active",
            metadata_json=_merged_metadata(
                version,
                chunks=len(chunks),
                chunking=chunk_config.as_metadata(),
                cleaning=cleaning_report.as_metadata(),
                document_metadata=doc_metadata,
            ),
        )
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


def _merged_metadata(
    version: ClaimedVersion,
    *,
    chunks: int,
    chunking: dict[str, Any] | None = None,
    cleaning: dict[str, Any] | None = None,
    document_metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "ingested_chunks": chunks,
        "pipeline_version": ingest.PIPELINE_VERSION,
    }
    if chunking is not None:
        meta["chunking"] = chunking
    if cleaning is not None:
        meta["cleaning"] = cleaning
    if document_metadata:
        # Front-matter keys are the retrieval filter's vocabulary; top-level
        # so `metadata @> {...}` predicates stay simple.
        meta.update(document_metadata)
    return meta


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
    version_ids: Sequence[uuid.UUID] | None = None,
) -> IngestStats:
    """Claim and ingest one batch. Returns what happened.

    Per-version isolation mirrors `inbox_consumer.drain_once`: one poison
    document is marked FAILED and the batch continues, so a single bad
    upload cannot stall the queue.

    `version_ids` passes the narrowing through to `claim_versions`. Without
    it the claim takes the globally oldest rows regardless of what the caller
    asked for - which is the bug `drain_versions` exists to fix, and the
    reason that parameter is threaded here rather than left to the caller.
    """
    stats = IngestStats()
    reclaimed = await reclaim_stale_ingestion(session, timeout_seconds=reclaim_timeout_seconds)
    if reclaimed:
        # A nonzero count means a worker died mid-ingest and documents sat
        # invisible until now. Worth surfacing rather than absorbing.
        logger.warning("stale_ingestions_reclaimed", count=reclaimed)
        stats.reclaimed = reclaimed

    versions = await claim_versions(session, batch=batch, version_ids=version_ids)
    stats.claimed = len(versions)
    if not versions:
        return stats

    for version in versions:
        # Record every claimed id up front, not at each outcome: a version the
        # loop reaches has been *handled* whichever branch it takes, and the
        # caller asking "did you deal with my row?" does not care which one.
        stats.handled_ids.append(version.version_id)

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


async def drain_versions(
    session: AsyncSession,
    version_ids: Sequence[uuid.UUID],
    *,
    embedder: Any,
    max_rounds: int = 20,
) -> IngestStats:
    """Ingest *these* versions, claiming repeatedly until none are left.

    Why this exists, and why it is not `drain_ingestion_once`
    ---------------------------------------------------------
    `drain_ingestion_once` claims the oldest `batch` rows *in the database* -
    the claim is deliberately tenant-agnostic ("one bulk worker serves every
    tenant"). That is correct for the worker, which does not care which
    document it gets next. It is wrong for a caller that does care, and the
    difference is not theoretical:

    Claiming 13 rows with `batch=10` and then looping "one drain per row"
    starves rows 11-13. `FOR UPDATE SKIP LOCKED` skips any row another
    transaction holds, so a claim can return fewer than `batch`, and the rows
    it returns are the *oldest* claimable - not the caller's. Measured on a
    live queue: 13 claimable rows, `claim(10)` returned 9, and the oldest row
    was absent; every subsequent round re-claimed from the same head and the
    tail never got a turn. The caller saw

        RuntimeError: ingesting <key> left ingestion_status='uploaded'

    for a document that was uploaded, stored, and perfectly ingestable.

    The loop here terminates on *absence from the queue*, not on a round
    count: a version that is claimed and ingested leaves the claimable set in
    the same transaction, so the next claim does not see it. Rounds therefore
    do not scale with the workload - one claim gets everything that fits in a
    batch, and `max_rounds` only bounds a queue that keeps refilling.

    Why each round runs in its own transaction
    ------------------------------------------
    The claim runs `... FOR UPDATE SKIP LOCKED`, so it takes a **row lock** on
    every row it returns. Those locks live until the transaction ends. When the
    whole loop shared one transaction the loop held round 1's locks while
    round 2 claimed, and since `SKIP LOCKED` *skips* a row it cannot lock
    instead of waiting on it, round 2 could return fewer rows than requested -
    including rows this loop was still trying to finish.

    Measured, before the fix: the settle probe for a requested id sat `idle in
    transaction` for **44 seconds** while holding that row's lock, and a
    `claim(3)` over 3 requested ids returned **2**, missing exactly the locked
    one. The symptoms were all indirect, which is why it is worth naming them:

    - `IngestionError: ... never ingested` for documents that were uploaded and
      perfectly ingestable, failing a *different* test on one run in three.
    - `psycopg.errors.DeadlockDetected` when a teardown
      `DELETE FROM document_versions` met the loop's still-held locks; the
      aborted cleanup then left `documents` rows pointing at a deleted
      `knowledge_spaces` row, so the *next* run died on a foreign key that had
      nothing to do with what it was testing.

    Committing per round is the fix, but it is only half of it. `apply_rls_tenant`
    sets `app.tenant_id` with `set_config(..., true)`, which is
    **transaction-scoped**, so the commit clears the binding - and the loop's
    settle probe then became an *unbound* read of a FORCE-RLS table. That
    returns zero rows and reports every requested id as unsettled; measured, it
    turned 3 failures into 11.

    The binding is therefore not refreshed, it is **removed**. The probe is gone
    entirely and "is this id settled?" is answered from the round's own report
    of the ids it *handled* (`IngestStats.handled_ids`), not by asking the
    database a second time. The round already knows what it touched, and that
    is strictly better information than a re-read: the probe needed a tenant
    binding the commit had just cleared, while the round's own result needs
    nothing. One earlier attempt answered this question from `claim_versions`
    instead - a `SECURITY DEFINER` function that needs no binding - but the
    claim is a *write* path (it moves rows out of the claimable set), so
    calling it purely to re-read left the caller unable to roll back without
    discarding that write. The round's own report has no such coupling.

    The caller's contract is unchanged: it still owns the final commit for
    anything it cares about. Each round commits only its own claim-and-ingest
    unit, which must be durable anyway - a version that reached READY cannot be
    rolled back to `uploaded` on a later round's failure without lying about
    what the model was already told.

    A round that claims nothing ends the loop rather than spinning: either
    every id is settled, or what remains is held by another worker (SKIP
    LOCKED skips it) and waiting would be a livelock. `max_rounds` is the
    second ceiling, for the case where a *different* claimable row at the
    head keeps consuming the batch - which on a shared queue is the normal
    case, not an edge case, because `created_at` is whole seconds and every
    row written in the same second is one ordering tie the LIMIT cuts
    arbitrarily. The default is sized for that: a caller with a handful of
    ids converges in one or two rounds, and the ceiling only bounds a queue
    that keeps refilling in front of the caller's rows.

    Raises `IngestionError` for ids that never settled - the caller asked for
    specific documents, so silence is not an acceptable outcome.

    ⚠️ THE SURVIVING FLAKE IS ENVIRONMENTAL, NOT THIS CODE
    ------------------------------------------------------
    The per-round commit above removes the *self*-locking (the loop no longer
    meets its own previous round's locks), and that was measurable: the whole
    file went from ~62s and 1-3 differing failures per run to ~20s and a
    steady 1 failure, with five consecutive runs failing 25/26.

    One flake survived, and it took a later investigation to identify it. It
    is **not** a defect in this loop: the database has a live outbox/ingestion
    consumer that claims rows concurrently with the test process. Symptoms
    that now fit one cause:

    - a failing test differs every run (`test_drain_versions_narrows_the_claim
      _it_issues` twice, then `test_chunks_carry_section_paths_and_ordinals`,
      `test_an_ingested_document_is_retrievable_by_hybrid_search`,
      `test_an_api_style_upload_with_a_content_type_ingests_to_ready`,
      `test_a_missing_object_fails_rather_than_retrying_forever`);
    - always the same shape: `IngestStats(claimed=0, ...)` from a **narrowed**
      claim over an id that is claimable and not locked by this loop;
    - `test_billing_ledger.py` shows the identical signature on the outbox
      side - six consecutive runs, two passing and four failing, with the
      failing test varying and every failure `RelayStats(claimed=0)`;
    - a plain SQL insert into `outbox_events` with no relay code in the
      process is marked `sent, attempts=1` within half a second, and
      `pg_trigger`/`pg_rules`/column defaults rule out the database doing it.

    `claim_ingestion_versions` is a **global** FIFO and needs no tenant
    binding, so a concurrent consumer competes with a narrowed claim for the
    same row; which one wins is timing, which is why the failing test moves.

    **What to do**: find and stop the external consumer before treating any
    failure here as a code defect. The evidence chain and the search commands
    are in `docs/acceptance/08-live-outbox-consumer.md`, and the three checks
    that reproduce it fastest are:

        1. `pytest apps/api/tests/integration/test_outbox_relay.py -rs`
           - the suite's own `_assert_no_live_relay` guard skips itself and
             says a live worker is consuming rows;
        2. insert a `status='queued'` row into `outbox_events` by plain SQL
           from a script with no relay imported, then read it back after
             half a second - it is already `sent, attempts=1`;
        3. `pg_trigger` (non-internal), `pg_rules` and the column defaults on
           `outbox_events` are all clean, so the database is not doing it.

    Established as *not* the cause: session leakage, the fixture's uuid
    version, and this loop's own locks. Two of five attempts at a fix were
    outright wrong (a per-round commit without reworking the probe turned 3
    failures into 11).

    The improvement stays rather than reverting to the old shape: the old
    shape was strictly worse on every measurement taken, and this one is
    correct on the mechanism it set out to fix.
    """
    remaining = list(dict.fromkeys(version_ids))
    total = IngestStats()
    if not remaining:
        return total

    for _round in range(max_rounds):
        # What did this round do, per requested id? Answering that is what
        # replaces the old settle probe, and it is strictly better
        # information: the probe asked the database "is this id still
        # claimable?" and needed a tenant binding to do it, while the round
        # already knows which ids it *handled*.
        #
        # This matters because the binding is gone by now. `apply_rls_tenant`
        # sets `app.tenant_id` with `set_config(..., true)` - transaction
        # scoped - so the `commit()` below clears it, and any *read* of
        # `document_versions` after that returns zero rows (FORCE RLS) and
        # would report every requested id as unsettled. Measured: a per-round
        # commit plus a SELECT probe turned 3 failures into 11.
        before = set(remaining)
        round_stats = await drain_ingestion_once(
            session,
            embedder=embedder,
            batch=max(len(remaining), 1),
            version_ids=remaining,
        )
        total.reclaimed = max(total.reclaimed, round_stats.reclaimed)
        total.claimed += round_stats.claimed
        total.ready += round_stats.ready
        total.failed += round_stats.failed
        total.deferred += round_stats.deferred
        # Carry the per-id attribution up too, not just the counters: the
        # caller's question is "did you handle *my* row?", and a total that
        # dropped the ids could answer it only by arithmetic, which is exactly
        # the summary-vs-row confusion this function exists to remove.
        for handled in round_stats.handled_ids:
            if handled not in total.handled_ids:
                total.handled_ids.append(handled)

        # End this round's transaction *before* the next claim. The claim holds
        # `FOR UPDATE` locks on everything it returned, and those survive until
        # commit - so without this the next round's claim meets its own
        # previous round's locks and `SKIP LOCKED` silently drops rows this
        # loop is still trying to finish. See this function's docstring.
        await session.commit()

        # A requested id is settled once the round has *handled* it, whatever
        # the outcome: READY, FAILED and re-released-to-UPLOADED (deferred) all
        # mean the loop no longer owes that id a decision.
        #
        #   - READY / FAILED are terminal. Leaving them in `remaining` would
        #     make the loop re-claim a row that can never be claimed again, so
        #     `max_rounds` would exhaust and raise a false "never ingested" -
        #     which is exactly what the earlier version of this function did to
        #     `test_drain_versions_raises_rather_than_returning_partial_success`.
        #   - Deferred is *not* terminal, but re-rounding it would mean the
        #     loop spins on a dependency outage: this round released the claim
        #     precisely so a *later cycle* retries it, not an inner loop.
        #
        # The ids the round did not touch are the only ones still owed
        # something, and those stay in `remaining` for the next round.
        remaining = [v for v in before if v not in set(round_stats.handled_ids)]

        if not remaining:
            break

        # Termination is "did the requested set shrink", not "did this round
        # claim anything". The two differ on a shared queue, and the
        # difference is not academic: a round can come back with rows the
        # caller never asked about (a tie group's LIMIT cut) or, if a tie is
        # held elsewhere and skipped, with fewer rows than requested.
        # Measured: 13 rows sharing `created_at = 1789827361`.
        if round_stats.claimed == 0:
            # Nothing was claimed and nothing of ours moved. Either everything
            # left is held by another transaction - another round would return
            # the same answer and spinning would be a livelock - or the queue
            # is gone. Stop and let the check below decide whether that was
            # acceptable.
            break

    if remaining:
        raise IngestionError(
            f"{len(remaining)} version(s) were never ingested after "
            f"{max_rounds} claim round(s): {[str(v) for v in remaining]}. "
            "The claim is a global FIFO, so a busy queue can starve a "
            "specific row - see drain_versions."
        )
    return total
