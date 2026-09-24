"""Unit tests: token bucket, key derivation and the rate-limit middleware.

No I/O and no database: the limiter's arithmetic and the middleware's decision
are deterministic, so they are verified here rather than through the network.
The one place Redis is involved gets its own integration test
(`tests/integration/test_rate_limit_redis.py`) because the point there is
sharing, which cannot be observed in one process.
"""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from platform_core.rate_limit import (
    UNLIMITED_PATHS,
    InMemoryRateLimiter,
    RateLimitMiddleware,
    RateLimitPolicy,
    TokenBucket,
    bucket_key,
)

# --- policy ---------------------------------------------------------------


def test_capacity_must_be_positive() -> None:
    with pytest.raises(ValueError):
        RateLimitPolicy(capacity=0, window_seconds=60)


def test_window_must_be_positive() -> None:
    with pytest.raises(ValueError):
        RateLimitPolicy(capacity=10, window_seconds=0)


def test_refill_rate_is_capacity_over_window() -> None:
    assert RateLimitPolicy(capacity=60, window_seconds=60).refill_per_second == 1.0


def test_workbench_read_budget_is_configurable_and_independent_of_api_writes() -> None:
    from types import SimpleNamespace

    from platform_core.rate_limit import policies_from_settings

    policies = policies_from_settings(
        SimpleNamespace(
            rate_limit_window_seconds=60,
            rate_limit_requests=600,
            rate_limit_workbench_requests=24000,
            rate_limit_anonymous_requests=300,
            rate_limit_webhook_requests=1200,
        )
    )
    assert policies["api"].capacity == 600
    assert policies["workbench"].capacity == 24000


# --- token bucket ---------------------------------------------------------


def test_bucket_allows_exactly_its_capacity() -> None:
    policy = RateLimitPolicy(capacity=3, window_seconds=60)
    bucket = TokenBucket(policy)

    outcomes = [bucket.take(now=1000.0).allowed for _ in range(4)]

    assert outcomes == [True, True, True, False]


def test_retry_after_is_time_until_one_token_refills() -> None:
    """3 tokens per 60s means one token every 20s, so a caller that exhausted
    the bucket waits 20s - not a whole window, which is what a fixed window
    would tell it."""
    bucket = TokenBucket(RateLimitPolicy(capacity=3, window_seconds=60))
    for _ in range(3):
        bucket.take(now=1000.0)

    decision = bucket.take(now=1000.0)

    assert decision.allowed is False
    assert decision.retry_after_seconds == 20


def test_retry_after_is_never_zero() -> None:
    """`Retry-After: 0` invites an immediate retry that is guaranteed to fail
    again, so the floor is one second."""
    bucket = TokenBucket(RateLimitPolicy(capacity=1, window_seconds=3600))
    bucket.take(now=0.0)

    decision = bucket.take(now=0.0)

    assert decision.allowed is False
    assert decision.retry_after_seconds >= 1


def test_tokens_refill_over_time() -> None:
    bucket = TokenBucket(RateLimitPolicy(capacity=2, window_seconds=2))
    bucket.take(now=0.0)
    bucket.take(now=0.0)
    assert bucket.take(now=0.0).allowed is False

    assert bucket.take(now=1.0).allowed is True


def test_a_long_idle_period_does_not_exceed_capacity() -> None:
    """A bucket that accumulated interest would let a client that was quiet
    for an hour fire an hour's worth of requests at once."""
    bucket = TokenBucket(RateLimitPolicy(capacity=2, window_seconds=2))
    bucket.take(now=0.0)

    # Ten windows of idleness, then a burst.
    outcomes = [bucket.take(now=10.0).allowed for _ in range(4)]

    assert outcomes == [True, True, False, False]


def test_the_clock_moving_backwards_does_not_create_tokens() -> None:
    """`time.monotonic` should not go backwards, but a negative elapsed would
    subtract tokens and lock a client out for no reason."""
    bucket = TokenBucket(RateLimitPolicy(capacity=1, window_seconds=60))
    bucket.take(now=100.0)

    decision = bucket.take(now=50.0)

    assert decision.allowed is False


# --- in-memory limiter ----------------------------------------------------


def test_separate_keys_have_separate_buckets() -> None:
    """The whole point of keying by tenant: one caller's spending must not
    consume another's budget."""
    limiter = InMemoryRateLimiter()
    policy = RateLimitPolicy(capacity=1, window_seconds=60)

    assert limiter.check("tenant:a", policy).allowed is True
    assert limiter.check("tenant:a", policy).allowed is False
    assert limiter.check("tenant:b", policy).allowed is True


def test_a_different_policy_replaces_the_bucket() -> None:
    """Otherwise a policy change (a smaller limit) would be ignored until the
    old bucket drained - the limit would not take effect when it mattered.

    The discriminating assertion is the *first* check under the new policy:
    a fresh bucket has one token and allows it, an exhausted three-token
    bucket would refuse it.
    """
    limiter = InMemoryRateLimiter()
    small = RateLimitPolicy(capacity=1, window_seconds=60)
    for _ in range(5):
        limiter.check("k", RateLimitPolicy(capacity=5, window_seconds=60))

    first = limiter.check("k", small)

    assert first.allowed is True, "the new policy must take effect immediately"
    assert first.remaining == 0
    assert limiter.check("k", small).allowed is False


def test_the_bucket_map_is_bounded() -> None:
    """A dict keyed by client address is an unbounded memory map under a
    distributed source of addresses."""
    limiter = InMemoryRateLimiter(max_keys=10)
    policy = RateLimitPolicy(capacity=5, window_seconds=60)

    for i in range(50):
        limiter.check(f"addr:10.0.0.{i}", policy)

    assert len(limiter._buckets) <= 10


# --- key derivation -------------------------------------------------------


def _request(path: str, host: str | None = "10.0.0.9", *, method: str = "GET") -> Request:
    client = (host, 12345) if host else None
    scope = {"type": "http", "path": path, "headers": [], "client": client, "method": method}
    return Request(scope)


def test_a_resolved_tenant_keys_by_tenant() -> None:
    key = bucket_key(_request("/v1/cases"), tenant_id="t-1")
    assert key == "ratelimit:api:tenant:t-1"


def test_an_unresolved_caller_keys_by_address() -> None:
    key = bucket_key(_request("/v1/cases"), tenant_id=None)
    assert key == "ratelimit:api:addr:10.0.0.9"


def test_webhook_traffic_gets_its_own_scope() -> None:
    """Webhooks arrive before a tenant exists and in provider-paced bursts, so
    they must not share the interactive bucket."""
    key = bucket_key(_request("/v1/webhooks/connectors/abc"), tenant_id=None)
    assert key == "ratelimit:webhook:addr:10.0.0.9"


def test_workbench_read_traffic_gets_a_separate_tenant_scope() -> None:
    key = bucket_key(_request("/v1/workbench/conversations"), tenant_id="t-1")
    assert key == "ratelimit:workbench:tenant:t-1"


def test_workbench_writes_stay_in_the_general_api_scope() -> None:
    key = bucket_key(
        _request("/v1/workbench/conversations/id/actions", method="POST"), tenant_id="t-1"
    )
    assert key == "ratelimit:api:tenant:t-1"


def test_a_missing_client_address_is_handled() -> None:
    """`request.client` is None under some ASGI transports; a crash here would
    be a 500 on every request."""
    assert bucket_key(_request("/v1/cases", host=None), tenant_id=None).endswith("addr:unknown")


# --- middleware -----------------------------------------------------------


def _app(
    *,
    capacity: int,
    workbench_capacity: int | None = None,
    window: int = 60,
    unlimited: frozenset[str] = UNLIMITED_PATHS,
) -> FastAPI:
    app = FastAPI()

    @app.get("/thing")
    def thing() -> dict:
        return {"ok": True}

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/v1/workbench/conversations")
    def workbench_read() -> dict:
        return {"ok": True}

    @app.post("/v1/workbench/conversations/id/actions")
    def workbench_write() -> dict:
        return {"ok": True}

    app.add_middleware(
        RateLimitMiddleware,
        limiter=InMemoryRateLimiter(),
        api_policy=RateLimitPolicy(capacity=capacity, window_seconds=window),
        workbench_policy=RateLimitPolicy(
            capacity=workbench_capacity if workbench_capacity is not None else capacity,
            window_seconds=window,
        ),
        anonymous_policy=RateLimitPolicy(capacity=capacity, window_seconds=window),
        webhook_policy=RateLimitPolicy(capacity=capacity * 2, window_seconds=window),
        unlimited_paths=unlimited,
    )
    return app


def test_over_budget_requests_get_429_and_retry_after() -> None:
    client = TestClient(_app(capacity=1), raise_server_exceptions=False)

    assert client.get("/thing").status_code == 200
    resp = client.get("/thing")

    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "RATE_LIMITED"
    assert resp.json()["error"]["retryable"] is True
    assert int(resp.headers["Retry-After"]) >= 1


def test_the_remaining_header_is_advisory() -> None:
    client = TestClient(_app(capacity=3), raise_server_exceptions=False)

    resp = client.get("/thing")

    assert resp.status_code == 200
    assert resp.headers["X-RateLimit-Remaining"] == "2"


def test_health_and_metrics_are_never_limited() -> None:
    """Limiting `/metrics` would blind monitoring during an incident, and a
    429 on `/healthz` makes an orchestrator kill a healthy container."""
    client = TestClient(_app(capacity=1), raise_server_exceptions=False)

    for _ in range(5):
        assert client.get("/healthz").status_code == 200


def test_a_capped_route_does_not_exhaust_an_exempt_one() -> None:
    """The exemption must be a separate path, not a shared bucket."""
    client = TestClient(_app(capacity=1), raise_server_exceptions=False)

    client.get("/thing")  # exhausts the bucket
    assert client.get("/thing").status_code == 429
    assert client.get("/healthz").status_code == 200


def test_a_larger_policy_is_honoured_for_webhooks() -> None:
    """The webhook prefix has its own budget; if the middleware picked the API
    policy for it, provider bursts would be throttled into data loss."""
    app = _app(capacity=1)

    @app.get("/v1/webhooks/connectors/abc")
    def webhook() -> dict:
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    client.get("/thing")
    assert client.get("/thing").status_code == 429
    # Twice the API capacity, and a separate bucket.
    assert client.get("/v1/webhooks/connectors/abc").status_code == 200


def test_workbench_reads_have_a_separate_budget_and_writes_keep_the_api_budget() -> None:
    client = TestClient(_app(capacity=1, workbench_capacity=2), raise_server_exceptions=False)

    assert client.get("/thing").status_code == 200
    assert client.get("/thing").status_code == 429
    assert client.get("/v1/workbench/conversations").status_code == 200
    assert client.get("/v1/workbench/conversations").status_code == 200
    assert client.get("/v1/workbench/conversations").status_code == 429
    assert client.post("/v1/workbench/conversations/id/actions").status_code == 429


# --- wiring on the real app ----------------------------------------------


def test_the_real_app_exempts_health_and_metrics() -> None:
    """Pins the exemption in the assembled stack, not just in a test app: the
    middleware ordering in `main.py` is where this could silently regress."""
    import importlib

    main_mod = importlib.import_module("platform_core.main")
    with TestClient(main_mod.app, raise_server_exceptions=False) as client:
        for _ in range(30):
            assert client.get("/healthz").status_code == 200
        assert client.get("/metrics").status_code == 200
