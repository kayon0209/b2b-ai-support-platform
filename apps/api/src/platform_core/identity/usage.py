"""Tenant usage and quota API (Phase 5: usage quotas and billing events).

    GET /v1/tenant/usage    current-period consumption and quota
    PUT /v1/tenant/quota    set the monthly agent-run quota

Usage is measured in agent runs started this calendar month (UTC) -- the unit
the platform meters on -- plus the tokens those runs reported. A NULL quota
means unlimited.

The quota is *enforced* when a run is queued (`agent_runtime.router`): over
quota is a 429, not a silent drop, so a caller can tell "we declined for
capacity" from "we had no evidence". Billing events
(`usage.recorded`) are emitted separately, when a run reaches a terminal
outcome.

Reading usage needs only an authenticated member; setting the quota is a
commercial change and requires TENANT_ADMIN plus an Idempotency-Key.
"""

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import Integer, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.agent_runtime.models import AgentRun
from platform_core.api import (
    error_response,
    get_context,
    new_trace_id,
    ok_response,
    require_policy,
    require_write_idempotency,
    tenant_session,
)
from platform_core.audit import service as audit_service
from platform_core.identity.models import Tenant
from platform_policy import Action

router = APIRouter(prefix="/v1/tenant", tags=["tenant"])


class QuotaIn(BaseModel):
    # None clears the quota (unlimited).
    #
    # The upper bound is the column's, not a product decision: the stored
    # value is an int4, so anything above 2**31-1 overflows at the database
    # and came back as an unhandled 500 rather than a refused request.
    monthly_run_quota: int | None = Field(default=None, ge=0, le=2_147_483_647)


class AdjustmentIn(BaseModel):
    """A correction against the billing ledger.

    The deltas are signed at this boundary and stored unsigned with
    `entry_kind='adjustment'`, because a negative token column would make
    "how much did we consume" unanswerable. A negative delta is therefore a
    credit and a positive one an extra charge.
    """

    run_id: uuid.UUID
    prompt_tokens_delta: int = 0
    completion_tokens_delta: int = 0
    reason: str = Field(default="", max_length=500)

    def is_empty(self) -> bool:
        return self.prompt_tokens_delta == 0 and self.completion_tokens_delta == 0


@dataclass(frozen=True)
class UsageSnapshot:
    period_start: int
    period_end: int
    runs_used: int
    prompt_tokens: int
    completion_tokens: int
    quota: int | None

    @property
    def remaining(self) -> int | None:
        return None if self.quota is None else max(0, self.quota - self.runs_used)

    @property
    def over_quota(self) -> bool:
        return self.quota is not None and self.runs_used >= self.quota

    def as_dict(self) -> dict[str, Any]:
        return {
            "period_start": self.period_start,
            "period_end": self.period_end,
            "runs_used": self.runs_used,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "quota": self.quota,
            "remaining": self.remaining,
            "over_quota": self.over_quota,
        }


def period_bounds(now: int) -> tuple[int, int]:
    """[start, end) of the calendar month containing `now`, in UTC seconds."""
    dt = datetime.fromtimestamp(now, tz=UTC)
    start = datetime(dt.year, dt.month, 1, tzinfo=UTC)
    end = datetime(dt.year + dt.month // 12, dt.month % 12 + 1, 1, tzinfo=UTC)
    return int(start.timestamp()), int(end.timestamp())


async def usage_snapshot(
    session: AsyncSession, *, tenant_id: uuid.UUID, now: int | None = None
) -> UsageSnapshot:
    """Current-period usage for one tenant.

    Runs created before `started_at` was populated have no timestamp and are
    not counted; the quality dashboard treats them the same way.
    """
    ts = int(time.time()) if now is None else now
    start, end = period_bounds(ts)
    in_period = (
        AgentRun.tenant_id == tenant_id,
        AgentRun.started_at.is_not(None),
        AgentRun.started_at >= start,
        AgentRun.started_at < end,
    )

    runs_used = int(
        (
            await session.execute(select(func.count()).select_from(AgentRun).where(*in_period))
        ).scalar_one()
    )
    prompt_tokens, completion_tokens = (
        await session.execute(
            select(
                func.coalesce(
                    func.sum(cast(AgentRun.token_usage["prompt_tokens"].astext, Integer)), 0
                ),
                func.coalesce(
                    func.sum(cast(AgentRun.token_usage["completion_tokens"].astext, Integer)), 0
                ),
            ).where(*in_period)
        )
    ).one()

    quota = (
        await session.execute(select(Tenant.monthly_run_quota).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()

    return UsageSnapshot(
        period_start=start,
        period_end=end,
        runs_used=runs_used,
        prompt_tokens=int(prompt_tokens or 0),
        completion_tokens=int(completion_tokens or 0),
        quota=None if quota is None else int(quota),
    )


def _unresolved() -> Any:
    return error_response("AUTH_UNRESOLVED", "tenant context not resolved", status_code=401)


@router.get("/usage")
async def get_usage(request: Request) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()
    async with tenant_session(ctx) as session:
        snapshot = await usage_snapshot(session, tenant_id=ctx.tenant_id)
    return ok_response({"usage": snapshot.as_dict()})


@router.get("/billing")
async def get_billing_rollup(request: Request) -> Any:
    """This tenant's billing ledger total for the current calendar month.

    Reads the append-only ledger (migration 0031) rather than re-counting
    `agent_runs`: the ledger is the record an invoice is computed from, and
    it is fed by the same `usage.recorded` event the run emits, so the two
    agree by construction while the ledger additionally survives a run row
    being rewritten.

    Requires AUDIT_READ - the same audience as the audit trail. Billing
    totals are commercial data, not operational telemetry a support agent
    needs to answer a customer.
    """
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()

    denied = require_policy(ctx, Action.AUDIT_READ)
    if denied is not None:
        return denied

    from platform_core.billing import monthly_rollup

    async with tenant_session(ctx) as session:
        rollup = await monthly_rollup(session, tenant_id=ctx.tenant_id)
    return ok_response({"billing": rollup.as_dict()})


@router.put("/quota")
async def set_quota(request: Request, body: QuotaIn) -> Any:
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()

    denied = require_policy(ctx, Action.TENANT_ADMIN)
    if denied is not None:
        return denied
    missing_idem = require_write_idempotency(request, Action.TENANT_ADMIN)
    if missing_idem is not None:
        return missing_idem

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.id == ctx.tenant_id))
        ).scalar_one_or_none()
        if tenant is None:
            return error_response("TENANT_NOT_FOUND", "tenant not found", status_code=404)

        before = tenant.monthly_run_quota
        tenant.monthly_run_quota = body.monthly_run_quota
        await audit_service.record(
            session,
            ctx=ctx,
            action="tenant.quota.updated",
            resource_type="tenant",
            resource_id=tenant.id,
            before={"monthly_run_quota": before},
            after={"monthly_run_quota": body.monthly_run_quota},
            trace_id=trace_id,
        )
        await session.commit()
        snapshot = await usage_snapshot(session, tenant_id=ctx.tenant_id)

    return ok_response({"usage": snapshot.as_dict()}, trace_id=trace_id)


@router.post("/billing/adjustments")
async def record_billing_adjustment(request: Request, body: AdjustmentIn) -> Any:
    """Correct a tenant's billed consumption.

    The ledger is append-only (the app role holds no UPDATE or DELETE), so a
    correction is a **new** row rather than an edit. That is the whole point:
    the record of what a customer consumed cannot be quietly rewritten, and an
    adjustment is visible as an adjustment.

    Reserved for `tenant_owner` (BILLING_ADJUST). Crediting an account is a
    financial statement about a customer, not an administrative convenience.

    Idempotent on the request's `Idempotency-Key`, which is required here even
    though `record_adjustment` can key on the timestamp instead: a client
    retry after a timeout must not credit or debit twice, and a timestamp key
    cannot tell a retry from a deliberate second correction.
    """
    ctx = get_context(request)
    if ctx is None:
        return _unresolved()

    denied = require_policy(ctx, Action.BILLING_ADJUST)
    if denied is not None:
        return denied
    missing_idem = require_write_idempotency(request, Action.BILLING_ADJUST)
    if missing_idem is not None:
        return missing_idem

    idempotency_key = request.headers.get("Idempotency-Key")
    if body.is_empty():
        # A zero adjustment would append a row that changes no total. Refuse
        # rather than let the ledger accumulate entries that mean nothing.
        return error_response(
            "ADJUSTMENT_EMPTY",
            "an adjustment must change at least one token count",
            status_code=400,
        )

    from platform_core.billing import monthly_rollup, record_adjustment

    trace_id = new_trace_id()
    async with tenant_session(ctx) as session:
        outcome = await record_adjustment(
            session,
            tenant_id=ctx.tenant_id,
            run_id=body.run_id,
            prompt_tokens_delta=body.prompt_tokens_delta,
            completion_tokens_delta=body.completion_tokens_delta,
            idempotency_key=idempotency_key,
        )
        # Audit only a write that happened. A redelivery is a no-op, and an
        # audit row per retry would report activity that never occurred.
        if not outcome.duplicate:
            await audit_service.record(
                session,
                ctx=ctx,
                action="billing.adjustment.recorded",
                resource_type="billing_entry",
                resource_id=outcome.entry_id,
                after={
                    "run_id": str(body.run_id),
                    "prompt_tokens_delta": body.prompt_tokens_delta,
                    "completion_tokens_delta": body.completion_tokens_delta,
                    "reason": body.reason,
                },
                trace_id=trace_id,
            )
        await session.commit()
        rollup = await monthly_rollup(session, tenant_id=ctx.tenant_id)

    return ok_response(
        {"billing": rollup.as_dict(), "duplicate": outcome.duplicate}, trace_id=trace_id
    )


__all__ = ["UsageSnapshot", "period_bounds", "router", "usage_snapshot"]
