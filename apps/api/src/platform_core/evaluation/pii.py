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


@dataclass(frozen=True)
class RetentionPolicy:
    # Superseded document versions kept for rollback for N days.
    superseded_version_days: int = 30
    # Resolved dead letters pruned after N days.
    dead_letter_days: int = 14
    # Inbox events archived after N days.
    inbox_event_days: int = 90


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
    return counts
