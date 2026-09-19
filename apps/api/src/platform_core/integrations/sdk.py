"""Connector SDK (ticket 27): the adapter contract every external system
integration implements, plus bounded-retry HTTP execution.

Design rules (docs/integrations.md):
- Provider payloads never leak into core domain models: adapters return
  canonical models.
- Every external request defines timeouts, retryable errors, bounded
  attempts, rate-limit handling, and circuit breaking.
- Secrets resolve server-side from credential_ref; adapters never receive
  raw secrets from callers.
"""

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

from platform_core.integrations.resilience import (
    CircuitBreaker,
    retry_delays,
)


class ConnectorError(Exception):
    def __init__(self, code: str, retryable: bool, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.retryable = retryable


class ConnectorUnavailable(ConnectorError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("CONNECTOR_UNAVAILABLE", retryable=True, detail=detail)


class ConnectorRejected(ConnectorError):
    def __init__(self, status: int, detail: str = "") -> None:
        super().__init__(f"CONNECTOR_REJECTED_{status}", retryable=False, detail=detail)


class ConnectorAuthExpired(ConnectorError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("CONNECTOR_AUTH_EXPIRED", retryable=False, detail=detail)


RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


def _jitter_ratio() -> float:
    """Jitter ratio from settings, imported lazily: the SDK is usable from
    contexts (scripts) where settings are irrelevant."""
    try:
        from platform_core.config import get_settings

        return float(get_settings().retry_jitter_ratio)
    except Exception:  # noqa: BLE001 - defaults stay safe without settings
        return 0.3


AUTH_FAILURE_STATUS = {401, 403}

# The error *codes* an adapter reports for a rejected credential. Declared
# next to the status set they are derived from so a reader changing one sees
# the other, and consumed by `dead_letter.should_record` to keep auth
# failures out of the dead-letter queue (they have NEEDS_REAUTH instead).
AUTH_FAILURE_CODES = frozenset({"CONNECTOR_AUTH_EXPIRED"})


@dataclass(frozen=True)
class ConnectorContext:
    """Everything an adapter call may use. Built by the platform."""

    tenant_id: str
    connector_id: str
    # Resolved from the secret manager by the caller, not from the request.
    credentials: dict[str, str]
    configuration: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionResult:
    ok: bool
    data: dict[str, Any] | None = None
    error_code: str | None = None
    ambiguous: bool = False  # transport died mid-flight; outcome unknown
    latency_ms: int = 0
    attempts: int = 0


class ConnectorAdapter(ABC):
    """Base class for all external system adapters (docs/api-contracts.md
    connector interface). Subclasses implement provider-specific calls and
    map payloads to canonical models.

    Read surface only: `health_check` and `fetch`. The write surface
    (`execute` / `verify_postcondition`) deliberately does NOT live here.

    It used to, with the shape `execute(command, parameters, key) ->
    ExecutionResult`. But the Tool Gateway consumes a different protocol for
    the same two method names - `ToolExecutor`, with
    `execute(tool_name, parameters, key) -> dict | None` and
    `verify_postcondition(tool_name, parameters, output)`. A subclass cannot
    satisfy both, so any adapter implementing the write surface was a Liskov
    violation: the mismatch is invisible at runtime (Python resolves by name)
    until a caller passes the other shape and gets a `TypeError`.

    `CrmReadAdapter` was excluded from the executor registry to work around
    exactly this, which treated the symptom. Removing the conflicting
    declarations is the fix: write-capable adapters now satisfy
    `ToolExecutor` structurally and can be registered.
    """

    provider: str = "abstract"
    capabilities: tuple[str, ...] = ()

    def __init__(self, context: ConnectorContext, breaker: CircuitBreaker | None = None) -> None:
        self.context = context
        self.breaker = breaker or CircuitBreaker()

    @abstractmethod
    async def health_check(self) -> bool: ...

    @abstractmethod
    async def fetch(self, resource: str, cursor: str | None = None) -> tuple[list[Any], str | None]:
        """Return (canonical records, next_cursor).

        Records are the adapter's canonical projections, not raw provider
        payloads - docs/api-contracts.md: "Connector-specific payloads stay
        inside the adapter. Domain modules consume canonical models." Typed
        as `list[Any]` because each provider projects to its own frozen
        dataclass, and forcing a shared dict shape would push provider
        detail back out to callers.
        """

    # --- Shared bounded HTTP helper ---

    async def http_request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        max_retries: int = 2,
        connect_timeout: float = 3.0,
        total_timeout: float = 10.0,
    ) -> ExecutionResult:
        """Bounded HTTP with timeout, classified errors, exponential backoff.

        `params` exists because a GET with a JSON body is not portable: many
        gateways and proxies drop it, and RFC 9110 gives a body on GET no
        defined semantics. Any read call that carries arguments (Jira's
        search, for instance) must pass them as query parameters.

        Rate limits (429) honor Retry-After. Auth failures map to
        NEEDS_REAUTH semantics via ConnectorAuthExpired. Network errors
        after exhausting retries report ambiguous=True for writes.
        """
        started = time.monotonic()
        self.breaker.before_call()
        attempts = 0
        last_error: ConnectorError | None = None

        # One client per call, shared across retries: a client per attempt
        # paid a fresh TCP + TLS handshake on every retry without reusing
        # any connection it had already warmed up.
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=connect_timeout,
                read=total_timeout,
                write=total_timeout,
                pool=total_timeout,
            )
        ) as client:
            jitter = _jitter_ratio()
            for attempt, delay in enumerate(retry_delays(max_retries, jitter_ratio=jitter)):
                attempts = attempt + 1
                try:
                    resp = await client.request(
                        method, url, headers=headers, json=json_body, params=params
                    )
                    if resp.status_code < 300:
                        self.breaker.on_success()
                        return ExecutionResult(
                            ok=True,
                            data=self._safe_json(resp),
                            latency_ms=int((time.monotonic() - started) * 1000),
                            attempts=attempts,
                        )
                    if resp.status_code in AUTH_FAILURE_STATUS:
                        self.breaker.on_failure()
                        return ExecutionResult(
                            ok=False,
                            error_code="CONNECTOR_AUTH_EXPIRED",
                            latency_ms=int((time.monotonic() - started) * 1000),
                            attempts=attempts,
                        )
                    if resp.status_code == 429:
                        last_error = ConnectorUnavailable("rate_limited")
                        self.breaker.on_failure()
                        retry_after = resp.headers.get("Retry-After")
                        wait = (
                            float(retry_after) if retry_after and retry_after.isdigit() else delay
                        )
                        await _sleep(wait)
                        continue
                    if resp.status_code in RETRYABLE_STATUS:
                        last_error = ConnectorUnavailable(f"status {resp.status_code}")
                        self.breaker.on_failure()
                        await _sleep(delay)
                        continue
                    self.breaker.on_failure()
                    return ExecutionResult(
                        ok=False,
                        error_code=f"CONNECTOR_REJECTED_{resp.status_code}",
                        latency_ms=int((time.monotonic() - started) * 1000),
                        attempts=attempts,
                    )
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_error = ConnectorUnavailable(type(exc).__name__)
                    self.breaker.on_failure()
                    if attempt < max_retries:
                        await _sleep(delay)

        latency = int((time.monotonic() - started) * 1000)
        code = last_error.code if last_error else "CONNECTOR_UNKNOWN"
        return ExecutionResult(
            ok=False,
            error_code=code,
            ambiguous=True,  # writes may or may not have landed
            latency_ms=latency,
            attempts=attempts,
        )

    @staticmethod
    def _safe_json(resp: httpx.Response) -> dict[str, Any] | None:
        try:
            data = resp.json()
        except ValueError:
            return None
        return data if isinstance(data, dict) else {"items": data}


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
