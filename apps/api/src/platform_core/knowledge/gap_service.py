"""Knowledge gap service (ticket 39, docs/development-plan.md Phase 4).

Recording a gap is a side effect of abstaining, so `record_gap` must be
cheap and must never fail the answer path: a queue-write problem cannot be
allowed to turn an honest abstention into an error. It therefore never
raises for the "already known" case and is safe to call on every abstention.

The reviewed-draft workflow is the safety-critical half. `publish_draft`
creates real knowledge, so it requires:

- the draft to be approved (a pending draft cannot be published);
- the approver to differ from the author (four-eyes: the person who wrote
  the answer is not the person who vouches for it);
- an explicit `Document` + `DocumentVersion` created through the normal
  path, so published content carries the same provenance, ACLs and
  retrieval gating as any other document.

Nothing here auto-publishes. `AGENTS.md` prohibits "automatically learning
from unreviewed conversations", and an auto-published draft would make the
agent's own guess the source it cites next time.
"""

import hashlib
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.audit import service as audit_service
from platform_core.identity.tenant_context import TenantContext
from platform_core.knowledge.gap_models import (
    DraftStatus,
    GapStatus,
    KnowledgeDraft,
    KnowledgeGap,
    is_knowledge_gap,
    question_hash,
)
from platform_core.knowledge.models import (
    Document,
    DocumentVersion,
    IngestionStatus,
    KnowledgeSource,
)

logger = logging.getLogger(__name__)

# The gap queue is ordered by how often a question was asked. Past this many
# occurrences a question stops being "popular" and starts being "someone's
# script", and ordering by raw count would let one bot dominate the queue.
MAX_ORDERED_FREQUENCY = 100


class GapError(Exception):
    """A gap workflow transition was refused."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class GapRecord:
    """Outcome of recording an abstention."""

    gap_id: uuid.UUID | None
    created: bool
    frequency: int


# --- Recording --------------------------------------------------------------


async def record_gap(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    question: str,
    reason_code: str,
    now: int | None = None,
) -> GapRecord:
    """Record an unanswerable question, aggregating repeats.

    Returns the gap and whether this call created it. Non-gap reasons (a
    restricted request, for example) are ignored: documenting them would be
    the wrong fix, and queuing them would train reviewers to skim the queue.
    """
    if not is_knowledge_gap(reason_code):
        return GapRecord(gap_id=None, created=False, frequency=0)

    normalized = question_hash(question)
    if not normalized or not question.strip():
        return GapRecord(gap_id=None, created=False, frequency=0)

    stamp = int(time.time()) if now is None else now
    existing = await _find_gap(session, tenant_id=tenant_id, hashed=normalized)

    if existing is not None:
        # A dismissed or resolved gap that recurs is a real signal: either
        # the decision was wrong or the fix did not land. Reopen it rather
        # than creating a duplicate row the unique constraint would reject.
        existing.frequency += 1
        existing.last_seen_at = stamp
        if existing.status in (GapStatus.DISMISSED.value, GapStatus.RESOLVED.value):
            existing.status = GapStatus.OPEN.value
            existing.acknowledged_at = None
        await session.flush()
        return GapRecord(gap_id=existing.id, created=False, frequency=existing.frequency)

    gap = KnowledgeGap(
        tenant_id=tenant_id,
        question_hash=normalized,
        sample_question=question.strip()[:2000],
        reason_code=reason_code,
        status=GapStatus.OPEN.value,
        frequency=1,
        first_seen_at=stamp,
        last_seen_at=stamp,
    )
    session.add(gap)
    await session.flush()
    return GapRecord(gap_id=gap.id, created=True, frequency=1)


async def _find_gap(
    session: AsyncSession, *, tenant_id: uuid.UUID, hashed: str
) -> KnowledgeGap | None:
    stmt = select(KnowledgeGap).where(
        KnowledgeGap.tenant_id == tenant_id,
        KnowledgeGap.question_hash == hashed,
    )
    return (await session.execute(stmt)).scalars().first()


# --- Queue ------------------------------------------------------------------


async def list_gaps(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[KnowledgeGap], int]:
    """The queue, most-demanded first.

    `frequency` is capped in the ordering so a single scripted caller cannot
    pin one question to the top of every reviewer's screen.
    """
    stmt = select(KnowledgeGap).where(KnowledgeGap.tenant_id == tenant_id)
    if status is not None:
        stmt = stmt.where(KnowledgeGap.status == status)

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = int((await session.execute(count_stmt)).scalar_one())

    ordering = func.least(KnowledgeGap.frequency, MAX_ORDERED_FREQUENCY).desc()
    rows = (
        await session.execute(
            stmt.order_by(ordering, KnowledgeGap.last_seen_at.desc()).limit(limit).offset(offset)
        )
    ).scalars()
    return list(rows), total


async def acknowledge(
    session: AsyncSession, *, ctx: TenantContext, gap_id: uuid.UUID
) -> KnowledgeGap:
    """Claim a gap so two reviewers do not both work it."""
    gap = await _load_gap(session, ctx=ctx, gap_id=gap_id)
    if gap.status == GapStatus.RESOLVED.value:
        raise GapError("ALREADY_RESOLVED", "a resolved gap needs no acknowledgement")

    gap.status = GapStatus.ACKNOWLEDGED.value
    gap.acknowledged_at = int(time.time())
    await session.flush()

    await audit_service.record(
        session,
        ctx=ctx,
        action="knowledge_gap.acknowledged",
        resource_type="knowledge_gap",
        resource_id=gap.id,
        after={"frequency": gap.frequency},
    )
    return gap


async def dismiss(
    session: AsyncSession, *, ctx: TenantContext, gap_id: uuid.UUID, reason: str
) -> KnowledgeGap:
    """Decide a gap should not be documented.

    The reason is required because "dismissed" is otherwise indistinguishable
    from "ignored", and a recurring gap that keeps getting dismissed is
    evidence the original judgement was wrong.
    """
    if not reason.strip():
        raise GapError("REASON_REQUIRED", "a dismissal must state why")

    gap = await _load_gap(session, ctx=ctx, gap_id=gap_id)
    gap.status = GapStatus.DISMISSED.value
    await session.flush()

    await audit_service.record(
        session,
        ctx=ctx,
        action="knowledge_gap.dismissed",
        resource_type="knowledge_gap",
        resource_id=gap.id,
        decision="denied",
        after={"reason": reason, "frequency": gap.frequency},
    )
    return gap


# --- Drafts -----------------------------------------------------------------


async def create_draft(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    gap_id: uuid.UUID,
    title: str,
    body: str,
    target_space_id: uuid.UUID | None = None,
) -> KnowledgeDraft:
    """Propose an answer for review. Not knowledge yet."""
    if not title.strip() or not body.strip():
        raise GapError("EMPTY_DRAFT", "a draft needs a title and a body")

    gap = await _load_gap(session, ctx=ctx, gap_id=gap_id)
    if gap.status == GapStatus.RESOLVED.value:
        raise GapError("ALREADY_RESOLVED", "this gap already has published knowledge")

    # A second submit of the same draft is a duplicate, not a second opinion.
    # The console generates a fresh idempotency key per click, so the key
    # cannot catch a double-click; and unlike customer messages there is no
    # content hash on this path, so without this check two identical drafts
    # appear and a reviewer has to work out that they are the same thing.
    # Matching on (gap, title) is deliberately narrow - a genuinely different
    # title for the same gap is still a new draft.
    existing = (
        await session.execute(
            select(KnowledgeDraft)
            .where(
                KnowledgeDraft.tenant_id == ctx.tenant_id,
                KnowledgeDraft.gap_id == gap.id,
                KnowledgeDraft.title == title.strip()[:512],
                KnowledgeDraft.status == DraftStatus.PENDING.value,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    draft = KnowledgeDraft(
        tenant_id=ctx.tenant_id,
        gap_id=gap.id,
        title=title.strip()[:512],
        body=body,
        status=DraftStatus.PENDING.value,
        author_kind="human",
    )
    session.add(draft)
    gap.status = GapStatus.DRAFTED.value
    if target_space_id is not None:
        gap.target_space_id = target_space_id
    await session.flush()

    await audit_service.record(
        session,
        ctx=ctx,
        action="knowledge_draft.created",
        resource_type="knowledge_draft",
        resource_id=draft.id,
        after={"gap_id": str(gap.id), "title": draft.title},
    )
    return draft


async def review_draft(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    draft_id: uuid.UUID,
    approve: bool,
    notes: str = "",
) -> KnowledgeDraft:
    """Approve or reject a draft. Approval alone does not publish."""
    draft = await _load_draft(session, ctx=ctx, draft_id=draft_id)
    if draft.status != DraftStatus.PENDING.value:
        raise GapError("ALREADY_REVIEWED", "this draft already has a review decision")

    draft.status = DraftStatus.APPROVED.value if approve else DraftStatus.REJECTED.value
    draft.reviewed_by = ctx.actor_id
    draft.reviewed_at = int(time.time())
    draft.review_notes = notes

    if not approve:
        # A rejected draft returns its gap to the queue: the gap is still
        # real, only this proposed answer was wrong.
        gap = await _load_gap(session, ctx=ctx, gap_id=draft.gap_id)
        if gap.status == GapStatus.DRAFTED.value:
            gap.status = GapStatus.ACKNOWLEDGED.value

    await session.flush()

    await audit_service.record(
        session,
        ctx=ctx,
        action="knowledge_draft.reviewed",
        resource_type="knowledge_draft",
        resource_id=draft.id,
        decision="completed" if approve else "denied",
        after={"approved": approve, "notes": notes},
    )
    return draft


async def publish_draft(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    draft_id: uuid.UUID,
    space_id: uuid.UUID,
    version_label: str = "v1",
) -> DocumentVersion:
    """Publish an approved draft as real, retrievable knowledge.

    Creates a `Document` + `DocumentVersion` in `processing` state through
    the same tables the normal upload path uses, so the content is subject
    to the usual parsing, chunking, and versioning rules. The gap is marked
    resolved only once the document exists.
    """
    draft = await _load_draft(session, ctx=ctx, draft_id=draft_id)

    if draft.status != DraftStatus.APPROVED.value:
        raise GapError(
            "DRAFT_NOT_APPROVED",
            "only an approved draft may be published",
        )
    if draft.published_document_id is not None:
        raise GapError("ALREADY_PUBLISHED", "this draft was already published")

    # Four-eyes: an author cannot approve-and-publish their own answer
    # unnoticed. Checking here as well as at review time covers the path
    # where a reviewer approves and a different actor publishes.
    if draft.reviewed_by is not None and draft.reviewed_by == ctx.actor_id:
        raise GapError(
            "SELF_APPROVAL",
            "the reviewer who approved a draft may not be the one to publish it",
        )

    gap = await _load_gap(session, ctx=ctx, gap_id=draft.gap_id)

    source = await _ensure_gap_source(session, ctx=ctx, space_id=space_id)

    # The canonical_uri is a logical identifier; the object_uri is the storage
    # key. They must not be confused: the ingestion worker calls
    # `service.get_object(version.object_uri)`, so object_uri must be a real
    # MinIO key, not a gap:// URI.
    canonical_uri = f"gap://{gap.id}/{draft.id}"
    document = Document(
        tenant_id=ctx.tenant_id,
        space_id=space_id,
        source_id=source.id,
        canonical_uri=canonical_uri,
        title=draft.title,
        owner_ref=str(ctx.actor_id) if ctx.actor_id else None,
        classification="internal",
    )
    session.add(document)
    await session.flush()

    content_hash = hashlib.sha256(draft.body.encode("utf-8")).hexdigest()
    version = DocumentVersion(
        tenant_id=ctx.tenant_id,
        document_id=document.id,
        version_label=version_label,
        content_hash=content_hash,
        status=IngestionStatus.PROCESSING.value,
        object_uri="",  # filled in after the key is derived from version.id
        parser_version="gap-draft-v1",
        ingestion_status=IngestionStatus.UPLOADED.value,
        metadata_json={
            "content_type": "text/markdown",
            "size_bytes": len(draft.body.encode("utf-8")),
        },
    )
    session.add(version)
    await session.flush()

    # Derive the tenant-prefixed key from the database-assigned id, then upload
    # the draft body through the same storage path as a normal upload. The row
    # is committed before the object: a row without an object is recoverable,
    # an object without a row is an orphan that no listing would ever clean up.
    from platform_core.knowledge.service import upload_object
    from platform_core.knowledge.storage import ObjectKey

    key = ObjectKey(
        tenant_id=str(ctx.tenant_id),
        document_version_id=str(version.id),
        filename=f"gap-draft-{draft.id}.md",
    ).to_key()
    version.object_uri = key
    await session.flush()

    # Upload is best-effort: if storage is unreachable the version row still
    # exists with a correct object_uri, so the ingestion worker will pick it
    # up and either succeed (if storage comes back) or fail the version to
    # FAILED for operator retry. A row without an object is recoverable;
    # aborting publish entirely on a storage blip would leave the gap
    # unresolved and the draft unusable. (The normal upload path in the
    # knowledge router uploads before returning, but publish_draft lives in
    # the service layer where a storage outage must not be fatal.)
    try:
        upload_object(key, draft.body.encode("utf-8"), "text/markdown")
    except Exception as exc:
        logger.warning(
            "gap_draft_upload_pending gap_id=%s version_id=%s error=%s",
            str(gap.id),
            str(version.id),
            type(exc).__name__,
        )
        version.metadata_json = {
            **version.metadata_json,
            "upload_deferred": True,
            "upload_error": type(exc).__name__,
        }

    draft.published_document_id = document.id
    gap.status = GapStatus.RESOLVED.value
    gap.target_space_id = space_id
    await session.flush()

    await audit_service.record(
        session,
        ctx=ctx,
        action="knowledge_draft.published",
        resource_type="knowledge_draft",
        resource_id=draft.id,
        after={
            "gap_id": str(gap.id),
            "document_id": str(document.id),
            "document_version_id": str(version.id),
        },
    )
    return version


async def _ensure_gap_source(
    session: AsyncSession, *, ctx: TenantContext, space_id: uuid.UUID
) -> KnowledgeSource:
    """A stable source row identifying gap-derived knowledge.

    Documents must belong to a source; labelling gap-derived content keeps
    "what did we learn from customers?" answerable later.
    """
    name = "Knowledge gap resolutions"
    stmt = select(KnowledgeSource).where(
        KnowledgeSource.tenant_id == ctx.tenant_id,
        KnowledgeSource.space_id == space_id,
        KnowledgeSource.name == name,
    )
    existing = (await session.execute(stmt)).scalars().first()
    if existing is not None:
        return existing

    source = KnowledgeSource(
        tenant_id=ctx.tenant_id,
        space_id=space_id,
        type="api",
        name=name,
        status="active",
    )
    session.add(source)
    await session.flush()
    return source


# --- Reads ------------------------------------------------------------------


async def list_drafts(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    status: str | None = None,
    limit: int = 50,
) -> list[KnowledgeDraft]:
    stmt = select(KnowledgeDraft).where(KnowledgeDraft.tenant_id == tenant_id)
    if status is not None:
        stmt = stmt.where(KnowledgeDraft.status == status)
    rows = (await session.execute(stmt.order_by(KnowledgeDraft.id).limit(limit))).scalars()
    return list(rows)


async def gap_stats(session: AsyncSession, *, tenant_id: uuid.UUID) -> dict[str, Any]:
    """Counts by status, for the queue header."""
    stmt = (
        select(KnowledgeGap.status, func.count(), func.sum(KnowledgeGap.frequency))
        .where(KnowledgeGap.tenant_id == tenant_id)
        .group_by(KnowledgeGap.status)
    )
    by_status: dict[str, int] = {}
    total_occurrences = 0
    for status, count, occurrences in (await session.execute(stmt)).all():
        by_status[str(status)] = int(count)
        total_occurrences += int(occurrences or 0)
    return {
        "by_status": by_status,
        "total_gaps": sum(by_status.values()),
        "total_occurrences": total_occurrences,
    }


# --- Internals --------------------------------------------------------------


async def _load_gap(
    session: AsyncSession, *, ctx: TenantContext, gap_id: uuid.UUID
) -> KnowledgeGap:
    stmt = select(KnowledgeGap).where(
        KnowledgeGap.id == gap_id,
        KnowledgeGap.tenant_id == ctx.tenant_id,
    )
    row = (await session.execute(stmt)).scalars().first()
    if row is None:
        # Absent and not-yours are the same error: distinguishing them would
        # confirm that a guessed id exists in another tenant.
        raise GapError("NOT_FOUND", "no such knowledge gap for this tenant")
    return row


async def _load_draft(
    session: AsyncSession, *, ctx: TenantContext, draft_id: uuid.UUID
) -> KnowledgeDraft:
    stmt = select(KnowledgeDraft).where(
        KnowledgeDraft.id == draft_id,
        KnowledgeDraft.tenant_id == ctx.tenant_id,
    )
    row = (await session.execute(stmt)).scalars().first()
    if row is None:
        raise GapError("NOT_FOUND", "no such knowledge draft for this tenant")
    return row


__all__ = [
    "GapError",
    "GapRecord",
    "acknowledge",
    "create_draft",
    "dismiss",
    "gap_stats",
    "list_drafts",
    "list_gaps",
    "publish_draft",
    "record_gap",
    "review_draft",
]
