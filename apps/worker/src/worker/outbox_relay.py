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

- Queue claims are committed before handler work, and carry a processing
  token. A process restart therefore consumes a durable attempt rather than
  erasing its retry count.
- Database handlers that are safe to replay use the stable `event_id`
  as their deduplication key. A customer-visible reply is not automatically
  replayed after an ambiguous result or a worker crash; it is parked for human
  review because the channel may already have accepted it.
- **`attempts` bounds retries.** A handler that keeps failing eventually
  is parked after a small, durable attempt budget rather than spinning
  forever. Each handler runs in its tenant-bound app-role transaction; the
  owner connection reads queue metadata only.

The relay is transport-agnostic in the same way `runner.py` is: handlers
are registered in a dict, so tests drive real dispatch without a broker and
production can back them with Celery or an HTTP sink.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from observability import JsonLogger
from observability_metrics import get_metrics
from platform_core.agent_runtime.copilot import COPILOT_EVENT_TYPE
from platform_core.agent_runtime.semantic.shadow import SHADOW_EVENT_TYPE
from platform_core.agent_runtime.tasks.planning_seam import TASK_PLANNING_EVENT_TYPE
from platform_core.billing.service import handle_usage_recorded
from platform_core.execution_budget import ExecutionBudget, use_execution_budget
from platform_core.identity.tenant_context import TenantContext, tenant_session
from platform_core.knowledge.release_evaluator import (
    RELEASE_EVALUATION_REQUEST_EVENT,
    RELEASE_POST_TEST_REQUEST_EVENT,
)
from platform_core.outbox import OutboxEvent, OutboxStatus
from platform_core.outbox_service import (
    OutboxClaim,
    claim_pending,
    mark_failed,
    mark_sent,
    reclaim_stale_claims,
)

logger = JsonLogger("platform.outbox_relay")

# After this many attempts a row stops being retried in the normal cycle.
# It stays `queued` so an operator can requeue it deliberately; silently
# marking it `failed` would hide an event that was never delivered.
MAX_ATTEMPTS = 5
STALE_OUTBOX_CLAIM_SECONDS = 600
RETRY_SAFE_EVENT_TYPES = frozenset({"case.created", "case.updated", "usage.recorded"})

DEFAULT_BATCH = 50


def _external_attempts_for_delivery(claim: OutboxClaim) -> int:
    """Partition one persisted event ceiling across its bounded deliveries."""
    if claim.max_attempts < 1 or claim.attempts < 1 or claim.attempts > claim.max_attempts:
        return 0
    base, remainder = divmod(claim.external_attempt_limit, claim.max_attempts)
    return base + (1 if claim.attempts <= remainder else 0)


class OutboxHandlerError(Exception):
    """Raised by a handler when delivery failed and retry is appropriate."""


class OutboxOutcomeUnknown(OutboxHandlerError):
    """A provider may have accepted a side effect; the event must not replay."""


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
    excluded_event_types: tuple[str, ...] = ()

    def register(self, event_type: str, handler: OutboxHandler) -> None:
        self.handlers[event_type] = handler

    async def run_once(self, session: AsyncSession, *, commit: bool = False) -> RelayStats:
        """Commit global queue claims, then run each handler in its tenant scope.

        The supplied session is the queue-bookkeeping connection. It reads
        only claim metadata and commits the processing token before dispatch.
        Each handler reloads the full event under a tenant-bound app-role
        session and commits its business work with the sent/failed receipt.
        """
        stats = RelayStats()
        now = int(time.time())
        requeued, stale_failed = await reclaim_stale_claims(
            session,
            cutoff=now - STALE_OUTBOX_CLAIM_SECONDS,
            max_attempts=self.max_attempts,
            retry_safe_event_types=RETRY_SAFE_EVENT_TYPES,
            exclude_event_types=self.excluded_event_types,
        )
        stats.failed += stale_failed
        if requeued or stale_failed:
            logger.warning(
                "outbox_stale_claims_recovered",
                requeued_count=requeued,
                failed_count=stale_failed,
            )
        parked_query = (
            select(func.count())
            .select_from(OutboxEvent)
            .where(
                OutboxEvent.status == OutboxStatus.QUEUED.value,
                OutboxEvent.attempts >= self.max_attempts,
            )
        )
        if self.excluded_event_types:
            parked_query = parked_query.where(
                OutboxEvent.event_type.not_in(self.excluded_event_types)
            )
        stats.parked = int((await session.scalar(parked_query)) or 0)
        claims = await claim_pending(
            session,
            batch=self.batch,
            exclude_event_types=self.excluded_event_types,
            max_attempts=self.max_attempts,
        )
        stats.claimed = len(claims)
        # A claim is durable before any tenant handler or external side effect.
        await session.commit()
        if claims:
            await self._dispatch_claims(claims, stats)
        if commit:
            await session.commit()
        return stats

    async def _dispatch_claims(self, claims: list[OutboxClaim], stats: RelayStats) -> None:
        for claim in claims:
            await self._dispatch_claim(claim, stats)

    async def _dispatch_claim(self, claim: OutboxClaim, stats: RelayStats) -> None:
        handler = self.handlers.get(claim.event_type)
        context = _ctx_for(claim)
        try:
            async with tenant_session(context) as tenant_work:
                event = (
                    await tenant_work.execute(
                        select(OutboxEvent)
                        .where(
                            OutboxEvent.tenant_id == claim.tenant_id,
                            OutboxEvent.id == claim.id,
                            OutboxEvent.event_id == claim.event_id,
                            OutboxEvent.status == OutboxStatus.PROCESSING.value,
                            OutboxEvent.processing_token == claim.processing_token,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if event is None:
                    stats.parked += 1
                    return

                if handler is None:
                    logger.warning(
                        "outbox_event_unhandled",
                        event_type=event.event_type,
                        event_id=str(event.event_id),
                    )
                    get_metrics().outbox_unhandled_total.labels(event_type=event.event_type).inc()
                    await mark_sent(tenant_work, event.id, processing_token=claim.processing_token)
                    stats.unhandled += 1
                    return

                from platform_core.config import get_settings

                settings = get_settings()
                remaining = claim.deadline_at - int(time.time())
                if remaining <= 0:
                    raise TimeoutError("OUTBOX_JOB_DEADLINE_EXHAUSTED")
                attempt_limit = min(
                    settings.outbox_handler_max_external_attempts,
                    _external_attempts_for_delivery(claim),
                )
                budget = ExecutionBudget.for_seconds(
                    deadline_seconds=min(settings.outbox_handler_deadline_seconds, remaining),
                    max_attempts=attempt_limit,
                    operation_limits={
                        "model": attempt_limit,
                        "tool": attempt_limit,
                        "outbound": attempt_limit,
                    },
                )
                with use_execution_budget(budget):
                    await handler(tenant_work, event)
                if not await mark_sent(
                    tenant_work, event.id, processing_token=claim.processing_token
                ):
                    raise RuntimeError("outbox claim was superseded before completion")
            stats.sent += 1
        except OutboxOutcomeUnknown:
            await self._settle_failed(claim, error="outbox_delivery_outcome_unknown", terminal=True)
            logger.error(
                "outbox_delivery_outcome_unknown",
                event_type=claim.event_type,
                event_id=str(claim.event_id),
                attempts=claim.attempts,
            )
            stats.failed += 1
        except Exception as exc:  # noqa: BLE001 - retry policy is explicit below
            deadline_exhausted = claim.deadline_at <= int(time.time())
            terminal = (
                claim.attempts >= claim.max_attempts
                or claim.event_type not in RETRY_SAFE_EVENT_TYPES
                or deadline_exhausted
            )
            await self._settle_failed(
                claim,
                error="outbox_job_deadline_exhausted" if deadline_exhausted else type(exc).__name__,
                terminal=terminal,
            )
            logger.warning(
                "outbox_delivery_failed",
                event_type=claim.event_type,
                event_id=str(claim.event_id),
                attempts=claim.attempts,
                error_code=type(exc).__name__,
            )
            stats.failed += 1

    async def _settle_failed(self, claim: OutboxClaim, *, error: str, terminal: bool) -> None:
        async with tenant_session(_ctx_for(claim)) as session:
            await mark_failed(
                session,
                claim.id,
                error,
                processing_token=claim.processing_token,
                terminal=terminal,
            )


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


async def handle_agent_reply(session: AsyncSession, event: OutboxEvent) -> None:
    """Deliver a human agent's reply over the channel the customer wrote in on.

    **Every read here is scoped by an explicit `tenant_id`.** The relay runs on
    the owner role (see `OutboxWorker.run_once`), so RLS filters nothing on this
    path and the `tenant_id` in the WHERE clause is the whole isolation. That is
    why this reads the turn by `(tenant_id, id)` rather than by id alone.

    Failure is raised, not swallowed. A reply that cannot be delivered must park
    the event where an operator can see it - the alternative is a message the
    customer never received and a queue that reported success, which is the
    `worker_cannot_send` shape this repository keeps finding.

    `command_id` is derived from the turn id for traceability. The channel
    providers do not all deduplicate that key, so an ambiguous result is parked
    for human review and is never automatically sent again.
    """
    import smtplib
    import uuid as _uuid

    import httpx
    from sqlalchemy import select as _select

    from platform_core.agent_runtime.models import ConversationTurn
    from platform_core.channels.outbound import (
        ChannelNotConfigured,
        build_channel_sender,
    )
    from platform_core.config import get_settings

    payload = event.payload if isinstance(event.payload, dict) else {}
    channel = str(payload.get("channel") or "").strip()
    address = str(payload.get("address") or "").strip()
    turn_id = str(payload.get("turn_id") or "").strip()
    conversation_key = str(payload.get("conversation_key") or "").strip()
    if not (channel and address and turn_id):
        # A malformed event is a producer bug. Raising parks it after the retry
        # budget rather than silently retiring a message that never went out.
        raise ValueError("agent reply event is missing its delivery target")

    text = (
        await session.execute(
            _select(ConversationTurn.text_redacted).where(
                ConversationTurn.tenant_id == event.tenant_id,
                ConversationTurn.id == _uuid.UUID(turn_id),
            )
        )
    ).scalar_one_or_none()
    if text is None:
        # The turn was pruned by retention, or the payload names the wrong one.
        raise ValueError(f"agent reply turn {turn_id} not found")

    sender = build_channel_sender(get_settings())
    if not sender.configured(channel):
        # A receive-only deployment. This is exactly the case that must not be
        # reported as delivered: the reply is persisted, `Workbench` shows it,
        # and the customer has heard nothing.
        raise ChannelNotConfigured(f"no outbound transport for {channel!r}")

    try:
        result = await sender.send_message(
            system=channel,
            address=address,
            conversation_key=conversation_key,
            content=str(text),
            command_id=f"agent-reply:{turn_id}",
        )
    except (httpx.TransportError, OSError, TimeoutError, smtplib.SMTPException) as exc:
        raise OutboxOutcomeUnknown("channel transport did not return a receipt") from exc
    if getattr(result, "ambiguous", False):
        raise OutboxOutcomeUnknown("channel delivery outcome is ambiguous")


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
    relay = OutboxRelay(
        batch=batch,
        excluded_event_types=(
            SHADOW_EVENT_TYPE,
            COPILOT_EVENT_TYPE,
            TASK_PLANNING_EVENT_TYPE,
            RELEASE_EVALUATION_REQUEST_EVENT,
            RELEASE_POST_TEST_REQUEST_EVENT,
        ),
    )
    relay.register("case.created", log_only_handler)
    relay.register("case.updated", log_only_handler)
    relay.register("usage.recorded", handle_usage_recorded)
    # A human's reply is the one event on this relay whose delivery the customer
    # is actively waiting for, so it gets a real handler rather than the
    # log-only default.
    relay.register("conversation.agent_reply", handle_agent_reply)
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
        """Commit global queue claims, then dispatch each event under tenant RLS.

        The owner connection reads only queue metadata and commits a fenced
        claim before work begins. Each handler reloads its payload on a
        tenant_session using the non-owner application role. Safe database
        events may be reclaimed after a crash; customer-visible replies are
        terminally parked as unknown rather than blindly sent again.
        """
        from worker.wiring import queue_bookkeeping_session

        async with queue_bookkeeping_session() as session:
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


def _ctx_for(claim: OutboxClaim) -> TenantContext:
    """The RLS context for an outbox claim.

    `actor_kind="system"`: the relay acts on behalf of no user. The actor is
    what audit trails attribute an action to, and claiming a user here would
    attribute a delivery to someone who did not make it.
    """
    return TenantContext(
        tenant_id=claim.tenant_id,
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
