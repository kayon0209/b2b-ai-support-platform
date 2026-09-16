"""Worker entrypoint and job loop (ticket 7, docs/architecture.md).

Worker classes per docs/deployment-and-operations.md:
- interactive: customer-visible AI work (inbox events -> agent runs)
- ingestion:   parsing/chunking/embedding (bulk, lowest priority)
- outbox:      relays committed business events to their consumers

All share the admission and priority logic in evaluation.queues, so
backpressure and starvation guarantees are identical to the tested
in-process model. Redis is the broker in a full deployment; the loop here
is deliberately transport-agnostic so the same code runs in tests.
"""

import asyncio
import signal
from dataclasses import dataclass

from observability import JsonLogger
from platform_core.agent_runtime.orchestrator import OrchestratorDeps
from platform_core.db import session_scope
from platform_core.evaluation.queues import PriorityQueueManager, Queue, QueueConfig
from worker.inbox_consumer import drain_once
from worker.outbox_relay import OutboxRelay, OutboxWorker, build_default_relay
from worker.wiring import audit_wiring, build_interactive_deps

logger = JsonLogger("platform.worker")

DEFAULT_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_BATCH = 20


@dataclass
class WorkerConfig:
    """Runtime knobs kept explicit so deployment can tune without code."""

    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    batch: int = DEFAULT_BATCH
    queue_config: dict[Queue, QueueConfig] | None = None


class InboxWorker:
    """Polls the transactional inbox and drives agent runs.

    The inbox (a database table) is the durable queue: work is never lost
    if the process dies, because the claim is a row status transition
    inside a transaction rather than an in-memory reservation.
    """

    def __init__(self, deps: OrchestratorDeps, config: WorkerConfig | None = None) -> None:
        self._deps = deps
        self._config = config or WorkerConfig()
        self._queues = PriorityQueueManager(self._config.queue_config or {})
        self._stopping = False

    @property
    def queues(self) -> PriorityQueueManager:
        return self._queues

    def request_stop(self) -> None:
        """Cooperative shutdown: finish the current batch, then exit."""
        self._stopping = True

    @property
    def stopping(self) -> bool:
        return self._stopping

    async def run_once(self) -> int:
        """One claim-and-process cycle against a fresh unit of work."""
        async with session_scope() as session:
            return await drain_once(session, deps=self._deps, batch=self._config.batch)

    async def run_forever(self) -> None:
        """Poll until stopped. Sleeps only when the queue was empty so
        backlog drains at full speed."""
        logger.info("worker_started", queue=Queue.INTERACTIVE.value)
        while not self._stopping:
            try:
                processed = await self.run_once()
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                logger.error("worker_cycle_failed", error_code=type(exc).__name__)
                await asyncio.sleep(self._config.poll_interval_seconds)
                continue
            if processed == 0:
                await asyncio.sleep(self._config.poll_interval_seconds)
        logger.info("worker_stopped", queue=Queue.INTERACTIVE.value)


def install_signal_handlers(worker: InboxWorker, loop: asyncio.AbstractEventLoop) -> None:
    """Graceful shutdown on SIGTERM/SIGINT where supported (POSIX)."""
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, worker.request_stop)
        except (NotImplementedError, AttributeError):  # pragma: no cover - Windows
            # Windows event loops do not support add_signal_handler; the
            # KeyboardInterrupt path in __main__ covers local use.
            continue


def main() -> None:
    """Local entrypoint: run the interactive worker + outbox relay together.

    Both loops run in one process because they are lightweight pollers and
    the outbox relay has no reason to be its own deployment unit until it
    becomes a throughput bottleneck. Either can be run alone by importing
    its class directly.

    `--outbox-only` is useful when the AI path is intentionally disabled
    (no LLM key) but business events still need to reach their consumers.
    """
    import sys

    outbox_only = "--outbox-only" in sys.argv

    if outbox_only:
        asyncio.run(_run_outbox_only())
        return

    # Real collaborators, assembled in one place. `build_interactive_deps`
    # raises when the chat provider is missing, so the worker fails to start
    # rather than claiming messages and silently never replying.
    deps = build_interactive_deps()
    wiring = audit_wiring(deps)
    logger.info("worker_wiring", **wiring.as_dict())
    if not wiring.can_send:
        # Not fatal, but loud: a run that cannot send still records its
        # outcome, and an operator must not read that as "the customer was
        # answered".
        logger.warning("worker_cannot_send", reason_code="NO_CHATWOOT_TOKEN")

    asyncio.run(_run_both(deps))


async def _run_both(deps: OrchestratorDeps) -> None:
    """Run the inbox worker and the outbox relay concurrently.

    Either loop failing must not take the other down: they own independent
    units of work, and a relay fault should not stop customer replies.
    """
    inbox = InboxWorker(deps)
    relay_worker = OutboxWorker(build_default_relay())

    loop = asyncio.get_running_loop()
    for worker in (inbox, relay_worker):
        install_signal_handlers(worker, loop)  # type: ignore[arg-type]

    try:
        await asyncio.gather(inbox.run_forever(), relay_worker.run_forever())
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        inbox.request_stop()
        relay_worker.request_stop()
        logger.info("worker_interrupted")


async def _run_outbox_only() -> None:
    relay_worker = OutboxWorker(build_default_relay())
    try:
        await relay_worker.run_forever()
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        relay_worker.request_stop()
        logger.info("worker_interrupted")


__all__ = [
    "InboxWorker",
    "OutboxRelay",
    "OutboxWorker",
    "WorkerConfig",
    "audit_wiring",
    "build_default_relay",
    "build_interactive_deps",
    "install_signal_handlers",
    "main",
]


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()
