"""Worker entrypoint and job loop (ticket 7, docs/architecture.md).

Two worker classes per docs/deployment-and-operations.md:
- interactive: customer-visible AI work (inbox events -> agent runs)
- ingestion:   parsing/chunking/embedding (bulk, lowest priority)

Both share the admission and priority logic in evaluation.queues, so
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
    """Local entrypoint: run the interactive worker with settings-derived deps."""
    from platform_core.config import get_settings
    from platform_core.llm import GiteeAiClient

    settings = get_settings()
    if settings.llm_api_key is None:
        raise SystemExit("APP_LLM_API_KEY is required to run the worker")

    client = GiteeAiClient()
    deps = OrchestratorDeps(
        embedder=None,
        generator=None,
        sender=None,
        extra={"chat": client},
    )
    worker = InboxWorker(deps)
    try:
        asyncio.run(worker.run_forever())
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        logger.info("worker_interrupted")


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()
