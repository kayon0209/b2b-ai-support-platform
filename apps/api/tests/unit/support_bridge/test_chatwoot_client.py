"""Unit tests: Chatwoot client (ticket 8) via httpx.MockTransport."""

import asyncio

import httpx
import pytest

from platform_core.support_bridge.chatwoot_client import (
    ChatwootRejected,
    ChatwootUnavailable,
    CircuitBreaker,
    CircuitOpen,
)

# --- Circuit breaker ---


def test_breaker_opens_after_threshold() -> None:
    breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)
    for _ in range(3):
        breaker.on_failure()
    assert breaker.state.value == "open"
    with pytest.raises(CircuitOpen):
        breaker.before_call()


def test_breaker_half_open_then_close() -> None:
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=0)
    breaker.on_failure()
    assert breaker.state.value == "half_open"  # cooldown elapsed instantly
    breaker.on_success()
    assert breaker.state.value == "closed"


def test_breaker_half_open_failure_reopens() -> None:
    # Long cooldown so OPEN is observable through the dynamic state property.
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=60)
    breaker.on_failure()
    assert breaker.state.value == "open"
    breaker._state = "half_open"  # simulate cooldown elapsed / probe phase
    breaker.on_failure()  # failure during half-open must re-open
    assert breaker.state.value == "open"


# --- send_message retry mapping (MockTransport) ---


def _client(handler, monkeypatch, *, max_retries: int = 2, breaker: CircuitBreaker | None = None):
    """Build a ChatwootClient whose HTTP calls go through MockTransport.

    Patching lives on monkeypatch so it is undone per-test; a module-level
    patch leaked handlers across tests (state pollution).
    """
    from platform_core.support_bridge import chatwoot_client as cc

    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(cc.httpx, "AsyncClient", factory)
    return cc.ChatwootClient(
        "http://chatwoot.test", "token", max_retries=max_retries, breaker=breaker
    )


def _run(coro):
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def test_send_success_returns_external_id(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(lambda req: httpx.Response(200, json={"id": 777}), monkeypatch, max_retries=0)
    result = _run(
        client.send_message(account_id="1", conversation_id="42", content="hi", command_id="cmd-1")
    )
    assert result.external_message_id == "777"
    assert result.ambiguous is False


def test_known_command_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Idempotency guard: a recorded command_id never re-POSTs."""
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"id": 1})

    client = _client(handler, monkeypatch, max_retries=0)
    result = _run(
        client.send_message(
            account_id="1",
            conversation_id="42",
            content="hi",
            command_id="already-sent",
            known_message_ids={"already-sent": "555"},
        )
    )
    assert result.external_message_id == "555"
    assert not calls  # no HTTP call at all


def test_retryable_status_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"id": 9})

    client = _client(handler, monkeypatch, max_retries=3)
    result = _run(
        client.send_message(account_id="1", conversation_id="42", content="hi", command_id="cmd-2")
    )
    assert result.external_message_id == "9"
    assert len(attempts) == 3


def test_non_retryable_raises_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(422, json={"error": "bad"})

    client = _client(handler, monkeypatch, max_retries=3)
    with pytest.raises(ChatwootRejected):
        _run(
            client.send_message(
                account_id="1", conversation_id="42", content="hi", command_id="cmd-3"
            )
        )
    assert len(attempts) == 1  # no retry on 4xx


def test_transport_errors_exhaust_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ConnectError("down")

    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=60)
    client = _client(handler, monkeypatch, max_retries=2, breaker=breaker)
    with pytest.raises(ChatwootUnavailable):
        _run(
            client.send_message(
                account_id="1", conversation_id="42", content="hi", command_id="cmd-4"
            )
        )
    assert len(attempts) == 3  # initial + 2 retries
    # threshold=2 crossed mid-flight: breaker opened and stays open
    assert breaker.state.value == "open"


def test_payload_carries_command_id(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = req.read()
        return httpx.Response(200, json={"id": 5})

    client = _client(handler, monkeypatch, max_retries=0)
    _run(
        client.send_message(
            account_id="1", conversation_id="42", content="hello", command_id="cmd-x"
        )
    )
    assert b"cmd-x" in seen["body"]
