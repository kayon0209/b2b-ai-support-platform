"""Feature list 1.5: continuing a conversation on another channel or device.

What was missing, and why it is about privacy as much as convenience:

The platform had per-conversation continuity (a visitor handle resumes the
same conversation) but nothing linked a conversation to *the person*. So a
customer who asked on WeChat and then emailed was two strangers, and the agent
answering the email could not see the question already asked.

**The link is by channel contact, never by enterprise account.** Colleagues
share an account. Resuming across an account boundary would show one employee's
messages to another - a privacy failure wearing a convenience feature's
clothes, and the kind that is discovered by a customer rather than a test.
Account binding answers "which contract is this" (7.3/SLA); it deliberately
does not answer "which human is this".

**What continuity returns is references, not transcripts.** The caller decides
what to load, and it loads through the same redaction and lease rules as any
other read. Returning text here would make this module a bypass around both.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.identity.control_lease import ConversationControlLease
from platform_core.support_bridge.continuity_models import ConversationContact

# How many earlier conversations to surface. Bounded because the point is
# "they were here before", not a full history - and an unbounded list is a
# prompt-sized liability.
DEFAULT_PRIOR_LIMIT = 5


@dataclass(frozen=True)
class PriorConversation:
    """A conversation this contact had elsewhere. A reference, not content."""

    conversation_ref_id: uuid.UUID
    channel: str | None
    opened_at: int
    lease_version: int | None = None


async def link_conversation(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_ref_id: uuid.UUID,
    external_contact_id: str,
    channel: str | None = None,
) -> ConversationContact:
    """Record which contact a conversation belongs to.

    Idempotent per conversation: a re-delivered inbound event updates the
    channel rather than failing the unique constraint, because a duplicate
    delivery is normal and must not look like an error.
    """
    if not external_contact_id:
        raise ValueError("external_contact_id is required to link a conversation")

    existing = (
        await session.execute(
            select(ConversationContact).where(
                ConversationContact.tenant_id == tenant_id,
                ConversationContact.conversation_ref_id == conversation_ref_id,
            )
        )
    ).scalar_one_or_none()

    now = int(time.time())
    if existing is not None:
        # A later event may know the channel the first one did not.
        if channel and not existing.channel:
            existing.channel = channel
            existing.updated_at = now
            await session.flush()
        return existing

    row = ConversationContact(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        conversation_ref_id=conversation_ref_id,
        external_contact_id=external_contact_id,
        channel=channel,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.flush()
    return row


async def prior_conversations(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    external_contact_id: str,
    exclude_conversation_ref_id: uuid.UUID | None = None,
    limit: int = DEFAULT_PRIOR_LIMIT,
) -> list[PriorConversation]:
    """Other conversations this contact had, newest first.

    `exclude_conversation_ref_id` is how the caller asks "what else has this
    person said", which is the useful question - including the current
    conversation in the answer is how a summary ends up quoting itself.
    """
    if limit <= 0 or not external_contact_id:
        return []

    stmt = select(ConversationContact).where(
        ConversationContact.tenant_id == tenant_id,
        ConversationContact.external_contact_id == external_contact_id,
    )
    if exclude_conversation_ref_id is not None:
        stmt = stmt.where(ConversationContact.conversation_ref_id != exclude_conversation_ref_id)
    # `created_at` is whole seconds, so two conversations opened in the same
    # second tie. The id breaks the tie to make the order *deterministic*:
    # without it, Postgres may return the same two rows in either order across
    # calls, and a UI listing "their other conversations" would reshuffle for
    # no reason. The id carries no chronology - the tie-break is arbitrary but
    # stable, which is what a list needs.
    stmt = stmt.order_by(
        ConversationContact.created_at.desc(), ConversationContact.id.desc()
    ).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()

    refs = [row.conversation_ref_id for row in rows]
    leases: dict[uuid.UUID, int] = {}
    if refs:
        # One extra query rather than a join: the lease is optional (a
        # conversation with no lease was never actioned) and joining would
        # silently drop rows where it is absent.
        lease_rows = (
            await session.execute(
                select(
                    ConversationControlLease.conversation_ref_id,
                    ConversationControlLease.lease_version,
                ).where(
                    ConversationControlLease.tenant_id == tenant_id,
                    ConversationControlLease.conversation_ref_id.in_(refs),
                )
            )
        ).all()
        leases = {row[0]: int(row[1]) for row in lease_rows}

    return [
        PriorConversation(
            conversation_ref_id=row.conversation_ref_id,
            channel=row.channel,
            opened_at=int(row.created_at),
            lease_version=leases.get(row.conversation_ref_id),
        )
        for row in rows
    ]


async def contact_for_conversation(
    session: AsyncSession, *, tenant_id: uuid.UUID, conversation_ref_id: uuid.UUID
) -> str | None:
    """Which contact a conversation belongs to, or None when unlinked.

    None is the honest answer for an anonymous visitor conversation and for
    anything predating this table; returning a placeholder would let a caller
    treat "unknown" as "same person".
    """
    return (
        await session.execute(
            select(ConversationContact.external_contact_id).where(
                ConversationContact.tenant_id == tenant_id,
                ConversationContact.conversation_ref_id == conversation_ref_id,
            )
        )
    ).scalar_one_or_none()


__all__ = [
    "DEFAULT_PRIOR_LIMIT",
    "PriorConversation",
    "contact_for_conversation",
    "link_conversation",
    "prior_conversations",
]
