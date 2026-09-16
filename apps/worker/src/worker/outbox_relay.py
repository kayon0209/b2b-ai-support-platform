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
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from observability import JsonLogger
from platform_core.db import session_scope
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

    async def run_once(self, session: AsyncSession) -> RelayStats:
        """Claim and dispatch one batch. Returns what happened.

        The session is supplied by the caller so a cycle shares one unit of
        work, matching `inbox_consumer.drain_once`.
        """
        stats = RelayStats()
        rows = await claim_pending(session, batch=self.batch)
        stats.claimed = len(rows)
        if not rows:
            return stats

        for row in rows:
            if row.attempts > self.max_attempts:
                # Already over budget from earlier cycles: leave it queued
                # and do not count it as work this cycle.
                stats.parked += 1
                continue

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

        return stats


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

    The two case events are the only ones `cases/router.py` produces today;
    listing them explicitly means a new event type is a deliberate addition
    rather than something that silently falls through.
    """
    relay = OutboxRelay(batch=batch)
    relay.register("case.created", log_only_handler)
    relay.register("case.updated", log_only_handler)
    return relay


async def drain_outbox_once(session: AsyncSession, *, relay: OutboxRelay) -> RelayStats:
    """Single-cycle helper mirroring `inbox_consumer.drain_once`."""
    return await relay.run_once(session)


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


def _now() -> int:  # pragma: no cover - trivial
    return int(time.time())


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
    "drain_outbox_once",
    "log_only_handler",
    "pending_count",
]
