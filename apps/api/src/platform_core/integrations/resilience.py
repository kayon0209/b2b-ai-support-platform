"""Shared resilience primitives: circuit breaker + retry schedule.

Extracted from the Chatwoot client so the connector SDK reuses identical
semantics (docs/integrations.md connector execution behavior).
"""

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpen(Exception):
    pass


@dataclass
class CircuitBreaker:
    """N consecutive failures open the circuit for a cooldown; one allowed
    probe in half-open, whose failure re-opens."""

    failure_threshold: int = 5
    cooldown_seconds: float = 30.0
    _consecutive_failures: int = 0
    _opened_at: float = 0.0
    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            if time.monotonic() - self._opened_at >= self.cooldown_seconds:
                return CircuitState.HALF_OPEN
        return self._state

    def before_call(self) -> None:
        if self.state == CircuitState.OPEN:
            raise CircuitOpen()

    def on_success(self) -> None:
        self._consecutive_failures = 0
        self._state = CircuitState.CLOSED

    def on_failure(self) -> None:
        self._consecutive_failures += 1
        if self._state == CircuitState.HALF_OPEN:
            self._open()
        elif self._consecutive_failures >= self.failure_threshold:
            self._open()

    def _open(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = time.monotonic()


def retry_delays(
    max_retries: int,
    *,
    base: float = 0.25,
    cap: float = 4.0,
    jitter_ratio: float = 0.0,
    rng: Any | None = None,
) -> list[float]:
    """Exponential backoff: 0.25, 0.5, 1, 2, 4... capped.

    `jitter_ratio` (plan 5.3) scales each delay by a random factor in
    [1 - ratio, 1]. Without jitter every caller retries on the same beat and
    the retry storm is synchronised - the failure that takes down a
    provider's recovery window exactly when it reopens. `rng` is injectable
    so tests can assert on deterministic sequences; production uses random.
    """
    import random

    random_source = rng if rng is not None else random
    delays = [min(base * (2**attempt), cap) for attempt in range(max_retries + 1)]
    if jitter_ratio <= 0:
        return delays
    return [delay * (1.0 - jitter_ratio * random_source.random()) for delay in delays]


def _typing_placeholder() -> None:  # pragma: no cover - keeps Any import honest
    return None
