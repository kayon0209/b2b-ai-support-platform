"""Unit tests: worker job loop (pilot failure-injection gate).

docs/deployment-and-operations.md requires that losing an external
dependency degrades the worker rather than killing it, and that admitted
work is never lost. Two properties are load-bearing and were previously
untested:

1. **Outage survival.** The durable queue is a database table, not the
   broker, so a broker/cache outage must not lose claimed work. The loop
   has to survive a failing cycle and keep polling.

2. **No hot error loop.** `run_forever` swallows every exception so one
   bad message cannot kill the worker. Left unqualified that also means a
   *persistent* fault - an unreachable database, a revoked credential -
   spins as fast as the CPU allows, logging forever while the backlog
   grows. The loop must back off, and it must eventually give up loudly
   rather than swallow the fault indefinitely.
"""

import asyncio

import pytest

from worker.runner import InboxWorker, WorkerConfig


def _run(coro):
    return asyncio.run(coro)


class _StubDeps:
    """Stands in for OrchestratorDeps; the loop never calls into it here."""

    def __getattr__(self, name: str) -> object:  # pragma: no cover
        raise AssertionError(f"deps.{name} should not be touched")


def _worker(**config) -> InboxWorker:
    config.setdefault("poll_interval_seconds", 0.001)
    return InboxWorker(_StubDeps(), WorkerConfig(**config))


def test_a_failing_cycle_does_not_kill_the_worker(monkeypatch) -> None:
    """A dependency outage must be survivable, not fatal."""
    worker = _worker()
    attempts: list[int] = []

    async def boom(self):
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("broker unreachable")
        worker.request_stop()
        return 0

    monkeypatch.setattr(InboxWorker, "run_once", boom)

    _run(asyncio.wait_for(worker.run_forever(), timeout=5))

    assert len(attempts) == 3, "the loop stopped instead of surviving the outage"


def test_a_persistent_fault_is_not_swallowed_forever(monkeypatch) -> None:
    """A fault that never clears must surface, not spin silently.

    Without a consecutive-failure ceiling the worker logs an error every
    poll interval indefinitely. An operator sees a green process and a
    growing backlog - the worst combination, because nothing pages.
    """
    worker = _worker(max_consecutive_failures=3)
    attempts: list[int] = []

    async def always_boom(self):
        attempts.append(1)
        raise ConnectionError("database down")

    monkeypatch.setattr(InboxWorker, "run_once", always_boom)

    with pytest.raises(ConnectionError):
        _run(asyncio.wait_for(worker.run_forever(), timeout=5))

    assert len(attempts) == 3, "the worker kept retrying past its failure budget"


def test_a_recovered_dependency_resets_the_failure_budget(monkeypatch) -> None:
    """Transient blips must not accumulate into a shutdown.

    The ceiling counts *consecutive* failures. A worker that has served a
    successful cycle is healthy again, so an occasional failure hours
    apart must never trip it.
    """
    worker = _worker(max_consecutive_failures=3)
    outcomes: list[str] = []

    async def flaky(self):
        outcomes.append("call")
        # fail, succeed, fail, succeed, fail, then stop cleanly
        if len(outcomes) % 2 == 1:
            if len(outcomes) >= 5:
                worker.request_stop()
                return 0
            raise ConnectionError("blip")
        return 1

    monkeypatch.setattr(InboxWorker, "run_once", flaky)

    _run(asyncio.wait_for(worker.run_forever(), timeout=5))

    assert len(outcomes) == 5, "a recovered worker was shut down by stale failures"


def test_empty_queue_sleeps_between_polls(monkeypatch) -> None:
    """An idle queue must not busy-spin the CPU."""
    worker = _worker(poll_interval_seconds=0.05)
    polls: list[int] = []

    async def empty(self):
        polls.append(1)
        if len(polls) >= 3:
            worker.request_stop()
        return 0

    monkeypatch.setattr(InboxWorker, "run_once", empty)

    async def scenario() -> float:
        loop = asyncio.get_running_loop()
        began = loop.time()
        await worker.run_forever()
        return loop.time() - began

    elapsed = _run(scenario())
    assert len(polls) == 3
    assert elapsed >= 0.1, "an idle worker did not sleep between empty polls"
