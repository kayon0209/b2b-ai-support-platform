"""Chatwoot REST API client (ticket 8).

Rules from AGENTS.md / docs/architecture.md:
- Only documented REST APIs; never the Chatwoot database.
- Outbound sends carry an idempotency key (command_id) so retries after
  ambiguity cannot duplicate customer-visible messages.
- Timeouts, bounded retries, circuit breaker, structured error mapping.
"""

import asyncio
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx

from platform_core.config import get_settings


class ChatwootError(Exception):
    """Base for mapped Chatwoot client errors."""

    def __init__(self, code: str, retryable: bool, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.retryable = retryable


class ChatwootUnavailable(ChatwootError):
    def __init__(self, detail: str = "") -> None:
        super().__init__("CHATWOOT_UNAVAILABLE", retryable=True, detail=detail)


class ChatwootRejected(ChatwootError):
    def __init__(self, status: int, detail: str = "") -> None:
        super().__init__(f"CHATWOOT_REJECTED_{status}", retryable=False, detail=detail)


class CircuitOpen(ChatwootError):
    def __init__(self) -> None:
        super().__init__("CHATWOOT_CIRCUIT_OPEN", retryable=True, detail="circuit breaker open")


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """Small deterministic breaker: N consecutive failures open it for a
    cooldown; a half-open probe failure re-opens."""

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


RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


@dataclass
class SendResult:
    status_code: int
    external_message_id: str | None
    ambiguous: bool  # transport died mid-flight; outcome unknown


class ChatwootClient:
    """Async client for one Chatwoot installation.

    send_message: POST /api/v1/accounts/{account_id}/conversations/{id}/messages
    Idempotency is delegated to the caller's command_id in the payload key;
    Chatwoot does not natively dedupe, so the platform stores
    (command_id -> external message id) and short-circuits retries.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_token: str | None = None,
        *,
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        settings = get_settings()
        self._base_url = (base_url or settings.chatwoot_base_url).rstrip("/")
        token = api_token
        if token is None and settings.chatwoot_api_token is not None:
            token = settings.chatwoot_api_token.get_secret_value()
        self._headers = {"api_access_token": token or ""}
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._breaker = breaker or CircuitBreaker()

    async def send_message(
        self,
        *,
        account_id: str,
        conversation_id: str,
        content: str,
        command_id: str,
        private: bool = False,
        echo_id: str | None = None,
        known_message_ids: dict[str, str] | None = None,
    ) -> SendResult:
        """Send a message with idempotent retry semantics.

        known_message_ids maps command_id -> already-created external
        message id (caller-owned store). If present we return the recorded
        id without a second POST — this is the duplicate-reply guard.
        """
        known = known_message_ids or {}
        if command_id in known:
            return SendResult(
                status_code=200, external_message_id=known[command_id], ambiguous=False
            )

        url = (
            f"{self._base_url}/api/v1/accounts/{account_id}"
            f"/conversations/{conversation_id}/messages"
        )
        payload: dict[str, Any] = {"content": content, "private": private, "command_id": command_id}
        if echo_id:
            payload["echo_id"] = echo_id

        self._breaker.before_call()
        last_error: ChatwootError | None = None
        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(
                    base_url=self._base_url,
                    headers=self._headers,
                    timeout=self._timeout,
                ) as client:
                    resp = await client.post(url, json=payload)
                if resp.status_code < 300:
                    self._breaker.on_success()
                    return SendResult(
                        status_code=resp.status_code,
                        external_message_id=str(resp.json().get("id")),
                        ambiguous=False,
                    )
                if resp.status_code in RETRYABLE_STATUS:
                    last_error = ChatwootUnavailable(f"status {resp.status_code}")
                    self._breaker.on_failure()
                else:
                    self._breaker.on_failure()
                    raise ChatwootRejected(resp.status_code, resp.text[:200])
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = ChatwootUnavailable(type(exc).__name__)
                self._breaker.on_failure()

            if attempt < self._max_retries:
                await asyncio.sleep(min(2**attempt * 0.25, 4.0))

        assert last_error is not None
        raise last_error

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker
