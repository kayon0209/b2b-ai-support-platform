"""What happens when a shared dependency is not answering.

The gap
-------
`rate_limit.py` documents a deliberate decision: when Redis is unreachable the
limiter falls back to in-process buckets **rather than failing open**, because
failing open removes all protection at exactly the moment a shared dependency
is degraded - which is when abusive traffic is most likely to go unnoticed.

That decision is documented in prose and tested nowhere. The suite has 42 rate
limit tests and none of them construct a `HybridRateLimiter`, call
`build_limiter`, or make Redis raise. A refactor that kept the class name and
replaced the body with `return allow()` would have passed all 42.

The property that matters
-------------------------
Not "it does not crash" - a limiter that crashes still protects the upstream, it
just takes the whole API with it. The property is that **a degraded limiter
still limits**. The in-process bucket is per-process, so N replicas allow N times
the rate, and the module says so rather than implying otherwise. What it must
never do is allow without counting.
"""

from __future__ import annotations

import pytest

from platform_core.rate_limit import (
    HybridRateLimiter,
    InMemoryRateLimiter,
    RateLimitPolicy,
)


def _policy(capacity: int = 3) -> RateLimitPolicy:
    # , not : the field names the bucket's size, and calling
    # it  in a test invites asserting against the wrong one.
    return RateLimitPolicy(capacity=capacity, window_seconds=60)


class _AlwaysFails:
    """A shared counter that is down.

    Raises on every call, which is what a connection-refused Redis looks like to
    the caller. The exception type is deliberately varied per test below, because
    "only `ConnectionError` is handled" is itself a defect: a timeout, an auth
    failure and a protocol error all mean the same thing to the caller, and
    none of them is worth propagating into the request path.
    """

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls = 0

    def check(self, key: str, policy: RateLimitPolicy):  # noqa: ANN201 - test double
        self.calls += 1
        raise self._error


def test_a_degraded_limiter_still_limits() -> None:
    """The whole point. A fallback that stops counting is a fallback to allow.

    Asserted by exhausting the budget: if the local bucket were not being used,
    or were being bypassed, the calls past the limit would be allowed and this
    fails. It is the assertion a `degraded` flag alone could never satisfy.
    """
    limiter = HybridRateLimiter(_AlwaysFails(ConnectionError("redis down")), InMemoryRateLimiter())
    policy = _policy(capacity=3)

    allowed = [limiter.check("tenant-a", policy).allowed for _ in range(6)]

    assert allowed[:3] == [True] * 3, allowed
    assert allowed[3:] == [False] * 3, (
        "the limiter stopped counting while Redis was down - that is fail-open"
    )
    assert limiter.degraded is True


@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("connection refused"),
        TimeoutError("timed out"),
        RuntimeError("protocol error"),
        OSError("network unreachable"),
    ],
    ids=["refused", "timeout", "protocol", "unreachable"],
)
def test_every_way_redis_can_be_unavailable_degrades_rather_than_raising(
    error: Exception,
) -> None:
    """One exception type handled is not a fallback.

    Redis being unavailable has several shapes and only one of them is
    `ConnectionError`. A handler that catches exactly that one propagates the
    rest into the request path, where the outcome is a 500 on every request
    rather than a degraded-but-serving API.
    """
    limiter = HybridRateLimiter(_AlwaysFails(error), InMemoryRateLimiter())

    decision = limiter.check("tenant-a", _policy(capacity=1))

    assert decision.allowed is True, "the first request through a fresh budget"
    assert limiter.degraded is True


def test_the_limiter_recovers_when_redis_comes_back() -> None:
    """Degraded is a state, not a latch.

    A `degraded` flag that only ever goes true means the API has no idea whether
    it is currently protected by a shared counter, and an operator reading it has
    no way to tell a five-second blip from a permanently broken Redis.
    """
    shared = _AlwaysFails(ConnectionError("redis down"))
    limiter = HybridRateLimiter(shared, InMemoryRateLimiter())
    limiter.check("tenant-a", _policy())
    assert limiter.degraded is True

    # Redis answers again.
    recovered = InMemoryRateLimiter()
    limiter._shared = recovered  # noqa: SLF001 - exercising the recovery edge

    limiter.check("tenant-a", _policy())
    assert limiter.degraded is False, "still reporting degraded after Redis recovered"
    assert shared.calls == 1, "the failing counter was consulted after recovery"


def test_a_working_shared_counter_is_used_and_the_fallback_is_not() -> None:
    """The negative case, so the degradation tests cannot pass by accident.

    If the hybrid always used the local bucket, every test above would still
    pass - they only assert that limiting happens. This asserts the shared
    counter is actually consulted, which is what makes the limit shared across
    replicas rather than per-process.
    """
    shared = InMemoryRateLimiter()
    local = InMemoryRateLimiter()
    limiter = HybridRateLimiter(shared, local)
    policy = _policy(capacity=1)

    first = limiter.check("tenant-a", policy)
    second = limiter.check("tenant-a", policy)

    assert first.allowed is True
    assert second.allowed is False
    # The budget was spent on the shared counter. If it had been spent locally
    # this would not be visible, which is the point.
    assert local.check("tenant-a", policy).allowed is True, (
        "the fallback was used while Redis was healthy"
    )


def test_build_limiter_survives_a_url_that_cannot_be_constructed() -> None:
    """Construction must not raise on a bad URL.

    `build_limiter` runs during app startup. An exception here is a crash loop
    with no API at all, which is a worse outcome than a temporarily
    unprotected-but-serving one.
    """
    from platform_core.rate_limit import build_limiter

    limiter = build_limiter(redis_url="redis://127.0.0.1:1/0")

    assert limiter is not None
    decision = limiter.check("tenant-a", _policy(capacity=1))
    assert decision.allowed is True
