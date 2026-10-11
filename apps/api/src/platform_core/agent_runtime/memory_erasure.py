"""Tenant-scoped erasure for stored customer memory.

Conversation summaries are built per run and never persisted. This operation
removes their source turns and the long-term facts derived from those turns;
verified tasks and the append-only audit trail remain operational records.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.agent_runtime import conversation_store
from platform_core.support_bridge.continuity import conversation_refs_for_contact


async def erase_contact_memory(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    external_contact_id: str,
    channel: str,
) -> dict[str, int]:
    """Delete one verified channel contact's stored conversation memory."""
    normalized_channel = channel.strip().casefold()
    refs = await conversation_refs_for_contact(
        session,
        tenant_id=tenant_id,
        external_contact_id=external_contact_id,
        channel=normalized_channel,
    )
    counts = await conversation_store.erase_memory_rows(
        session,
        tenant_id=tenant_id,
        contact_ref=conversation_store.contact_ref_from_external(
            tenant_id,
            external_contact_id,
            channel=normalized_channel,
        ),
        conversation_ref_ids=refs,
    )
    return {"conversations_matched": len(refs), **counts}
