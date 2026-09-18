"""Unit tests: connector health state machine and the auth-failure reporter.

These are deliberately I/O-free. The state machine is the part that decides
whether a connector is allowed to execute writes, so it has to be verifiable
without a database - otherwise the rule that "a reachability probe must never
clear NEEDS_REAUTH" is only checked by an integration test that needs Docker
to run at all.
"""

import asyncio
import uuid
from typing import Any

import pytest

from platform_core.integrations import health
from platform_core.integrations.models import Connector
from platform_core.tool_gateway.registry import (
    AUTH_EXPIRED_CODE,
    AuthReportingExecutor,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _connector(status: str = "active") -> Connector:
    connector = Connector(
        tenant_id=uuid.uuid4(),
        provider="crm",
        name="acme-crm",
        status=status,
        capabilities=["update_account"],
        configuration={"base_url": "https://crm.test"},
        credential_ref="env://CRM_TOKEN",
    )
    connector.id = uuid.uuid4()
    return connector


# --- status_after_probe ----------------------------------------------------


def test_unreachable_active_connector_becomes_degraded() -> None:
    assert health.status_after_probe("active", reachable=False) == "degraded"


def test_reachable_degraded_connector_returns_to_active() -> None:
    assert health.status_after_probe("degraded", reachable=True) == "active"


@pytest.mark.parametrize("reachable", [True, False])
def test_probe_never_clears_needs_reauth(reachable: bool) -> None:
    """The load-bearing rule of this module.

    `health_check` is an unauthenticated reachability call, so a successful
    probe is not evidence that the credential works. If reachability could
    lift NEEDS_REAUTH, an expired credential would be silently re-armed and
    the next thing the platform does with an "active" connector is execute a
    write through it.
    """
    assert health.status_after_probe("needs_reauth", reachable=reachable) == "needs_reauth"


@pytest.mark.parametrize("reachable", [True, False])
def test_probe_leaves_a_disabled_connector_disabled(reachable: bool) -> None:
    """Operator intent outranks a probe: switching a connector off must not
    be undone by the endpoint answering."""
    assert health.status_after_probe("disabled", reachable=reachable) == "disabled"


# --- status_after_auth_failure --------------------------------------------


def test_auth_failure_parks_an_active_connector() -> None:
    assert health.status_after_auth_failure("active") == "needs_reauth"


def test_auth_failure_parks_a_degraded_connector() -> None:
    assert health.status_after_auth_failure("degraded") == "needs_reauth"


def test_auth_failure_does_not_revive_a_disabled_connector() -> None:
    """A connector nobody is calling must not generate an action item."""
    assert health.status_after_auth_failure("disabled") == "disabled"


# --- can_clear_reauth ------------------------------------------------------


def test_reactivation_requires_a_reachable_provider() -> None:
    allowed, reason = health.can_clear_reauth(reachable=False, credential_resolves=True)
    assert allowed is False
    assert reason == "CONNECTOR_UNREACHABLE"


def test_reactivation_requires_a_resolvable_credential() -> None:
    """Catches "operator repointed the reference at a variable they forgot to
    set" - the failure that would otherwise leave the API reporting `active`
    for a connector that still cannot authenticate."""
    allowed, reason = health.can_clear_reauth(reachable=True, credential_resolves=False)
    assert allowed is False
    assert reason == "CREDENTIAL_UNRESOLVED"


def test_reactivation_succeeds_with_both_conditions() -> None:
    assert health.can_clear_reauth(reachable=True, credential_resolves=True) == (True, "OK")


def test_unresolved_credential_is_reported_before_unreachability() -> None:
    """When both conditions fail, name the one the operator owns.

    The credential is fixable by the operator; reachability may be a network
    problem that is not theirs. Reporting the network first sends them to
    debug connectivity, and they only learn about the missing secret after
    fixing it - two round trips for something the server already knew.
    """
    allowed, reason = health.can_clear_reauth(reachable=False, credential_resolves=False)
    assert allowed is False
    assert reason == "CREDENTIAL_UNRESOLVED"


# --- credential_is_present -------------------------------------------------


def test_blank_credential_values_count_as_absent() -> None:
    """`{"api_token": ""}` produces an `Authorization: Bearer ` header, which
    is not a credential and would look configured while every call 401s."""
    assert health.credential_is_present({"api_token": ""}) is False
    assert health.credential_is_present({"api_token": "   "}) is False
    assert health.credential_is_present({}) is False


def test_non_blank_credential_counts_as_present() -> None:
    assert health.credential_is_present({"api_token": "abc"}) is True


# --- executable statuses ---------------------------------------------------


def test_only_active_connectors_are_executable() -> None:
    assert health.status_is_executable("active") is True
    for status in ("degraded", "needs_reauth", "disabled"):
        assert health.status_is_executable(status) is False


# --- AuthReportingExecutor -------------------------------------------------


class _RecordingExecutor:
    """Stands in for a real adapter's ToolExecutor protocol."""

    def __init__(self, output: dict[str, Any] | None) -> None:
        self._output = output
        self.calls: list[str] = []

    async def execute(
        self, tool_name: str, parameters: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any] | None:
        self.calls.append(tool_name)
        return self._output

    async def verify_postcondition(
        self, tool_name: str, parameters: dict[str, Any], output: dict[str, Any] | None
    ) -> bool | None:
        return True


def _wrapper(inner: _RecordingExecutor, captured: list[dict[str, Any]]) -> AuthReportingExecutor:
    """Build the wrapper with the health write stubbed.

    The wrapper's job is detection and dispatch; the write itself is covered
    by the integration suite. Stubbing lets this test answer "is an auth
    rejection observed at all", which is exactly the question `health_check`
    having zero callers got wrong.
    """

    class _Session:
        pass

    return AuthReportingExecutor(
        inner,
        session=_Session(),  # type: ignore[arg-type]
        connector=_connector(),
        ctx=None,
    )


def test_auth_expired_result_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []

    async def _fake_record(session: Any, connector: Any, **kwargs: Any) -> Any:
        captured.append({"connector": connector, "kwargs": kwargs})
        return None

    monkeypatch.setattr(health, "record_auth_failure", _fake_record)
    inner = _RecordingExecutor({"ok": False, "error_code": AUTH_EXPIRED_CODE})
    executor = _wrapper(inner, captured)

    output = _run(executor.execute("crm.update_account", {}, "k1"))

    assert output == {"ok": False, "error_code": AUTH_EXPIRED_CODE}
    assert len(captured) == 1
    assert captured[0]["kwargs"]["error_code"] == AUTH_EXPIRED_CODE


def test_successful_result_is_not_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, Any]] = []

    async def _fake_record(session: Any, connector: Any, **kwargs: Any) -> Any:
        captured.append({"kwargs": kwargs})
        return None

    monkeypatch.setattr(health, "record_auth_failure", _fake_record)
    inner = _RecordingExecutor({"ok": True, "account_ref": "a1"})
    executor = _wrapper(inner, captured)

    _run(executor.execute("crm.update_account", {}, "k1"))

    assert captured == []


def test_non_auth_failure_is_not_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 500 from the provider is not a credential problem. Parking the
    connector in NEEDS_REAUTH would send the operator to the wrong fix."""
    captured: list[dict[str, Any]] = []

    async def _fake_record(session: Any, connector: Any, **kwargs: Any) -> Any:
        captured.append({"kwargs": kwargs})
        return None

    monkeypatch.setattr(health, "record_auth_failure", _fake_record)
    inner = _RecordingExecutor({"ok": False, "error_code": "CONNECTOR_UNAVAILABLE"})
    executor = _wrapper(inner, captured)

    _run(executor.execute("crm.update_account", {}, "k1"))

    assert captured == []


def test_verify_postcondition_delegates_unchanged() -> None:
    """The wrapper must not change the write path's own verdict: the gateway
    still owns the final execution status."""
    inner = _RecordingExecutor({"ok": True})
    executor = _wrapper(inner, [])

    assert _run(executor.verify_postcondition("crm.update_account", {}, {"ok": True})) is True
