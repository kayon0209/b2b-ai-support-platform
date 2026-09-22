"""Worker entrypoint and job loop (ticket 7, docs/architecture.md).

Worker classes per docs/deployment-and-operations.md:
- interactive: customer-visible AI work (inbox events -> agent runs)
- ingestion:   parsing/chunking/embedding (bulk, lowest priority)
- outbox:      relays committed business events to their consumers
- retention:   periodic per-tenant data-lifecycle sweep
- sla:         periodic per-tenant breach scan and escalation ladder

Backpressure is structural rather than an in-process queue: each class polls
its own table and claims a batch with `FOR UPDATE SKIP LOCKED`, and bulk work
runs in its own process, so an ingestion backlog can never occupy the
interactive worker's batch. Batch size and the idle-only backoff in the poll
loop are the remaining knobs.

`evaluation.queues` models an in-process priority queue and is unit-tested,
but it is deliberately NOT wired in here: a queue in one process's memory
cannot coordinate admission across worker processes, so the durable table is
the real queue.

Selection is by `APP_WORKER_QUEUE`. Before that variable was read, compose
declared an `ai-worker-ingestion` service with `APP_WORKER_QUEUE: ingestion`
and the process ignored it: the container came up, ran the *interactive*
worker, and no document was ever indexed. A misconfigured deployment that
starts cleanly and silently does the wrong job is the failure shape this
module exists to prevent, so an unknown queue name is fatal rather than a
fallback to the default.
"""

import asyncio
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from observability import JsonLogger
from platform_core.agent_runtime.orchestrator import OrchestratorDeps
from worker.inbox_consumer import drain_once
from worker.ingestion_consumer import drain_ingestion_once
from worker.outbox_relay import OutboxRelay, OutboxWorker, build_default_relay
from worker.retention_consumer import drain_retention_once
from worker.sla_consumer import drain_sla_once
from worker.wiring import (
    IngestionDeps,
    WorkerConfigurationError,
    audit_wiring,
    build_ingestion_deps,
    build_interactive_deps,
    queue_bookkeeping_session,
)

logger = JsonLogger("platform.worker")

DEFAULT_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_BATCH = 20

# Environment variable selecting which worker class this process runs.
QUEUE_ENV_VAR = "APP_WORKER_QUEUE"

# The worker roles this process can take. Spelled out here rather than
# borrowed from an enum so the dispatch table is readable on its own, and so
# an unknown value is fatal (see `resolve_queue`) instead of falling back to
# a default role.
ROLE_INTERACTIVE = "interactive"
ROLE_INGESTION = "ingestion"
ROLE_OUTBOX = "outbox"
# Retention is a periodic sweep, not a queue drain: it owns every tenant at
# once and runs on a long interval. Its own role so a slow sweep never delays
# customer replies.
ROLE_RETENTION = "retention"
ROLE_SLA = "sla"
WORKER_ROLES = (
    ROLE_INTERACTIVE,
    ROLE_INGESTION,
    ROLE_OUTBOX,
    ROLE_RETENTION,
    ROLE_SLA,
)


@dataclass
class WorkerConfig:
    """Runtime knobs kept explicit so deployment can tune without code."""

    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    batch: int = DEFAULT_BATCH
    # Consecutive failed cycles tolerated before the loop gives up. Without a
    # ceiling a persistent fault (unreachable database, revoked credential)
    # spins at full speed logging forever while the backlog grows - a green
    # process and an unbounded queue, which is the one combination no alert
    # fires on. The counter resets on any successful cycle, so transient
    # blips hours apart never accumulate into a shutdown.
    max_consecutive_failures: int = 10
    # Upper bound on the backoff applied between failed cycles, so recovery
    # is still noticed promptly once the dependency returns.
    max_backoff_seconds: float = 30.0


def _backoff_seconds(config: WorkerConfig, consecutive_failures: int) -> float:
    """Exponential backoff, capped so recovery stays responsive.

    `int ** int` is typed `Any` because the result depends on the sign of
    the exponent, so the multiplier is converted explicitly instead of
    letting `Any` flow into the declared `float` return.
    """
    base = float(config.poll_interval_seconds)
    cap = float(config.max_backoff_seconds)
    multiplier = float(2 ** (consecutive_failures - 1))
    return min(base * multiplier, cap)


async def _run_poll_loop(
    *,
    name: str,
    cycle: Callable[[], Awaitable[int]],
    config: WorkerConfig,
    stopping: Callable[[], bool],
) -> None:
    """Shared poll/backoff/drain loop for every worker class.

    Extracted so the failure policy is identical everywhere instead of
    reimplemented per worker and drifting. The contract each cycle must
    satisfy: return the number of items processed, or raise.

    Two properties, both load-bearing:

    - **Sleep only when idle.** A non-empty backlog drains at full speed;
      a fixed sleep would cap throughput at `batch / interval` regardless of
      how much work is queued.
    - **A bounded failure budget.** A persistent fault (unreachable database,
      revoked credential) would otherwise spin at full speed logging forever
      while the backlog grows - a green process and an unbounded queue, the
      one combination no alert fires on. `max_consecutive_failures` turns
      that into a loud exit. The counter resets on any successful cycle, so
      transient blips hours apart never accumulate into a shutdown.
    """
    logger.info("worker_started", queue=name)
    consecutive_failures = 0
    while not stopping():
        try:
            processed = await cycle()
        except Exception as exc:  # noqa: BLE001 - classified below
            consecutive_failures += 1
            logger.error(
                "worker_cycle_failed",
                queue=name,
                error_code=type(exc).__name__,
                consecutive_failures=consecutive_failures,
            )
            if consecutive_failures >= config.max_consecutive_failures:
                logger.error(
                    "worker_giving_up", queue=name, consecutive_failures=consecutive_failures
                )
                raise
            await asyncio.sleep(_backoff_seconds(config, consecutive_failures))
            continue
        consecutive_failures = 0
        if processed == 0:
            await asyncio.sleep(config.poll_interval_seconds)
    logger.info("worker_stopped", queue=name)


class InboxWorker:
    """Polls the transactional inbox and drives agent runs.

    The inbox (a database table) is the durable queue: work is never lost
    if the process dies, because the claim is a row status transition
    inside a transaction rather than an in-memory reservation.
    """

    def __init__(self, deps: OrchestratorDeps, config: WorkerConfig | None = None) -> None:
        self._deps = deps
        self._config = config or WorkerConfig()
        self._stopping = False

    def request_stop(self) -> None:
        """Cooperative shutdown: finish the current batch, then exit."""
        self._stopping = True

    @property
    def stopping(self) -> bool:
        return self._stopping

    async def run_once(self) -> int:
        """One claim-and-process cycle against a fresh unit of work.

        The session opened here is the queue's **bookkeeping** session - the
        owner role, because a claim runs before any tenant is known. It is not
        where the run happens: `drain_once` opens a `tenant_session` per event,
        so the agent reaches tenant data as `platform_app` with RLS enforced.
        See `worker.wiring.queue_bookkeeping_session` for why one role cannot do
        both jobs.
        """
        async with queue_bookkeeping_session() as bookkeeping:
            return await drain_once(bookkeeping, deps=self._deps, batch=self._config.batch)

    async def run_forever(self) -> None:
        """Poll until stopped, with the shared drain/backoff policy."""
        await _run_poll_loop(
            name=ROLE_INTERACTIVE,
            cycle=self.run_once,
            config=self._config,
            stopping=lambda: self._stopping,
        )


class IngestionWorker:
    """Polls `document_versions` and runs the ingestion pipeline.

    Same durable-queue shape as `InboxWorker`: the queue is the table, the
    claim is a committed row write, and a crash mid-ingest is recovered by
    `reclaim_stale_ingestion` rather than by an in-memory retry.

    Batch size defaults lower than the inbox worker's because ingestion is
    the bulk, lowest-priority class (docs/deployment-and-operations.md):
    each claimed document costs an external storage read and one or more
    embedding calls, so a large batch would hold a worker for minutes and
    delay the interactive path's ability to make progress.
    """

    def __init__(self, deps: IngestionDeps, config: WorkerConfig | None = None) -> None:
        self._deps = deps
        self._config = config or WorkerConfig(batch=5)
        self._stopping = False

    def request_stop(self) -> None:
        self._stopping = True

    @property
    def stopping(self) -> bool:
        return self._stopping

    async def run_once(self) -> int:
        from platform_core.db import session_scope_with_url
        from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant
        from worker.wiring import app_role_url

        # The app (non-bypass) role, so RLS is a real boundary on this path
        # rather than decoration. The worker has no tenant of its own - it
        # discovers one from each row it claims - so the RLS setting is
        # applied per claimed version inside `drain_ingestion_once`, and the
        # claim query itself runs before any tenant is set.
        del TenantContext, apply_rls_tenant  # applied per claimed version
        async with session_scope_with_url(app_role_url()) as session:
            stats = await drain_ingestion_once(
                session,
                embedder=self._deps.embedder,
                batch=self._config.batch,
            )
            if stats.reclaimed:
                logger.warning("ingestion_reclaimed", count=stats.reclaimed)
            return stats.processed

    async def run_forever(self) -> None:
        await _run_poll_loop(
            name=ROLE_INGESTION,
            cycle=self.run_once,
            config=self._config,
            stopping=lambda: self._stopping,
        )


class RetentionWorker:
    """Periodically applies the tenant retention policy.

    Not a queue drain: there is no work item to claim, so it sweeps every
    active tenant each cycle. The interval is long (retention is measured in
    days, not seconds) and the sweep is idempotent, so a missed cycle is
    harmless and a repeated one changes nothing.
    """

    # Once an hour is far more often than the policy needs and still cheap:
    # three indexed deletes/updates per tenant.
    DEFAULT_INTERVAL_SECONDS = 3600.0

    def __init__(self, config: WorkerConfig | None = None) -> None:
        self._config = config or WorkerConfig(
            poll_interval_seconds=self.DEFAULT_INTERVAL_SECONDS, batch=1
        )
        self._stopping = False

    def request_stop(self) -> None:
        self._stopping = True

    @property
    def stopping(self) -> bool:
        return self._stopping

    async def run_once(self) -> int:
        stats = await drain_retention_once()
        return stats.changed_rows

    async def run_forever(self) -> None:
        await _run_poll_loop(
            name=ROLE_RETENTION,
            cycle=self.run_once,
            config=self._config,
            stopping=lambda: self._stopping,
        )


class SlaWorker:
    """Periodically scans for breached SLA clocks and walks the ladder.

    A minute, not the hour the retention sweep uses: a breach is actionable
    now, and the ladder's second rung is an hour past the deadline, so an
    interval longer than that would report level 2 and level 1 in the same
    sweep - the record would be right and the notification would be useless.

    The sweep is idempotent by construction (`case_escalations`' unique key),
    so a repeated cycle changes nothing and a missed one is caught up on the
    next pass because the level is derived from the deadline rather than from
    the previous level.
    """

    DEFAULT_INTERVAL_SECONDS = 60.0

    def __init__(self, config: WorkerConfig | None = None) -> None:
        self._config = config or WorkerConfig(
            poll_interval_seconds=self.DEFAULT_INTERVAL_SECONDS, batch=1
        )
        self._stopping = False

    def request_stop(self) -> None:
        self._stopping = True

    @property
    def stopping(self) -> bool:
        return self._stopping

    async def run_once(self) -> int:
        stats = await drain_sla_once()
        return stats.escalated

    async def run_forever(self) -> None:
        await _run_poll_loop(
            name=ROLE_SLA,
            cycle=self.run_once,
            config=self._config,
            stopping=lambda: self._stopping,
        )


def install_signal_handlers(worker: Any, loop: asyncio.AbstractEventLoop) -> None:
    """Graceful shutdown on SIGTERM/SIGINT where supported (POSIX)."""
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, worker.request_stop)
        except (NotImplementedError, AttributeError):  # pragma: no cover - Windows
            # Windows event loops do not support add_signal_handler; the
            # KeyboardInterrupt path in __main__ covers local use.
            continue


def resolve_queue(argv: list[str] | None = None) -> str:
    """Which worker class this process should run.

    `--outbox-only` wins over the environment because it is the more
    specific instruction (a one-shot operational run), and it predates the
    env var. An unknown value is fatal: a typo in a deployment manifest must
    not silently downgrade to the interactive worker, which would consume
    customer messages instead of indexing documents.
    """
    args = argv if argv is not None else []
    if "--outbox-only" in args:
        return ROLE_OUTBOX
    raw = (os.environ.get(QUEUE_ENV_VAR) or "").strip().lower()
    if not raw:
        return ROLE_INTERACTIVE
    if raw not in WORKER_ROLES:
        raise WorkerConfigurationError(
            f"{QUEUE_ENV_VAR}={raw!r} is not a known worker queue; "
            f"expected one of: {', '.join(WORKER_ROLES)}"
        )
    return raw


def run(coro: "Awaitable[None]") -> None:
    """`asyncio.run` with a loop psycopg can actually use.

    On Windows `asyncio.run` builds a ProactorEventLoop, which psycopg's
    async driver refuses — every DB call dies at connect. The API entrypoint
    already selects a selector loop; the worker did not, so it could not
    start at all on Windows and failed its first poll cycle with
    `RuntimeError: psycopg async cannot run on a ProactorEventLoop`.
    """
    if sys.platform == "win32":
        asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)  # type: ignore[arg-type]
    else:
        asyncio.run(coro)


def main() -> None:
    """Local entrypoint, dispatched by `APP_WORKER_QUEUE`.

    The interactive worker and the outbox relay share one process because
    both are lightweight pollers and the relay has no reason to be its own
    deployment unit until it becomes a throughput bottleneck. Ingestion is
    its own process: it is bulk work whose batches would otherwise compete
    with customer-facing runs for the same event loop.
    """
    try:
        queue = resolve_queue(sys.argv[1:])
    except WorkerConfigurationError as exc:
        # `error_code`, not the message: the allowlist drops free text, and the
        # readable text is not lost by doing so - this exception subclasses
        # SystemExit (see `wiring.WorkerConfigurationError`), so re-raising it
        # prints the message to stderr immediately below this line. What the
        # structured field has to carry is the part that gets grepped.
        logger.error("worker_misconfigured", error_code=type(exc).__name__)
        raise

    if queue == ROLE_OUTBOX:
        run(_run_outbox_only())
        return

    if queue == ROLE_INGESTION:
        ingestion_deps = build_ingestion_deps()
        logger.info("worker_wiring", queue=queue, has_embedding=ingestion_deps.can_embed)
        run(_run_ingestion_only(ingestion_deps))
        return

    if queue == ROLE_RETENTION:
        # No wiring: the sweep needs no external collaborator.
        logger.info("worker_wiring", queue=queue)
        run(_run_retention_only())
        return

    if queue == ROLE_SLA:
        # No wiring either: the breach scan reads Cases and writes its own
        # ledger. It notifies through the outbox, which is the relay's job.
        logger.info("worker_wiring", queue=queue)
        run(_run_sla_only())
        return

    # Real collaborators, assembled in one place. `build_interactive_deps`
    # raises when the chat provider is missing, so the worker fails to start
    # rather than claiming messages and silently never replying.
    interactive_deps = build_interactive_deps()
    wiring = audit_wiring(interactive_deps)
    logger.info("worker_wiring", queue=queue, **wiring.as_dict())
    if not wiring.can_send:
        # Not fatal, but loud: a run that cannot send still records its
        # outcome, and an operator must not read that as "the customer was
        # answered".
        logger.warning("worker_cannot_send", reason_code="NO_CHATWOOT_TOKEN")

    run(_run_both(interactive_deps))


async def _run_both(deps: OrchestratorDeps) -> None:
    """Run the inbox worker and the outbox relay concurrently.

    Either loop failing must not take the other down: they own independent
    units of work, and a relay fault should not stop customer replies.
    """
    inbox = InboxWorker(deps)
    relay_worker = OutboxWorker(build_default_relay())

    loop = asyncio.get_running_loop()
    for worker in (inbox, relay_worker):
        install_signal_handlers(worker, loop)

    try:
        await asyncio.gather(inbox.run_forever(), relay_worker.run_forever())
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        inbox.request_stop()
        relay_worker.request_stop()
        logger.info("worker_interrupted")


async def _run_ingestion_only(deps: IngestionDeps) -> None:
    worker = IngestionWorker(deps)
    loop = asyncio.get_running_loop()
    install_signal_handlers(worker, loop)
    try:
        await worker.run_forever()
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        worker.request_stop()
        logger.info("worker_interrupted", queue=ROLE_INGESTION)


async def _run_retention_only() -> None:
    worker = RetentionWorker()
    loop = asyncio.get_running_loop()
    install_signal_handlers(worker, loop)
    try:
        await worker.run_forever()
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        worker.request_stop()
        logger.info("worker_interrupted", queue=ROLE_RETENTION)


async def _run_sla_only() -> None:
    worker = SlaWorker()
    loop = asyncio.get_running_loop()
    install_signal_handlers(worker, loop)
    try:
        await worker.run_forever()
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        worker.request_stop()
        logger.info("worker_interrupted", queue=ROLE_SLA)


async def _run_outbox_only() -> None:
    relay_worker = OutboxWorker(build_default_relay())
    try:
        await relay_worker.run_forever()
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        relay_worker.request_stop()
        logger.info("worker_interrupted")


__all__ = [
    "IngestionWorker",
    "InboxWorker",
    "OutboxRelay",
    "OutboxWorker",
    "WorkerConfig",
    "audit_wiring",
    "build_default_relay",
    "build_ingestion_deps",
    "build_interactive_deps",
    "install_signal_handlers",
    "resolve_queue",
    "main",
    "resolve_queue",
]


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()
