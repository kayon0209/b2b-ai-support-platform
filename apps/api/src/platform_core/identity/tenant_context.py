"""Server-side tenant context (ticket 3).

tenant_id is NEVER accepted from client payloads. It is resolved from
authenticated membership (or later: connector configuration / trusted
resource mapping) and attached to the request scope. DB sessions read it
from this context and apply `SET LOCAL app.tenant_id` for RLS.
"""

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

ActorKind = Literal["user", "service", "system"]


@dataclass(frozen=True)
class TenantContext:
    tenant_id: uuid.UUID
    actor_id: uuid.UUID | None
    actor_kind: ActorKind
    role: str | None = None


class TenantContextError(Exception):
    """Raised when a code path attempts to proceed without resolved context."""


_current: TenantContext | None = None


def set_tenant_context(ctx: TenantContext) -> None:
    global _current
    _current = ctx


def get_tenant_context() -> TenantContext:
    if _current is None:
        raise TenantContextError("tenant context not resolved")
    return _current


def clear_tenant_context() -> None:
    global _current
    _current = None


async def apply_rls_tenant(session: AsyncSession, ctx: TenantContext) -> None:
    """Bind tenant context to the current transaction for RLS.

    SET LOCAL is transaction-scoped and cannot leak across requests.
    The value comes from server-resolved context only.
    """
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": str(ctx.tenant_id)},
    )
