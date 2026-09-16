"""Audit service (ticket 24): one append-only write path for AuditEvent.

Every sensitive command (case commands, lease transfers, knowledge
publication, tool executions, denied authorization attempts) records an
audit event in the SAME transaction as the state change. No update or
delete APIs exist for audit rows.
"""

import hashlib
import json
import time
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.audit.models import AuditEvent
from platform_core.identity.tenant_context import TenantContext


def _hash_dict(payload: dict[str, Any] | None) -> str | None:
    if payload is None:
        return None
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def record(
    session: AsyncSession,
    *,
    ctx: TenantContext,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID | None = None,
    decision: str = "completed",
    reason_code: str = "OK",
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> uuid.UUID:
    """Append one audit event. Caller owns the transaction."""
    occurred = int(time.time())
    event = AuditEvent(
        tenant_id=ctx.tenant_id,
        occurred_at=occurred,
        actor_type=ctx.actor_kind,
        actor_id=ctx.actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        decision=decision,
        reason_code=reason_code,
        trace_id=trace_id or "",
        before_hash=_hash_dict(before),
        after_hash=_hash_dict(after),
        metadata_redacted={},
    )
    session.add(event)
    await session.flush()
    return event.id
