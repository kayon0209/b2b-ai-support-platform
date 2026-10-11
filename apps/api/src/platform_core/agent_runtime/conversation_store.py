"""Redacted conversation turns: the memory store (iteration plan 2.1).

The source channel remains the system of record for its raw message. This
store keeps what memory needs - the redacted words, in order - plus an
integrity hash over the original bytes, and nothing else. Every write goes through
`evaluation.pii.redact_text` before it reaches the row, which is the
enforcement point for "customer PII does not gain a second copy at rest".
"""

import hashlib
import re
import time
import uuid
from typing import Any, cast

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.agent_runtime.conversation import DURABLE_FACT_KEYS, Turn, TurnRole
from platform_core.agent_runtime.models import ContactFact, ConversationTurn
from platform_core.evaluation.pii import DEFAULT_RETENTION, redact_text


def _role_value(role: TurnRole | str) -> str:
    if isinstance(role, TurnRole):
        return role.value
    return TurnRole(role).value


async def append_turn(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_ref_id: uuid.UUID,
    turn: Turn,
    source: str = "platform",
) -> uuid.UUID:
    """Persist one turn, redacted. Returns the row id (used as fact source).

    The redaction count is deliberately not reported upward: a turn that
    needed redaction is still stored redacted, and the count belongs to the
    audit log of the send path, not to memory.
    """
    redacted, _count = redact_text(turn.text)
    row_id = uuid.uuid4()
    session.add(
        ConversationTurn(
            tenant_id=tenant_id,
            id=row_id,
            conversation_ref_id=conversation_ref_id,
            role=_role_value(turn.role),
            text_redacted=redacted,
            text_hash=hashlib.sha256(turn.text.encode()).hexdigest(),
            ts=turn.ts or int(time.time()),
            ref=turn.ref or "",
            source=source,
            created_at=int(time.time()),
        )
    )
    return row_id


async def append_authored_turn(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_ref_id: uuid.UUID,
    text: str,
    role: TurnRole | str,
    source: str,
    ts: int | None = None,
    origin: str = "",
    canned_reply_id: uuid.UUID | None = None,
    copilot_job_id: uuid.UUID | None = None,
    source_refs: list[dict[str, object]] | None = None,
    author_ref: str | None = None,
    turn_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Persist a turn the **platform authored**, verbatim. Returns the row id.

    Not redacted, and that is the whole reason this is a separate function
    rather than a flag on `append_turn`. Redaction exists to keep customer PII
    out of storage; it is not a property of the *column*, and applying it here
    would corrupt the content rather than protect anyone:

    - `redact_text` masks any 10+ digit run, so an agent answering "your order
      SO-9001 ships on 20260930" would have the number replaced with a marker -
      the one thing the message existed to convey.
    - It would also make the stored copy differ from what the customer
      received, which breaks the question this table is read to answer: *what
      did we actually tell them*.

    The caller is a human's own words, already attributed to that human in the
    audit trail. A separate name rather than a `redact=False` argument because a
    defaulted boolean can be flipped at a call site by someone who has not read
    the reason, and this is not a behaviour anyone should change by accident.
    """
    row_id = turn_id or uuid.uuid4()
    session.add(
        ConversationTurn(
            tenant_id=tenant_id,
            id=row_id,
            conversation_ref_id=conversation_ref_id,
            role=_role_value(role),
            text_redacted=text,
            text_hash=hashlib.sha256(text.encode()).hexdigest(),
            ts=ts or int(time.time()),
            ref="",
            source=source,
            created_at=int(time.time()),
            origin=origin,
            canned_reply_id=canned_reply_id,
            copilot_job_id=copilot_job_id,
            source_refs=source_refs or [],
            author_ref=author_ref,
        )
    )
    return row_id


async def load_turns(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_ref_id: uuid.UUID,
    limit: int,
) -> list[Turn]:
    """The latest `limit` turns, oldest first, as memory Turns.

    Text comes off the row already redacted; memory never sees raw content
    because none was stored.
    """
    rows = (
        (
            await session.execute(
                select(ConversationTurn)
                .where(
                    ConversationTurn.tenant_id == tenant_id,
                    ConversationTurn.conversation_ref_id == conversation_ref_id,
                )
                .order_by(ConversationTurn.ts.desc(), ConversationTurn.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    rows = list(reversed(rows))
    return [
        Turn(
            role=TurnRole(row.role),
            text=row.text_redacted,
            ts=int(row.ts or 0),
            ref=row.ref or "",
        )
        for row in rows
    ]


async def upsert_facts(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    contact_ref: uuid.UUID,
    facts: list[tuple[str, str]],
    source_turn_id: uuid.UUID | None = None,
    now: int | None = None,
) -> int:
    """Write facts from a customer turn, rejecting late-arriving history.

    Source order comes from the persisted turn, never from when a worker
    happened to process it. The database trigger repeats this check so an
    older application process in a rolling deployment cannot bypass it.
    Values are redacted at extraction time already; a final redact here is
    defence in depth on the write boundary.
    """
    if not facts or source_turn_id is None:
        return 0
    if any(key not in DURABLE_FACT_KEYS for key, _value in facts):
        raise ValueError("unsupported durable fact type")
    ts = now if now is not None else int(time.time())
    source = (
        await session.execute(
            select(ConversationTurn.ts).where(
                ConversationTurn.tenant_id == tenant_id,
                ConversationTurn.id == source_turn_id,
                ConversationTurn.role == TurnRole.CUSTOMER.value,
            )
        )
    ).scalar_one_or_none()
    if source is None:
        # Missing, cross-tenant, or non-customer provenance is not durable
        # memory. This is a fail-closed no-op, not a fallback to worker time.
        return 0
    source_ts = int(source)
    expires_at = source_ts + DEFAULT_RETENTION.contact_fact_days * 86400
    written = 0
    for key, value in facts:
        redacted_value, _count = redact_text(value)
        stmt = (
            pg_insert(ContactFact)
            .values(
                tenant_id=tenant_id,
                contact_ref=contact_ref,
                key=key[:63],
                value=redacted_value[:255],
                source_turn_id=source_turn_id,
                source_ts=source_ts,
                revision=1,
                confidence=100,
                updated_at=ts,
                expires_at=expires_at,
            )
            .on_conflict_do_update(
                constraint="uq_contact_fact_key",
                set_={
                    "value": redacted_value[:255],
                    "source_turn_id": source_turn_id,
                    "source_ts": source_ts,
                    "revision": ContactFact.revision + 1,
                    "updated_at": ts,
                    "expires_at": expires_at,
                },
                where=or_(
                    ContactFact.source_turn_id.is_(None),
                    ContactFact.source_ts < source_ts,
                    and_(
                        ContactFact.source_ts == source_ts,
                        ContactFact.source_turn_id < source_turn_id,
                    ),
                ),
            )
        )
        result = await session.execute(stmt.returning(ContactFact.id))
        written += int(result.scalar_one_or_none() is not None)
    return written


async def load_facts(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    contact_ref: uuid.UUID,
    now: int | None = None,
) -> list[tuple[str, str]]:
    """Unexpired facts for one tenant- and channel-scoped contact."""

    current_time = now if now is not None else int(time.time())
    rows = (
        (
            await session.execute(
                select(ContactFact)
                .where(
                    ContactFact.tenant_id == tenant_id,
                    ContactFact.contact_ref == contact_ref,
                    ContactFact.expires_at.is_not(None),
                    ContactFact.expires_at > current_time,
                )
                .order_by(ContactFact.key)
            )
        )
        .scalars()
        .all()
    )
    return [(row.key, row.value) for row in rows]


async def erase_memory_rows(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    contact_ref: uuid.UUID,
    conversation_ref_ids: list[uuid.UUID],
) -> dict[str, int]:
    """Erase stored conversation memory; operational tasks and audit stay put.

    Summaries are derived on each run and are not stored. Removing turns also
    removes the source from which any later summary could be rebuilt.
    """
    facts_result = await session.execute(
        delete(ContactFact).where(
            ContactFact.tenant_id == tenant_id,
            ContactFact.contact_ref == contact_ref,
        )
    )
    turns_deleted = 0
    if conversation_ref_ids:
        turns_result = await session.execute(
            delete(ConversationTurn).where(
                ConversationTurn.tenant_id == tenant_id,
                ConversationTurn.conversation_ref_id.in_(conversation_ref_ids),
            )
        )
        turns_deleted = int(cast(CursorResult[Any], turns_result).rowcount or 0)
    return {
        "contact_facts_deleted": int(cast(CursorResult[Any], facts_result).rowcount or 0),
        "conversation_turns_deleted": turns_deleted,
    }


def contact_ref_from_external(
    tenant_id: uuid.UUID,
    external_contact_id: str,
    *,
    channel: str = "chatwoot",
) -> uuid.UUID:
    """Stable per-tenant and per-channel ref for an external contact.

    Chatwoot keeps its frozen legacy namespace. Other channels get distinct
    refs so equal provider-local identifiers cannot merge unrelated people.
    """
    normalized_channel = channel.strip().casefold()
    if not normalized_channel or not re.fullmatch(r"[a-z0-9_-]{1,31}", normalized_channel):
        raise ValueError("channel must be a normalized provider identifier")
    if not external_contact_id:
        raise ValueError("external_contact_id is required")
    namespace = (
        "chatwoot:contact" if normalized_channel == "chatwoot" else f"{normalized_channel}:contact"
    )
    return uuid.uuid5(tenant_id, f"{namespace}:{external_contact_id}")


def fact_tuples(facts: object) -> list[tuple[str, str]]:
    """DurableFact objects -> (key, value) pairs for the store."""
    return [(fact.key, fact.value) for fact in facts]  # type: ignore[attr-defined]


async def latest_suggestion(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_ref_id: uuid.UUID,
) -> tuple[str, list[str]] | None:
    """The last thing the AI said here, with the sources it cited.

    The agent workbench's reason to exist: a human picking up a handoff should
    read the proposed answer and its grounding instead of reconstructing both
    from the audit log. Returns None when the AI never produced anything -
    which is normal for a conversation opened straight into a human queue.

    Exposed as a function rather than as models because AGENTS.md forbids one
    module importing another's ORM classes: `cases` calls this and receives
    strings.

    The text comes from the stored turn, not from `AgentRun`: a run records
    only `output_hash`, deliberately, so the body lives with the turns.
    """
    from platform_core.agent_runtime.models import AgentRun, Citation

    turn = (
        await session.execute(
            select(ConversationTurn.text_redacted)
            .where(
                ConversationTurn.tenant_id == tenant_id,
                ConversationTurn.conversation_ref_id == conversation_ref_id,
                ConversationTurn.role == _role_value(TurnRole.AGENT),
                ConversationTurn.source != "agent",
            )
            .order_by(ConversationTurn.ts.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    run_id = (
        await session.execute(
            select(AgentRun.id)
            .where(
                AgentRun.tenant_id == tenant_id,
                AgentRun.conversation_ref_id == conversation_ref_id,
            )
            .order_by(AgentRun.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    sources: list[str] = []
    if run_id is not None:
        rows = (
            await session.execute(
                select(Citation.source_uri)
                .where(Citation.tenant_id == tenant_id, Citation.agent_run_id == run_id)
                .order_by(Citation.id)
            )
        ).scalars()
        sources = [str(row) for row in rows]

    if turn is None and not sources:
        return None
    return (turn or "", sources)
