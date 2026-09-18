"""Inbound rate limiting.

Why a token bucket and not a fixed window
-----------------------------------------
A fixed window ("1000 requests per minute, reset on the minute") permits twice
the intended rate across a boundary: 1000 requests at 12:00:59 and another
1000 at 12:01:00 are both inside a window and both accepted. A token bucket
refills continuously, so a burst is bounded by the bucket's capacity rather
than by where the clock happened to be. The bucket also gives a meaningful
`Retry-After` - how long until one token is available - which a window cannot
compute without assuming the caller arrived at the start of it.

Three decisions worth stating
-----------------------------
1. **Redis is the counter store, and that is not the durable queue.** ADR 0002
   records that `APP_REDIS_URL` was read by no code and that this is deliberate
   because durable work lives in Postgres tables. Rate-limit counters are
   exactly the opposite kind of state: ephemeral, loss-tolerant, and worthless
   after a restart. Redis is the right home for them, and losing them costs one
   window of protection.

2. **The limiter falls back to in-process buckets when Redis is unreachable,
   rather than failing open.** Failing open would remove all protection at the
   moment a shared dependency is degraded - which is when abusive traffic is
   most likely to go unnoticed. The fallback is weaker (per process, so N
   replicas allow N times the rate) and the module says so instead of
   implying otherwise.

3. **Health and metrics are never limited.** Limiting `/metrics` would blind
   the monitoring precisely during an incident, and a 429 on `/healthz` makes
   an orchestrator kill a healthy container.

Keys are tenant when a tenant is resolved, and the client address otherwise.
A tenant key is what stops one noisy tenant from consuming another's budget;
the address key covers the paths reachable without a token (webhooks).
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from observability_metrics import get_metrics

# Never limited, for the reasons in the module docstring.
UNLIMITED_PATHS = frozenset({"/healthz", "/metrics", "/openapi.json", "/docs", "/redoc"})

# A public-facing webhook is burstier than interactive API traffic: a provider
# delivers a batch and retries on failure, and 429ing legitimate deliveries
# would turn the platform's own protection into data loss. It gets its own,
# more generous budget rather than sharing the API's.
WEBHOOK_PREFIX = "/v1/webhooks/"

BUCKET_KEY_PREFIX = "ratelimit"


@dataclass(frozen=True)
class RateLimitPolicy:
    """Tokens available and how fast they come back."""

    capacity: int
    window_seconds: float

    @property
    def refill_per_second(self) -> float:
        return self.capacity / self.window_seconds

    def __post_init__(self) -> None:
        if self.capacity < 1:
            raise ValueError("capacity must be at least 1")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: int = 0


class TokenBucket:
    """Pure token bucket. No I/O, so the arithmetic is unit-testable."""

    def __init__(self, policy: RateLimitPolicy) -> None:
        self._policy = policy
        self._tokens = float(policy.capacity)
        self._updated_at = time.monotonic()

    def take(self, *, now: float | None = None) -> RateLimitDecision:
        current = time.monotonic() if now is None else now
        elapsed = max(0.0, current - self._updated_at)
        self._tokens = min(
            float(self._policy.capacity),
            self._tokens + elapsed * self._policy.refill_per_second,
        )
        self._updated_at = current

        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return RateLimitDecision(True, int(self._tokens))

        # Time until exactly one token accumulates. Rounded up, and at least
        # one second, because a `Retry-After: 0` invites an immediate retry
        # that is guaranteed to fail again.
        deficit = 1.0 - self._tokens
        wait = math.ceil(deficit / self._policy.refill_per_second)
        return RateLimitDecision(False, 0, max(1, wait))


class RateLimiter(Protocol):
    def check(self, key: str, policy: RateLimitPolicy) -> RateLimitDecision: ...


class InMemoryRateLimiter:
    """Per-process buckets.

    Bounded on purpose. A dict keyed by client address is an unbounded memory
    map under a distributed source of addresses, so entries are dropped once
    the map exceeds `max_keys`; the cost of dropping one is a client getting a
    fresh bucket, which is the safe direction to be wrong in.
    """

    def __init__(self, *, max_keys: int = 10_000) -> None:
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()
        self._max_keys = max_keys

    def check(self, key: str, policy: RateLimitPolicy) -> RateLimitDecision:
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None or bucket._policy != policy:
                if len(self._buckets) >= self._max_keys:
                    self._buckets.clear()
                bucket = TokenBucket(policy)
                self._buckets[key] = bucket
            return bucket.take()

    def reset(self) -> None:
        """Test-only: drop all buckets."""
        with self._lock:
            self._buckets.clear()


# Token bucket in one round trip. Doing this as separate GET/SET calls would
# let two concurrent requests both read the same token count and both take it,
# which is exactly the burst a limiter exists to stop.
_LUA = """
local data = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
local now = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local refill = tonumber(ARGV[3])

if tokens == nil or ts == nil then
    tokens = capacity
    ts = now
end

local elapsed = math.max(0, now - ts) / 1000.0
tokens = math.min(capacity, tokens + elapsed * refill)

local allowed = 0
local retry_ms = 0
if tokens >= 1 then
    tokens = tokens - 1
    allowed = 1
else
    retry_ms = math.ceil((1 - tokens) / refill * 1000)
end

redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', KEYS[1], ARGV[4])
return {allowed, retry_ms}
"""


class RedisRateLimiter:
    """Shared token buckets, so the limit is per deployment and not per pod.

    Errors are swallowed and reported as "allowed": a rate limiter that takes
    the API down when its store hiccups is worse than the abuse it prevents.
    The middleware pairs this with an in-process fallback so a Redis outage
    degrades the limit rather than removing it.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self._script = client.register_script(_LUA)
        self.errors = 0

    def check(self, key: str, policy: RateLimitPolicy) -> RateLimitDecision:
        # TTL is two windows: long enough that a bucket survives a quiet
        # period without being thrown away, short enough that an abandoned key
        # does not live forever.
        ttl_ms = int(policy.window_seconds * 2 * 1000)
        try:
            allowed, retry_ms = self._script(
                keys=[key],
                args=[
                    int(time.time() * 1000),
                    policy.capacity,
                    # redis-py serializes floats; the script does tonumber().
                    policy.refill_per_second,
                    ttl_ms,
                ],
            )
        except Exception:  # noqa: BLE001 - see the class docstring
            self.errors += 1
            raise
        if int(allowed) == 1:
            return RateLimitDecision(True, 0)
        wait_seconds = math.ceil(int(retry_ms) / 1000) if int(retry_ms) else 1
        return RateLimitDecision(False, 0, max(1, wait_seconds))


class HybridRateLimiter:
    """Redis when it answers, in-process buckets when it does not."""

    def __init__(self, shared: RateLimiter | None, local: RateLimiter) -> None:
        self._shared = shared
        self._local = local
        self.degraded = False

    def check(self, key: str, policy: RateLimitPolicy) -> RateLimitDecision:
        if self._shared is not None:
            try:
                decision = self._shared.check(key, policy)
            except Exception:  # noqa: BLE001 - fall through to the local bucket
                self.degraded = True
            else:
                self.degraded = False
                return decision
        return self._local.check(key, policy)


def client_address(request: Request) -> str:
    """Best-effort client identity for the unauthenticated paths.

    `request.client` is the peer address, which behind a proxy is the proxy -
    so every caller shares one bucket. That is a real limitation and it is
    stated rather than hidden: honouring `X-Forwarded-For` would let any
    caller choose its own bucket by sending a header, which is strictly worse
    than a shared bucket. A deployment behind a proxy should rate-limit at the
    proxy and treat this as defence in depth.
    """
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    return str(host) if host else "unknown"


def bucket_key(request: Request, *, tenant_id: str | None) -> str:
    """Tenant when known, address otherwise.

    A tenant key is what stops one noisy tenant from consuming another's
    budget. Falling back to the address would put every unauthenticated caller
    in one bucket, which is correct for `/v1/webhooks/` (there is no tenant
    yet) and harmless elsewhere because nothing else is reachable unauthenticated.
    """
    scope = "webhook" if request.url.path.startswith(WEBHOOK_PREFIX) else "api"
    if tenant_id:
        return f"{BUCKET_KEY_PREFIX}:{scope}:tenant:{tenant_id}"
    return f"{BUCKET_KEY_PREFIX}:{scope}:addr:{client_address(request)}"


def build_limiter(*, redis_url: str | None) -> HybridRateLimiter:
    """Redis-backed when reachable, in-process otherwise.

    The Redis client is built with sub-second timeouts on purpose: this sits in
    the request path, and a hung connection would be a latency problem before
    it was ever a limiting problem. A failure here is not fatal - the local
    buckets still apply.
    """
    local = InMemoryRateLimiter()
    shared: RateLimiter | None = None
    if redis_url:
        try:
            import redis

            client = redis.Redis.from_url(
                redis_url,
                socket_connect_timeout=0.5,
                socket_timeout=0.5,
                decode_responses=True,
            )
            client.ping()
            shared = RedisRateLimiter(client)
        except Exception:  # noqa: BLE001 - degrade to the local buckets
            shared = None
    return HybridRateLimiter(shared, local)


def policies_from_settings(settings: Any) -> dict[str, RateLimitPolicy]:
    """Build the three policies from configuration.

    Three because the audiences differ: interactive API traffic, traffic that
    arrives with no tenant yet, and webhook deliveries that arrive in provider
    -paced bursts.
    """
    window = float(settings.rate_limit_window_seconds)
    return {
        "api": RateLimitPolicy(capacity=int(settings.rate_limit_requests), window_seconds=window),
        "anonymous": RateLimitPolicy(
            capacity=int(settings.rate_limit_anonymous_requests), window_seconds=window
        ),
        "webhook": RateLimitPolicy(
            capacity=int(settings.rate_limit_webhook_requests), window_seconds=window
        ),
    }


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Reject over-budget requests with 429 and a `Retry-After`."""

    def __init__(
        self,
        app: object,
        *,
        limiter: RateLimiter,
        api_policy: RateLimitPolicy,
        anonymous_policy: RateLimitPolicy,
        webhook_policy: RateLimitPolicy,
        unlimited_paths: frozenset[str] = UNLIMITED_PATHS,
    ) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._limiter = limiter
        self._api_policy = api_policy
        self._anonymous_policy = anonymous_policy
        self._webhook_policy = webhook_policy
        self._unlimited_paths = unlimited_paths

    def _policy_for(self, path: str, *, tenant_id: str | None) -> RateLimitPolicy | None:
        if path in self._unlimited_paths:
            return None
        if path.startswith(WEBHOOK_PREFIX):
            return self._webhook_policy
        return self._api_policy if tenant_id else self._anonymous_policy

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        ctx = getattr(request.state, "tenant_context", None)
        tenant_id = str(ctx.tenant_id) if ctx is not None else None

        policy = self._policy_for(request.url.path, tenant_id=tenant_id)
        if policy is None:
            return await call_next(request)

        key = bucket_key(request, tenant_id=tenant_id)
        decision = self._limiter.check(key, policy)

        get_metrics().http_requests_total.labels(
            method=request.method.upper(),
            route="ratelimit",
            status="429" if not decision.allowed else "allowed",
            outcome="throttled" if not decision.allowed else "ok",
        ).inc()

        if not decision.allowed:
            return Response(
                content='{"error":{"code":"RATE_LIMITED","retryable":true}}',
                status_code=429,
                media_type="application/json",
                headers={"Retry-After": str(decision.retry_after_seconds)},
            )

        response = await call_next(request)
        # Advisory: lets a client back off before it is refused rather than
        # after. Not a security control - a caller may ignore it.
        response.headers["X-RateLimit-Remaining"] = str(decision.remaining)
        return response
