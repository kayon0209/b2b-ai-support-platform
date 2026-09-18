"""Outbox relay consumer (ticket 7, docs/architecture.md data storage).

Business state and its outbound event are written in one transaction
(`outbox_service.enqueue`). Nothing has happened externally yet at that
point: the row is committed, the world does not know. This module publishes
the queued rows and marks them sent.

Why a consumer is needed at all: without one, every `case.created` /
`case.updated` event accumulates in the table forever. The transactional
outbox is only half a pattern until something relays it — the audit trail
would claim a downstream notification that never left the process.

Delivery semantics chosen here, and why:

- **At-least-once.** A crash after publish but before `mark_sent` re-sends.
  That is deliberate: the alternative (mark sent, then publish) can silently
  drop an event, which is worse for a support system than a duplicate.
  Consumers dedupe using the stable `event_id`, which is why it is a
  column and not the row primary key.
- **`attempts` bounds retries.** A handler that keeps failing eventually
  parks (status stays `queued`, `last_error` recorded) rather than spinning
  forever. Parking is visible because `attempts` and `last_error` are on
  the row.
- **One transaction per batch, not per event.** The claim uses
  `FOR UPDATE SKIP LOCKED`, so two relays never fight over the same row,
  and a handler failure inside the batch does not roll back the events
  already marked sent in that batch.

The relay is transport-agnostic in the same way `runner.py` is: handlers
are registered in a dict, so tests drive real dispatch without a broker and
production can back them with Celery or an HTTP sink.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from observability import JsonLogger
from platform_core.billing.service import handle_usage_recorded
from platform_core.db import session_scope
from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant
from platform_core.outbox import OutboxEvent
from platform_core.outbox_service import claim_pending, mark_failed, mark_sent

logger = JsonLogger("platform.outbox_relay")

# After this many attempts a row stops being retried in the normal cycle.
# It stays `queued` so an operator can requeue it deliberately; silently
# marking it `failed` would hide an event that was never delivered.
MAX_ATTEMPTS = 5

DEFAULT_BATCH = 50


class OutboxHandlerError(Exception):
    """Raised by a handler when delivery failed and retry is appropriate."""


# A handler receives the full event row so it can use payload, trace_id and
# tenant_id without another lookup.
OutboxHandler = Callable[[AsyncSession, OutboxEvent], Awaitable[None]]


@dataclass
class RelayStats:
    """Per-cycle outcome, returned so callers and tests can assert on it."""

    claimed: int = 0
    sent: int = 0
    failed: int = 0
    parked: int = 0
    unhandled: int = 0

    @property
    def processed(self) -> int:
        return self.sent + self.failed + self.parked + self.unhandled


@dataclass
class OutboxRelay:
    """Publishes queued outbox rows to registered handlers.

    `handlers` maps `event_type` to a coroutine. An event with no handler is
    counted as `unhandled` and marked sent: the platform produced the event
    and no component in this deployment consumes it yet, which is a
    configuration fact rather than a delivery failure. Retrying forever
    would pin the queue for an audience that does not exist.
    """

    handlers: dict[str, OutboxHandler] = field(default_factory=dict)
    batch: int = DEFAULT_BATCH
    max_attempts: int = MAX_ATTEMPTS

    def register(self, event_type: str, handler: OutboxHandler) -> None:
        self.handlers[event_type] = handler

    async def run_once(self, session: AsyncSession, *, commit: bool = False) -> RelayStats:
        """Claim and dispatch one batch. Returns what happened.

        The session is supplied by the caller so a cycle shares one unit of
        work, matching `inbox_consumer.drain_once`.

        **The caller owns the transaction.** Everything this method writes -
        the handler's rows and the `mark_sent` / `mark_failed` update - lands
        in the session's transaction, which is only durable once somebody
        commits. `OutboxWorker.run_once` supplies a `session_scope()`, which
        commits; a caller that passes a raw session must either commit
        itself or pass `commit=True`, which commits once at the end of the
        batch.

        Getting this wrong is silent and expensive. A caller that forgets
        sees `RelayStats.sent == 1` - the handler ran, no exception was
        raised - while the transaction is rolled back on close and nothing
        was actually recorded. This is exactly how the billing ledger was
        found reporting `sent=1` with an empty rollup.

        `commit=True` is deliberately the *end* of the batch, not per row:
        the batch shares one unit of work, so a crash mid-batch leaves the
        whole batch to be retried rather than half-delivered.

        **Each row is dispatched under its own tenant's RLS binding.** The
        claim query runs before any tenant is known (the worker discovers
        tenants from the rows it claims), but everything after it - the
        handler's reads and writes, and the `mark_sent` / `mark_failed`
        update - must run with `app.tenant_id` set to that row's tenant.
        Without it the app role's RLS policy filters the work away, and the
        failure is invisible: an insert whose `WITH CHECK` does not match is
        rejected, but `ON CONFLICT DO NOTHING` turns that rejection into
        zero rows inserted and no error, so the relay reports `sent` while
        the ledger stays empty. This was found exactly that way - a billing
        test saw `sent=1` and an empty rollup.
        """
        stats = RelayStats()
        rows = await claim_pending(session, batch=self.batch)
        stats.claimed = len(rows)
        if not rows:
            return stats

        await self._dispatch(session, rows, stats)
        if commit:
            await session.commit()
        return stats

    async def _dispatch(
        self, session: AsyncSession, rows: list[OutboxEvent], stats: RelayStats
    ) -> None:
        """Dispatch claimed rows in place. Commit stays with the caller."""

        for row in rows:
            if row.attempts > self.max_attempts:
                # Already over budget from earlier cycles: leave it queued
                # and do not count it as work this cycle.
                stats.parked += 1
                continue

            # Scope to the event's tenant before the handler touches anything.
            # Set per row, so one batch can span tenants without mixing them.
            await apply_rls_tenant(session, _ctx_for(row))

            handler = self.handlers.get(row.event_type)
            if handler is None:
                logger.info(
                    "outbox_event_unhandled",
                    event_type=row.event_type,
                    event_id=str(row.event_id),
                )
                await mark_sent(session, row.id)
                stats.unhandled += 1
                continue

            try:
                await handler(session, row)
            except Exception as exc:  # noqa: BLE001 - classified below
                # Record, do not re-raise: one bad event must not stop the
                # rest of the batch or roll back the publishes already done.
                #
                # The savepoint is required. A handler that failed *inside*
                # the database (an integrity error, a policy violation) has
                # aborted the enclosing transaction, so the `mark_failed`
                # UPDATE below would itself raise - and the reason the event
                # failed would be lost, which is the one thing an operator
                # needs from a parked row.
                async with session.begin_nested():
                    await mark_failed(session, row.id, f"{type(exc).__name__}: {exc}")
                logger.warning(
                    "outbox_delivery_failed",
                    event_type=row.event_type,
                    event_id=str(row.event_id),
                    attempts=row.attempts,
                    error_code=type(exc).__name__,
                )
                stats.failed += 1
                continue

            await mark_sent(session, row.id)
            stats.sent += 1


async def log_only_handler(session: AsyncSession, event: OutboxEvent) -> None:
    """Default handler: record that the event happened.

    A real deployment replaces this per event type with a broker publish or
    an HTTP call to a downstream consumer. Keeping the default as a log
    means an unconfigured event type is observable rather than invisible,
    and it never pretends to have notified anything.
    """
    logger.info(
        "outbox_event_relayed",
        event_type=event.event_type,
        aggregate_type=event.aggregate_type,
        aggregate_id=event.aggregate_id,
        tenant_id=str(event.tenant_id),
        trace_id=event.trace_id,
    )


def build_default_relay(batch: int = DEFAULT_BATCH) -> OutboxRelay:
    """Relay with the event types the platform currently emits.

    Every emitted type is listed explicitly. That is the point of this
    function: a new event type is a deliberate addition here rather than
    something that silently falls through to the log-only default.

    `usage.recorded` was the case that motivated the rule. The orchestrator
    had been enqueuing it since the usage API landed, and because this list
    named only the two case events, every billing event hit the log-only
    handler - so "usage quotas and billing events" (docs/development-plan.md
    Phase 5) had an emitter and no aggregator.
    """
    relay = OutboxRelay(batch=batch)
    relay.register("case.created", log_only_handler)
    relay.register("case.updated", log_only_handler)
    relay.register("usage.recorded", handle_usage_recorded)
    return relay


class OutboxWorker:
    """Polls the outbox and relays batches.

    Shaped like `InboxWorker` in runner.py: same cooperative stop, same
    "sleep only when there was nothing to do" drain strategy.
    """

    def __init__(
        self,
        relay: OutboxRelay,
        *,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self._relay = relay
        self._poll_interval = poll_interval_seconds
        self._stopping = False

    def request_stop(self) -> None:
        self._stopping = True

    @property
    def stopping(self) -> bool:
        return self._stopping

    async def run_once(self) -> RelayStats:
        async with session_scope() as session:
            return await self._relay.run_once(session)

    async def run_forever(self) -> None:
        logger.info("outbox_relay_started")
        while not self._stopping:
            try:
                stats = await self.run_once()
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                logger.error("outbox_relay_cycle_failed", error_code=type(exc).__name__)
                await asyncio.sleep(self._poll_interval)
                continue

            if stats.parked:
                # Surface parked rows every cycle; they need a human.
                logger.warning("outbox_rows_parked", count=stats.parked)

            if stats.processed == 0:
                await asyncio.sleep(self._poll_interval)
        logger.info("outbox_relay_stopped")


def _ctx_for(row: OutboxEvent) -> TenantContext:
    """The RLS context for an outbox row.

    `actor_kind="system"`: the relay acts on behalf of no user. The actor is
    what audit trails attribute an action to, and claiming a user here would
    attribute a delivery to someone who did not make it.
    """
    return TenantContext(
        tenant_id=row.tenant_id,
        actor_id=None,
        actor_kind="system",
    )


async def pending_count(session: AsyncSession) -> int:
    """Queued rows, for health checks and operational dashboards.

    Exposed as a function rather than a metric so it can be sampled by
    whichever endpoint or exporter the deployment uses.
    """
    from sqlalchemy import func, select

    stmt = select(func.count()).select_from(OutboxEvent).where(OutboxEvent.status == "queued")
    return int((await session.execute(stmt)).scalar_one())


__all__ = [
    "DEFAULT_BATCH",
    "MAX_ATTEMPTS",
    "OutboxHandler",
    "OutboxHandlerError",
    "OutboxRelay",
    "OutboxWorker",
    "RelayStats",
    "build_default_relay",
    "log_only_handler",
    "pending_count",
]
