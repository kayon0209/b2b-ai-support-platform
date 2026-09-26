"""Server-side tenant context (ticket 3).

tenant_id is NEVER accepted from client payloads. It is resolved from
authenticated membership (or later: connector configuration / trusted
resource mapping) and attached to the request scope. DB sessions read it
from this context and apply `SET LOCAL app.tenant_id` for RLS.

`tenant_session(ctx)` is the canonical way to open such a session, and it lives
here rather than in `platform_core.api` because **it is not an HTTP concern**.
Any caller that touches tenant data needs it - request handlers, and the
worker's agent-run path, which has no HTTP request at all. It used to live in
`api.py`, whose docstring says "shared HTTP helpers", so the worker either had
to import the request layer for a database helper or (as it did) hand-roll the
binding and get it wrong.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

ActorKind = Literal["user", "service", "system", "customer"]


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

    Prefer `tenant_session(ctx)`, which calls this **and** re-binds on every
    later transaction. Calling this alone is correct only when nothing in the
    scope will commit.
    """
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tid, true)"),
        {"tid": str(ctx.tenant_id)},
    )


def bind_tenant_on_every_transaction(session: AsyncSession, ctx: TenantContext) -> None:
    """Re-apply the RLS binding whenever a new transaction begins.

    `set_config('app.tenant_id', ..., true)` is **transaction-scoped**: it does
    not survive a `COMMIT`. A handler that commits mid-request - which every
    write handler does, so the row is durable before it is reported - therefore
    loses the binding, and every subsequent read returns **zero rows with no
    error**. The failure is indistinguishable from "this tenant has no data",
    which is why it is worth removing structurally rather than remembering.

    Two handlers had already been written the broken way:

    - `PUT /v1/tenant/quota` committed the new quota and then read the usage
      snapshot, so the response reported zero consumption for a tenant that
      had some;
    - `POST /v1/tenant/billing/adjustments` committed the correction and then
      read the rollup, reporting an empty ledger.

    The agent-run path was a third: it acquired the control lease (which
    commits), and every statement after that ran unbound - so the feature flags
    it read resolved to `UNKNOWN_FLAG`, and with RLS off entirely it read
    another tenant's row instead. See `tenant_session` and
    `FINDINGS-2026-09-21-CARD-AND-RLS.md`.

    Binding at `after_begin` fixes all of them and every future call site,
    instead of asking each author to re-apply it after each commit - a rule
    that has already been forgotten three times.
    """
    tenant_id = str(ctx.tenant_id)

    @event.listens_for(session.sync_session, "after_begin")
    def _rebind(_session: object, _transaction: object, connection: object) -> None:
        connection.execute(  # type: ignore[attr-defined]
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": tenant_id},
        )


@asynccontextmanager
async def tenant_session(ctx: TenantContext) -> AsyncIterator[AsyncSession]:
    """Session bound to the non-bypass app role with RLS applied.

    The bootstrap owner role is a superuser and would bypass RLS entirely,
    so tenant-facing work always connects as `platform_app`. The tenant is
    bound per transaction as additional defence on top of the query
    filters.

    The binding is re-applied at the start of every transaction, not once per
    session, so a caller that commits mid-scope keeps it - see
    `bind_tenant_on_every_transaction`.

    One scope is ONE tenant. A caller that must discover tenants from the rows
    it works on (a queue claim) cannot use this, and must open its own session:
    that is a claim, not tenant work - see `worker.wiring.queue_bookkeeping_session`.
    """
    from platform_core.db import app_role_url, session_scope_with_url

    async with session_scope_with_url(app_role_url()) as session:
        bind_tenant_on_every_transaction(session, ctx)
        # Bind before the caller's first statement. `apply_rls_tenant` opens a
        # transaction, which the listener above has already covered, so this is
        # belt-and-braces for a session handed out with no transaction yet.
        await apply_rls_tenant(session, ctx)
        yield session
