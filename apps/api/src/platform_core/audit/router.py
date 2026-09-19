"""Audit management query API (ticket 26).

- GET /v1/audit-events: tenant-scoped, paginated, filterable.
- Access controlled by the policy engine: only auditor / security_admin /
  tenant_owner may read audit data (docs/security.md audit section).
- RLS still bounds the query to the caller's tenant; the policy layer is
  the first gate, RLS the second.
"""

from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.api import error_response, tenant_session
from platform_core.audit.models import AuditEvent
from platform_core.identity import tenant_context
from platform_core.identity.tenant_context import TenantContext
from platform_policy import Action, Decision, PolicyEngine, Principal

router = APIRouter(prefix="/v1/audit-events", tags=["audit"])


class AuditEventOut(BaseModel):
    id: str
    occurred_at: int
    actor_type: str
    actor_id: str | None
    action: str
    resource_type: str
    resource_id: str | None
    decision: str
    reason_code: str
    trace_id: str
    before_hash: str | None
    after_hash: str | None


def _principal_from_ctx(ctx: TenantContext) -> Principal:
    return Principal(
        tenant_id=str(ctx.tenant_id),
        actor_id=str(ctx.actor_id) if ctx.actor_id else "",
        role=ctx.role or "unknown",
    )


async def _query_events(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    action: str | None,
    decision: str | None,
    since: int | None,
) -> tuple[list[AuditEvent], int]:
    stmt = select(AuditEvent).order_by(AuditEvent.occurred_at.desc(), AuditEvent.id)
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    if decision:
        stmt = stmt.where(AuditEvent.decision == decision)
    if since is not None:
        stmt = stmt.where(AuditEvent.occurred_at >= since)
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await session.execute(stmt.limit(limit).offset(offset))).scalars().all()
    return list(rows), int(total)


@router.get("")
async def list_audit_events(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    action: str | None = Query(default=None, max_length=127),
    decision: str | None = Query(default=None, max_length=31),
    since: int | None = Query(default=None),
) -> Any:
    ctx = getattr(request.state, "tenant_context", None)
    if ctx is None:
        ctx = tenant_context.get_tenant_context()

    # Policy gate 1: role-based audit read permission.
    engine = PolicyEngine()
    principal = _principal_from_ctx(ctx)
    gate = engine.check(principal, Action.AUDIT_READ)
    if gate.decision != Decision.ALLOW.value:
        # 403, not a 200 with an error body: a denial carrying a success
        # status is read as success by any client that branches on it.
        return error_response(
            "AUDIT_ACCESS_DENIED",
            gate.reason_code or "audit access denied",
            status_code=403,
        )

    async with tenant_session(ctx) as session:
        rows, total = await _query_events(
            session,
            limit=limit,
            offset=offset,
            action=action,
            decision=decision,
            since=since,
        )
        items = [
            AuditEventOut(
                id=str(r.id),
                occurred_at=r.occurred_at,
                actor_type=r.actor_type,
                actor_id=str(r.actor_id) if r.actor_id else None,
                action=r.action,
                resource_type=r.resource_type,
                resource_id=str(r.resource_id) if r.resource_id else None,
                decision=r.decision,
                reason_code=r.reason_code,
                trace_id=r.trace_id,
                before_hash=r.before_hash,
                after_hash=r.after_hash,
            ).model_dump()
            for r in rows
        ]
    return {"items": items, "total": total, "limit": limit, "offset": offset}
