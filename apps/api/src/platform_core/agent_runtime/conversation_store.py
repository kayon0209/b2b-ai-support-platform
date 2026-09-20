"""Redacted conversation turns: the memory store (iteration plan 2.1).

The raw message lives only in Chatwoot. This store keeps what memory needs
to work - the redacted words, in order - plus an integrity hash over the
original bytes, and nothing else. Every write goes through
`evaluation.pii.redact_text` before it reaches the row, which is the
enforcement point for "customer PII does not gain a second copy at rest".
"""

import hashlib
import time
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.agent_runtime.conversation import Turn, TurnRole
from platform_core.agent_runtime.models import ContactFact, ConversationTurn
from platform_core.evaluation.pii import redact_text


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
    """Write durable facts; current statements override history (plan 2.5).

    `ON CONFLICT DO UPDATE` is the whole conflict policy: the customer
    changing their answer IS the update. Values are redacted at extraction
    time already (`extract_durable_facts` refuses PII-shaped turns); a final
    redact here is defence in depth on the write boundary.
    """
    if not facts:
        return 0
    ts = now if now is not None else int(time.time())
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
                confidence=100,
                updated_at=ts,
            )
            .on_conflict_do_update(
                constraint="uq_contact_fact_key",
                set_={
                    "value": redacted_value[:255],
                    "source_turn_id": source_turn_id,
                    "updated_at": ts,
                },
            )
        )
        await session.execute(stmt)
        written += 1
    return written


async def load_facts(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    contact_ref: uuid.UUID,
) -> list[tuple[str, str]]:
    """Current facts for a contact, newest-statement wins by construction."""

    rows = (
        (
            await session.execute(
                select(ContactFact)
                .where(
                    ContactFact.tenant_id == tenant_id,
                    ContactFact.contact_ref == contact_ref,
                )
                .order_by(ContactFact.key)
            )
        )
        .scalars()
        .all()
    )
    return [(row.key, row.value) for row in rows]


def contact_ref_from_external(tenant_id: uuid.UUID, external_contact_id: str) -> uuid.UUID:
    """Stable per-tenant ref for a Chatwoot contact, same derivation as the
    conversation ref: no schema change needed to map external ids."""
    return uuid.uuid5(tenant_id, f"chatwoot:contact:{external_contact_id}")


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
