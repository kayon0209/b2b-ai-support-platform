"""Integration tests: the shared (Redis) rate-limit counter.

These exist because the reason for using Redis cannot be observed inside one
process. An in-process limiter passes every unit test and still allows N times
the configured rate on N replicas, so the property worth pinning is that two
limiter instances - standing in for two pods - spend from one bucket.
"""

import os
import uuid

import pytest
import redis as redis_lib

from platform_core.rate_limit import (
    HybridRateLimiter,
    InMemoryRateLimiter,
    RateLimitPolicy,
    RedisRateLimiter,
    build_limiter,
)

pytestmark = pytest.mark.integration

REDIS_URL = os.environ.get("APP_REDIS_URL", "redis://localhost:6380/0")


@pytest.fixture
def client():
    c = redis_lib.Redis.from_url(REDIS_URL, socket_connect_timeout=2, decode_responses=True)
    try:
        c.ping()
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"redis not reachable: {exc}")
    yield c
    c.close()


@pytest.fixture
def key():
    """A unique bucket key, so a parallel run cannot perturb this one."""
    return f"ratelimit:test:{uuid.uuid4()}"


def test_two_processes_share_one_bucket(client, key) -> None:
    """The whole reason Redis is in the request path.

    With per-process buckets, N replicas allow N times the configured rate -
    the limit would be a property of the deployment's size rather than of the
    tenant's entitlement.
    """
    first = RedisRateLimiter(client)
    second = RedisRateLimiter(client)
    policy = RateLimitPolicy(capacity=3, window_seconds=60)

    outcomes = [first.check(key, policy).allowed for _ in range(3)]
    outcomes.append(second.check(key, policy).allowed)

    assert outcomes == [True, True, True, False], outcomes


def test_the_counter_expires(client, key) -> None:
    """A rate-limit key that outlives its window is an unbounded Redis growth
    path - one key per tenant per scope, forever."""
    RedisRateLimiter(client).check(key, RateLimitPolicy(capacity=5, window_seconds=60))

    ttl_ms = client.pttl(key)

    assert ttl_ms > 0, "the key must expire"
    # Two windows, so a bucket survives a quiet period without living forever.
    assert ttl_ms <= 120_000


def test_the_refusal_carries_a_usable_retry_after(client, key) -> None:
    limiter = RedisRateLimiter(client)
    policy = RateLimitPolicy(capacity=1, window_seconds=2)

    assert limiter.check(key, policy).allowed is True
    refused = limiter.check(key, policy)

    assert refused.allowed is False
    assert refused.retry_after_seconds >= 1


def test_the_production_builder_uses_redis_when_it_answers() -> None:
    limiter = build_limiter(redis_url=REDIS_URL)

    assert isinstance(limiter, HybridRateLimiter)
    assert isinstance(limiter._shared, RedisRateLimiter)


class _BrokenShared:
    """A shared limiter whose store is unreachable."""

    def check(self, key: str, policy: RateLimitPolicy):
        raise ConnectionError("redis is down")


def test_a_redis_outage_degrades_to_local_buckets_rather_than_failing_open() -> None:
    """Failing open would remove all protection exactly when a shared
    dependency is degraded - which is when abusive traffic is most likely to
    go unnoticed. The fallback is weaker (per process) and that is stated, not
    implied."""
    local = InMemoryRateLimiter()
    limiter = HybridRateLimiter(_BrokenShared(), local)
    policy = RateLimitPolicy(capacity=1, window_seconds=60)

    assert limiter.check("k", policy).allowed is True
    assert limiter.check("k", policy).allowed is False
    assert limiter.degraded is True


def test_recovery_returns_to_the_shared_counter() -> None:
    local = InMemoryRateLimiter()
    limiter = HybridRateLimiter(_BrokenShared(), local)
    policy = RateLimitPolicy(capacity=5, window_seconds=60)
    limiter.check("k", policy)
    assert limiter.degraded is True

    limiter._shared = InMemoryRateLimiter()  # stands in for Redis recovering
    limiter.check("k", policy)

    assert limiter.degraded is False
