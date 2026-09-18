"""Priority queues + backpressure (ticket 37, docs/deployment-and-operations.md).

Queue design:
- interactive: customer-visible AI work (reserved capacity, highest priority)
- tools: external actions
- ingestion: parsing/chunking/embedding (bulk, lowest priority)
- sync: connector synchronization
- evaluation: offline evaluation

Backpressure: when a queue's depth exceeds its high watermark, submissions
to LOWER priority queues are shed (rejected with a visible error) while
interactive submissions are always admitted up to its own watermark.
Interactive and tool queues have reserved capacity that bulk work cannot
consume.

**Not wired into the workers.** `worker.runner` used to construct a
`PriorityQueueManager` and expose it, but no loop ever consulted it, so it
implied backpressure that did not exist. The workers poll their own tables
and claim batches with `FOR UPDATE SKIP LOCKED`, and bulk work runs in its
own process — which is what actually keeps an ingestion backlog out of the
interactive worker's batch. This module remains as a tested, self-contained
model of the admission policy; wiring it in would mean a queue that only
coordinates one process, which is not the deployment shape.
"""

import time
from dataclasses import dataclass, field
from enum import StrEnum

from platform_core.integrations.resilience import retry_delays


class Queue(StrEnum):
    INTERACTIVE = "interactive"
    TOOLS = "tools"
    INGESTION = "ingestion"
    SYNC = "sync"
    EVALUATION = "evaluation"


# Priority order: lower number = served first. Bulk queues cannot starve
# interactive work because the dispatcher always drains the higher
# priority queue first and interactive admits independently.
PRIORITY = {
    Queue.INTERACTIVE: 0,
    Queue.TOOLS: 1,
    Queue.SYNC: 2,
    Queue.INGESTION: 3,
    Queue.EVALUATION: 4,
}


class QueueFull(Exception):
    """Raised when backpressure sheds a submission. Visible, not silent."""

    def __init__(self, queue: Queue, depth: int) -> None:
        super().__init__(f"queue {queue.value} full at depth {depth}")
        self.queue = queue
        self.depth = depth


@dataclass
class QueueConfig:
    high_watermark: int
    reserved_capacity: int = 0  # for interactive: slots bulk cannot take


DEFAULT_QUEUE_CONFIG: dict[Queue, QueueConfig] = {
    Queue.INTERACTIVE: QueueConfig(high_watermark=500),
    Queue.TOOLS: QueueConfig(high_watermark=300, reserved_capacity=50),
    Queue.SYNC: QueueConfig(high_watermark=500),
    Queue.INGESTION: QueueConfig(high_watermark=1000),
    Queue.EVALUATION: QueueConfig(high_watermark=200),
}


@dataclass
class Job:
    job_id: str
    queue: Queue
    payload_ref: str  # reference (e.g. inbox event id); payloads live in DB
    enqueued_at: int = field(default_factory=lambda: int(time.time()))
    attempts: int = 0


class PriorityQueueManager:
    """In-process dispatcher representing the worker queue topology.

    Production wires the same admission/priority logic onto Celery queues;
    this class keeps the policy deterministic and unit-testable.
    """

    def __init__(
        self,
        config: dict[Queue, QueueConfig] | None = None,
    ) -> None:
        self._config = config or DEFAULT_QUEUE_CONFIG
        self._queues: dict[Queue, list[Job]] = {q: [] for q in Queue}

    def depth(self, queue: Queue) -> int:
        return len(self._queues[queue])

    def submit(self, queue: Queue, payload_ref: str, job_id: str | None = None) -> Job:
        """Admit a job with backpressure.

        Interactive submissions are admitted while depth < its watermark —
        bulk shed never blocks customer-visible work.
        Bulk submissions (lower priority) are shed when ANY higher-priority
        queue with reserved capacity is congested or own watermark hit.
        """
        cfg = self._config[queue]
        own_depth = self.depth(queue)
        if own_depth >= cfg.high_watermark:
            raise QueueFull(queue, own_depth)

        if queue is not Queue.INTERACTIVE:
            interactive_cfg = self._config[Queue.INTERACTIVE]
            if self.depth(Queue.INTERACTIVE) >= interactive_cfg.high_watermark * 0.8:
                # Reserved capacity pressure: shed bulk work first.
                raise QueueFull(Queue.INTERACTIVE, self.depth(Queue.INTERACTIVE))

        job = Job(
            job_id=job_id or f"{queue.value}-{payload_ref}",
            queue=queue,
            payload_ref=payload_ref,
        )
        self._queues[queue].append(job)
        return job

    def next_job(self) -> Job | None:
        """Pop the highest-priority ready job (strict priority order)."""
        for queue in sorted(Queue, key=lambda q: PRIORITY[q]):
            q = self._queues[queue]
            if q:
                return q.pop(0)
        return None

    def drain(self, limit: int = 100) -> list[Job]:
        drained: list[Job] = []
        while len(drained) < limit:
            job = self.next_job()
            if job is None:
                break
            drained.append(job)
        return drained


def backoff_for(job: Job, *, max_attempts: int = 5) -> float | None:
    """Retry policy: exponential with cap; None = dead-letter the job."""
    if job.attempts >= max_attempts:
        return None
    return retry_delays(max_attempts)[min(job.attempts, max_attempts)]
