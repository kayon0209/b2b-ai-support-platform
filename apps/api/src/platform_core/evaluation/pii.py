"""PII handling and retention controls (ticket 36, docs/security.md).

Field classification + redaction pipeline applied before model and log
boundaries, and a retention sweeper that expires superseded document
versions and prunes expired data by tenant policy.
"""

import re
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession


class Sensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


# Field-name -> classification. Unlisted fields default to INTERNAL and are
# redacted to CONFIDENTIAL classes when bound for external systems.
FIELD_CLASSIFICATION: dict[str, Sensitivity] = {
    "email": Sensitivity.RESTRICTED,
    "phone": Sensitivity.RESTRICTED,
    "ssn": Sensitivity.RESTRICTED,
    "credit_card": Sensitivity.RESTRICTED,
    "api_key": Sensitivity.RESTRICTED,
    "token": Sensitivity.RESTRICTED,
    "password": Sensitivity.RESTRICTED,
    "secret": Sensitivity.RESTRICTED,
    "authorization": Sensitivity.RESTRICTED,
    "customer_name": Sensitivity.CONFIDENTIAL,
    "address": Sensitivity.CONFIDENTIAL,
    "contract_value": Sensitivity.CONFIDENTIAL,
}


@dataclass
class RedactionReport:
    redacted_fields: list[str]
    redaction_count: int


# Regex-based value redaction for untyped content (message bodies etc.)
_VALUE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"), "[EMAIL]"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[CARD]"),
    (re.compile(r"\b\+?\d[\d\s-]{7,14}\d\b"), "[PHONE]"),
]


def redact_text(text: str) -> tuple[str, int]:
    """Redact PII-shaped values in free text. Returns (text, count)."""
    count = 0
    for pattern, replacement in _VALUE_PATTERNS:
        text, n = pattern.subn(replacement, text)
        count += n
    return text, count


def classify_field(name: str) -> Sensitivity:
    return FIELD_CLASSIFICATION.get(name.lower(), Sensitivity.INTERNAL)


def minimize_payload(
    payload: dict[str, Any],
    *,
    destination: str,
) -> tuple[dict[str, Any], RedactionReport]:
    """Prepare a payload for a boundary crossing.

    - destination "model": RESTRICTED fields dropped, CONFIDENTIAL redacted
      by value pattern, everything else passes.
    - destination "log": CONFIDENTIAL and RESTRICTED dropped entirely.
    Returns the minimized copy plus a report (auditable, no silent loss).
    """
    out: dict[str, Any] = {}
    redacted: list[str] = []
    count = 0
    for key, value in payload.items():
        level = classify_field(key)
        if destination == "log" and level in (Sensitivity.RESTRICTED, Sensitivity.CONFIDENTIAL):
            redacted.append(key)
            count += 1
            continue
        if destination == "model":
            if level == Sensitivity.RESTRICTED:
                redacted.append(key)
                count += 1
                continue
            if isinstance(value, str) and level == Sensitivity.CONFIDENTIAL:
                value, n = redact_text(value)
                count += n
                redacted.append(key)
        if isinstance(value, dict):
            value, sub_report = minimize_payload(value, destination=destination)
            count += sub_report.redaction_count
            redacted.extend(f"{key}.{f}" for f in sub_report.redacted_fields)
        out[key] = value
    return out, RedactionReport(redacted_fields=redacted, redaction_count=count)


# --- Retention (docs/deployment-and-operations.md backup/recovery) ---

# `DocumentVersion.status` and `DocumentVersion.ingestion_status` are two
# different vocabularies that happen to share two strings. `status` is
# draft/processing/active/superseded/expired/failed and is what retrieval filters
# on (`dv.status = 'active'`); `ingestion_status` is the `IngestionStatus` enum,
# which has no `active` member at all. So the retention sweep's use of
# `IngestionStatus.EXPIRED.value` reads as the authority for that column and is
# only correct because the two happen to spell 'expired' the same way.
#
# Named here so a future edit to either vocabulary does not silently move
# retention's meaning. The value is the contract, not the enum member.
VERSION_EXPIRED = "expired"


@dataclass(frozen=True)
class RetentionPolicy:
    # Superseded document versions kept for rollback for N days.
    superseded_version_days: int = 30
    # Resolved dead letters pruned after N days.
    dead_letter_days: int = 14
    # Inbox events archived after N days.
    inbox_event_days: int = 90
    # Redacted conversation turns pruned after N days (iteration plan 2.1):
    # Chatwoot is the system of record for raw content, so the local memory
    # copy is a cache and must not outlive its usefulness.
    conversation_turn_days: int = 90


DEFAULT_RETENTION = RetentionPolicy()


async def sweep_expired_data(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    now: int,
    policy: RetentionPolicy = DEFAULT_RETENTION,
) -> dict[str, int]:
    """Expire/prune rows per retention policy. RLS context must be set.

    Returns per-table row counts for the audit record. Deletion here is
    policy-driven data lifecycle, not ad-hoc removal.
    """
    from sqlalchemy import delete, update
    from sqlalchemy.engine import CursorResult

    def _affected(result: object) -> int:
        """DML row count.

        `AsyncSession.execute` is typed as returning `Result[Any]`, which
        does not expose `rowcount` - but every DML statement actually
        returns a `CursorResult` that does.
        """
        return int(cast(CursorResult[Any], result).rowcount or 0)

    from platform_core.integrations.models import DeadLetterItem
    from platform_core.knowledge.models import DocumentVersion, IngestionStatus
    from platform_core.support_bridge.models import InboxEvent, InboxEventStatus

    counts: dict[str, int] = {}

    # 1) SUPERSEDED versions past retention -> EXPIRED status (retrieval stops)
    #
    # The age test is `expires_at`, not a creation timestamp: DocumentVersion
    # has no `created_at` column, and `expires_at` is the field retrieval
    # itself gates on (retrieval/hybrid.py). Using a non-existent column here
    # would have raised UndefinedColumn on every sweep.
    cutoff = now - policy.superseded_version_days * 86400
    result = await session.execute(
        update(DocumentVersion)
        .where(
            DocumentVersion.tenant_id == tenant_id,
            DocumentVersion.status == IngestionStatus.SUPERSEDED.value,
            DocumentVersion.expires_at.is_not(None),
            DocumentVersion.expires_at < cutoff,
        )
        .values(status=IngestionStatus.EXPIRED.value)
    )
    counts["document_versions_expired"] = _affected(result)

    # 2) resolved dead letters past retention -> delete
    dl_cutoff = now - policy.dead_letter_days * 86400
    result = await session.execute(
        delete(DeadLetterItem).where(
            DeadLetterItem.tenant_id == tenant_id,
            DeadLetterItem.status == "resolved",
            DeadLetterItem.resolved_at.is_not(None),
            DeadLetterItem.resolved_at < dl_cutoff,
        )
    )
    counts["dead_letters_pruned"] = _affected(result)

    # 3) completed inbox events past retention -> delete (payload already minimized)
    ie_cutoff = now - policy.inbox_event_days * 86400
    result = await session.execute(
        delete(InboxEvent).where(
            InboxEvent.tenant_id == tenant_id,
            InboxEvent.status == InboxEventStatus.COMPLETED.value,
            InboxEvent.received_at < ie_cutoff,
        )
    )
    counts["inbox_events_pruned"] = _affected(result)

    # 4) conversation turns past retention -> delete (redacted cache, plan 2.1)
    turn_cutoff = now - policy.conversation_turn_days * 86400
    from platform_core.agent_runtime.models import ConversationTurn

    result = await session.execute(
        delete(ConversationTurn).where(
            ConversationTurn.tenant_id == tenant_id,
            ConversationTurn.ts < turn_cutoff,
        )
    )
    counts["conversation_turns_pruned"] = _affected(result)
    return counts


# --- Object erasure and reconciliation (iteration plan stage 4, T4.2) ---


async def erase_expired_objects(
    session: AsyncSession,
    storage: Any,
    *,
    tenant_id: uuid.UUID,
    now: int,
    limit: int = 500,
) -> dict[str, int]:
    """Delete the bytes behind every EXPIRED version whose erasure is unproven.

    Why this exists as a second pass rather than inline in the sweep
    ---------------------------------------------------------------
    The sweep sets `status = 'expired'`. That is a decision, and until now it
    was the whole of what "retention" did - the uploaded document stayed in the
    bucket forever, readable by anyone holding its key. Erasing inside the same
    UPDATE would be tidier, and it would also silently skip every row whose
    delete failed: there would be no record that an erasure was owed, because
    the row already looked finished.

    Splitting them means the row is the work queue. Rows with
    `bytes_deleted_at IS NULL` are picked up again next cycle, so a worker that
    dies halfway, or a bucket that is briefly unreachable, repairs itself on
    the next run instead of leaking quietly and permanently.

    Order: bytes first, then the stamp
    ----------------------------------
    The stamp goes on only after the endpoint confirms the object is absent. If
    the order were reversed, a crash between them would record an erasure that
    never happened - which is worse than having no record, because it converts
    an open problem into a closed one.

    `limit` bounds the batch. A tenant that accumulated a very large backlog
    would otherwise hold one transaction open across thousands of HTTP calls.
    Leftover rows are simply picked up by the next cycle.
    """
    import asyncio

    from sqlalchemy import select, update

    from platform_core.knowledge.models import DocumentVersion

    rows = (
        await session.execute(
            select(DocumentVersion.id, DocumentVersion.object_uri)
            .where(
                DocumentVersion.tenant_id == tenant_id,
                DocumentVersion.status == VERSION_EXPIRED,
                DocumentVersion.bytes_deleted_at.is_(None),
            )
            .limit(limit)
        )
    ).all()

    counts = {"objects_erased": 0, "objects_already_absent": 0, "objects_failed": 0}
    if not rows:
        return counts

    # A versioned bucket cannot be erased through DELETE.
    #
    # Measured against MinIO rather than assumed: after deleting an object from
    # a versioned bucket, the current version 404s - which is exactly what
    # `object_exists` reports - while every earlier version still returns its
    # full content when fetched by `version_id`. So the stamp below would
    # record a completed erasure of bytes that remain completely readable, and
    # an auditor reading `bytes_deleted_at` would be told the opposite of the
    # truth.
    #
    # Not raised: the exception would propagate out of the per-tenant loop and
    # stop every other tenant's retention, which is a much larger blast radius
    # than the one misconfigured bucket. Counting them as failed keeps the rows
    # unstamped (so the backlog stays visible and the alert keeps firing)
    # while the dead-letter and inbox pruning still completes.
    try:
        versioned = await asyncio.to_thread(storage.bucket_versioning_enabled)
    except Exception:  # noqa: BLE001 - cannot establish the precondition
        versioned = True
    if versioned:
        return {"objects_erased": 0, "objects_already_absent": 0, "objects_failed": len(rows)}

    for version_id, raw_uri in rows:
        key = str(raw_uri or "")
        if not key:
            # Nothing was ever stored - the upload failed before reaching the
            # bucket. Stamped rather than skipped: leaving it NULL would make
            # every future pass re-select it, and with a bound batch that
            # starves rows that do have bytes waiting.
            await session.execute(
                update(DocumentVersion)
                .where(DocumentVersion.id == version_id)
                .values(bytes_deleted_at=now)
            )
            counts["objects_already_absent"] += 1
            continue

        try:
            # The storage client is synchronous httpx. Calling it directly from
            # this coroutine would block the worker's event loop for the whole
            # round trip, per object.
            removed = await asyncio.to_thread(storage.delete_object, key)
        except Exception:  # noqa: BLE001 - a storage outage must not fail the batch
            # Deliberately left unstamped: this row still owes an erasure, and
            # NULL is exactly what the next pass selects on.
            counts["objects_failed"] += 1
            continue

        await session.execute(
            update(DocumentVersion)
            .where(DocumentVersion.id == version_id)
            .values(bytes_deleted_at=now)
        )
        if removed:
            counts["objects_erased"] += 1
        else:
            # Already gone. Still stamped, so we stop looking for it.
            counts["objects_already_absent"] += 1

    return counts


async def reconcile_objects(
    session: AsyncSession,
    storage: Any,
    *,
    tenant_id: uuid.UUID,
) -> dict[str, int]:
    """Compare the bucket against the table, in both directions.

    Two kinds of orphan, and they are not symmetric:

    - **Bytes with no row.** An upload wrote to storage and then failed before
      the row committed. Nothing can ever find these again - no status, no
      sweep, no query - except a walk of the prefix. These are deleted, because
      leaving them means keeping data nobody can account for.

    - **A row whose bytes are missing.** Not deleted. Removing that row would
      stop the report firing, which is convenient, and it would also destroy
      the only record that we owe the tenant an erasure we cannot prove. So it
      is counted and left for an operator. This is the whole point of the
      distinction: one is garbage, the other is evidence.

    The walk is prefixed by the tenant id, so a reconciliation run reads one
    tenant's namespace. Enumerating the bucket wholesale would make this job
    the one place in the platform that touches every tenant's objects at once.
    """
    import asyncio

    from sqlalchemy import select

    from platform_core.knowledge.models import DocumentVersion

    prefix = f"{tenant_id}/"
    stored = await asyncio.to_thread(storage.list_objects, prefix)

    rows = (
        await session.execute(
            select(DocumentVersion.id, DocumentVersion.object_uri).where(
                DocumentVersion.tenant_id == tenant_id,
                DocumentVersion.bytes_deleted_at.is_(None),
            )
        )
    ).all()

    known = {str(uri or "") for _id, uri in rows}
    counts = {
        "rows_missing_object": 0,
        "orphan_objects_removed": 0,
        "orphan_objects_failed": 0,
    }

    # A key with no row is genuinely unreferenced. Note this compares against
    # every row of this tenant, not just expired ones: reconciling only the
    # expired set would call a live document an orphan.
    for key in stored:
        if key in known:
            continue
        try:
            await asyncio.to_thread(storage.delete_object, key)
        except Exception:  # noqa: BLE001 - one failure must not abandon the rest
            counts["orphan_objects_failed"] += 1
            continue
        counts["orphan_objects_removed"] += 1

    # The other direction. Rows keyed to an object that is not in the bucket and
    # never claimed erased are reported, not repaired.
    for _id, uri in rows:
        key = str(uri or "")
        if key and key not in stored:
            counts["rows_missing_object"] += 1

    return counts
