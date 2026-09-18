"""Billing: the `usage.recorded` consumer and the monthly rollup.

`docs/development-plan.md` Phase 5 lists "usage quotas and billing events".
The emitter and the quota enforcement existed; the consumer and the rollup
did not. `build_default_relay` registered only the two case events, so a
`usage.recorded` row travelled the outbox and hit the log-only default -
observable, but never aggregated. Nothing downstream could answer "what did
this tenant consume in March".

Two properties this module is built around:

**Idempotent by the event's own id.** The outbox is at-least-once, so the
same event can arrive twice. `record_usage` keys on `event_id` and uses
`ON CONFLICT DO NOTHING`, so a redelivery is a no-op that reports itself as
such. An insert-then-catch would work too, but a caught integrity error
aborts the enclosing transaction - and the relay commits a batch at a time,
so one duplicate would discard the batch's other work.

**Append-only.** The app role has no UPDATE or DELETE on the ledger, so a
correction must be a new row. `record_adjustment` exists for that; it does
not recompute history.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.billing.models import BillingEntry, EntryKind
from platform_core.identity.usage import period_bounds
from platform_core.outbox import OutboxEvent


@dataclass(frozen=True)
class RecordOutcome:
    """Whether the event produced a new row, or was already recorded."""

    entry_id: uuid.UUID | None
    duplicate: bool


async def record_usage(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    event_id: uuid.UUID,
    run_id: uuid.UUID,
    route: str = "",
    run_status: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    recorded_at: int | None = None,
) -> RecordOutcome:
    """Append one usage entry. Safe to call twice for the same event.

    Keyed on `event_id` rather than `run_id`: a redelivery of one event must
    collapse, while two legitimate events about one run (a completion and a
    later correction) must not.
    """
    ts = int(time.time()) if recorded_at is None else recorded_at
    period_start, _ = period_bounds(ts)

    stmt = (
        pg_insert(BillingEntry)
        .values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            event_id=event_id,
            run_id=run_id,
            entry_kind=EntryKind.USAGE.value,
            route=route,
            run_status=run_status,
            prompt_tokens=max(0, int(prompt_tokens)),
            completion_tokens=max(0, int(completion_tokens)),
            period_start=period_start,
            recorded_at=ts,
        )
        .on_conflict_do_nothing(constraint="uq_billing_entry_event")
        .returning(BillingEntry.id)
    )
    inserted = (await session.execute(stmt)).scalar_one_or_none()
    return RecordOutcome(entry_id=inserted, duplicate=inserted is None)


async def record_adjustment(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    prompt_tokens_delta: int = 0,
    completion_tokens_delta: int = 0,
    recorded_at: int | None = None,
    idempotency_key: str | None = None,
) -> RecordOutcome:
    """Append a correction. Never edits an existing row.

    The deltas are stored in the same non-negative columns as usage, so a
    correction that reduces a count is expressed as a negative delta - which
    the CHECK constraints forbid. Adjustments therefore carry their magnitude
    in the token columns while `entry_kind='adjustment'` tells the rollup to
    subtract rather than add.

    **Idempotency.** With an `idempotency_key` the row is keyed on
    `(tenant, key)` and a repeat is a no-op, which is what makes this safe to
    expose as a write command: without it, a client retry after a timeout
    would credit or debit the account twice. Without a key the old behaviour
    stands - keyed on `(run, timestamp)`, so a deliberate second correction at
    a different moment is a second row - which is what ad-hoc and test callers
    want.

    `ON CONFLICT DO NOTHING` rather than insert-then-catch: a caught
    IntegrityError aborts the enclosing transaction, and the relay commits a
    batch at a time, so one duplicate would discard the batch's other work.
    """
    ts = int(time.time()) if recorded_at is None else recorded_at
    period_start, _ = period_bounds(ts)
    if idempotency_key is not None:
        event_id = uuid.uuid5(uuid.NAMESPACE_URL, f"adjustment:{tenant_id}:{idempotency_key}")
    else:
        event_id = uuid.uuid5(uuid.NAMESPACE_URL, f"adjustment:{run_id}:{ts}")

    stmt = (
        pg_insert(BillingEntry)
        .values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            event_id=event_id,
            run_id=run_id,
            entry_kind=EntryKind.ADJUSTMENT.value,
            route="",
            run_status="",
            prompt_tokens=abs(int(prompt_tokens_delta)),
            completion_tokens=abs(int(completion_tokens_delta)),
            period_start=period_start,
            recorded_at=ts,
        )
        .on_conflict_do_nothing(constraint="uq_billing_entry_event")
        .returning(BillingEntry.id)
    )
    inserted = (await session.execute(stmt)).scalar_one_or_none()
    return RecordOutcome(entry_id=inserted, duplicate=inserted is None)


@dataclass(frozen=True)
class BillingRollup:
    period_start: int
    period_end: int
    entries: int
    usage_entries: int
    adjustment_entries: int
    prompt_tokens: int
    completion_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> dict[str, int]:
        return {
            "period_start": self.period_start,
            "period_end": self.period_end,
            "entries": self.entries,
            "usage_entries": self.usage_entries,
            "adjustment_entries": self.adjustment_entries,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


async def monthly_rollup(
    session: AsyncSession, *, tenant_id: uuid.UUID, now: int | None = None
) -> BillingRollup:
    """Aggregate one tenant's ledger for the calendar month containing `now`.

    Adjustments subtract. The sign is applied here rather than stored,
    because a signed token column would let a negative slip into the usage
    path and make "how much did we consume" unanswerable.
    """
    ts = int(time.time()) if now is None else now
    start, end = period_bounds(ts)

    rows = (
        await session.execute(
            select(
                BillingEntry.entry_kind,
                func.count(),
                func.sum(BillingEntry.prompt_tokens),
                func.sum(BillingEntry.completion_tokens),
            )
            .where(
                BillingEntry.tenant_id == tenant_id,
                BillingEntry.period_start == start,
            )
            .group_by(BillingEntry.entry_kind)
        )
    ).all()

    usage_entries = adjustment_entries = 0
    prompt = completion = 0
    for kind, count, prompt_sum, completion_sum in rows:
        n = int(count or 0)
        sign = -1 if kind == EntryKind.ADJUSTMENT.value else 1
        if kind == EntryKind.ADJUSTMENT.value:
            adjustment_entries = n
        else:
            usage_entries = n
        prompt += sign * int(prompt_sum or 0)
        completion += sign * int(completion_sum or 0)

    return BillingRollup(
        period_start=start,
        period_end=end,
        entries=usage_entries + adjustment_entries,
        usage_entries=usage_entries,
        adjustment_entries=adjustment_entries,
        # Floor at zero: an over-applied correction should not report negative
        # consumption, which reads as a data error rather than a correction.
        prompt_tokens=max(0, prompt),
        completion_tokens=max(0, completion),
    )


async def handle_usage_recorded(session: AsyncSession, event: OutboxEvent) -> None:
    """Outbox handler for `usage.recorded`.

    Signature matches `outbox_relay.OutboxHandler` (session, event). Raises
    on a malformed payload so the relay records `last_error` and retries -
    silently dropping a billing event is worse than a parked row an operator
    can see.
    """
    payload = event.payload or {}
    try:
        run_id = uuid.UUID(str(payload["run_id"]))
    except (KeyError, ValueError) as exc:
        raise ValueError(f"usage.recorded payload has no usable run_id: {payload}") from exc

    await record_usage(
        session,
        tenant_id=event.tenant_id,
        event_id=event.event_id,
        run_id=run_id,
        route=str(payload.get("route", "")),
        run_status=str(payload.get("status", "")),
        prompt_tokens=int(payload.get("prompt_tokens", 0) or 0),
        completion_tokens=int(payload.get("completion_tokens", 0) or 0),
        recorded_at=event.created_at or None,
    )


__all__ = [
    "BillingRollup",
    "RecordOutcome",
    "handle_usage_recorded",
    "monthly_rollup",
    "record_adjustment",
    "record_usage",
]
