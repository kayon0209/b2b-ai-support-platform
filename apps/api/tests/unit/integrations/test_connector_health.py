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

from platform_core.integrations import dead_letter, health
from platform_core.integrations.models import Connector
from platform_core.tool_gateway.registry import ConnectorOutcomeExecutor


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


# --- ConnectorOutcomeExecutor ----------------------------------------------


AUTH_EXPIRED_CODE = "CONNECTOR_AUTH_EXPIRED"


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


def _wrapper(inner: _RecordingExecutor) -> ConnectorOutcomeExecutor:
    """Build the wrapper with a session stand-in.

    The wrapper's job is detection and dispatch; the writes themselves are
    covered by the integration suite. Stubbing the session lets these tests
    answer "is this outcome observed at all", which is exactly the question
    `health_check` having zero callers got wrong.
    """

    class _Session:
        pass

    return ConnectorOutcomeExecutor(
        inner,
        session=_Session(),  # type: ignore[arg-type]
        connector=_connector(),
        ctx=None,
    )


@pytest.fixture
def reported(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, Any]]]:
    """Capture both side-effect paths instead of writing to a database."""
    captured: dict[str, list[dict[str, Any]]] = {"auth": [], "dead_letter": []}

    async def _fake_auth(session: Any, connector: Any, **kwargs: Any) -> Any:
        captured["auth"].append(kwargs)
        return None

    async def _fake_dead_letter(session: Any, **kwargs: Any) -> Any:
        captured["dead_letter"].append(kwargs)
        return None

    monkeypatch.setattr(health, "record_auth_failure", _fake_auth)
    monkeypatch.setattr(dead_letter, "record", _fake_dead_letter)
    return captured


def test_auth_expired_result_parks_the_connector(reported: dict[str, list[Any]]) -> None:
    inner = _RecordingExecutor({"ok": False, "error_code": AUTH_EXPIRED_CODE})

    output = _run(_wrapper(inner).execute("crm.update_account", {}, "k1"))

    assert output == {"ok": False, "error_code": AUTH_EXPIRED_CODE}
    assert len(reported["auth"]) == 1
    assert reported["auth"][0]["error_code"] == AUTH_EXPIRED_CODE
    # An auth rejection has its own queue; duplicating it here would fill the
    # dead-letter list with rows whose fix is "rotate the credential".
    assert reported["dead_letter"] == []


def test_successful_result_is_not_reported(reported: dict[str, list[Any]]) -> None:
    inner = _RecordingExecutor({"ok": True, "account_ref": "a1"})

    _run(_wrapper(inner).execute("crm.update_account", {}, "k1"))

    assert reported["auth"] == []
    assert reported["dead_letter"] == []


def test_retry_exhausted_failure_becomes_a_dead_letter(
    reported: dict[str, list[Any]],
) -> None:
    """A transport failure that exhausted its retries previously left nothing
    behind but a failed ToolExecution row that nothing listed."""
    inner = _RecordingExecutor({"ok": False, "error_code": "CONNECTOR_UNAVAILABLE", "attempts": 3})

    _run(_wrapper(inner).execute("crm.update_account", {"account_ref": "a1"}, "k1"))

    assert reported["auth"] == []
    assert len(reported["dead_letter"]) == 1
    assert reported["dead_letter"][0]["error_code"] == "CONNECTOR_UNAVAILABLE"
    assert reported["dead_letter"][0]["attempts"] == 3


def test_ambiguous_failure_becomes_a_dead_letter(reported: dict[str, list[Any]]) -> None:
    """Ambiguity is the case a human must judge: the write may have landed and
    the platform cannot decide on its own whether retrying is safe."""
    inner = _RecordingExecutor(
        {"ok": False, "error_code": "CONNECTOR_UNAVAILABLE", "ambiguous": True}
    )

    _run(_wrapper(inner).execute("crm.update_account", {}, "k1"))

    assert len(reported["dead_letter"]) == 1
    assert reported["dead_letter"][0]["ambiguous"] is True


def test_adapter_output_is_returned_unchanged(reported: dict[str, list[Any]]) -> None:
    """The wrapper observes; it must not rewrite what the adapter reported, or
    the gateway's postcondition verification would judge a different result."""
    payload = {"ok": False, "error_code": "CONNECTOR_REJECTED_400", "detail": "bad tier"}
    inner = _RecordingExecutor(payload)

    assert _run(_wrapper(inner).execute("crm.update_account", {}, "k1")) == payload


def test_verify_postcondition_delegates_unchanged() -> None:
    """The wrapper must not change the write path's own verdict: the gateway
    still owns the final execution status."""
    inner = _RecordingExecutor({"ok": True})
    wrapped = _wrapper(inner)

    assert _run(wrapped.verify_postcondition("crm.update_account", {}, {"ok": True})) is True


# --- dead letter classification and digest ---------------------------------


def test_auth_rejection_is_not_a_dead_letter() -> None:
    assert dead_letter.should_record(error_code=AUTH_EXPIRED_CODE, ambiguous=False) is False


def test_ambiguous_outcome_is_a_dead_letter_regardless_of_code() -> None:
    """`CONNECTOR_UNKNOWN` with ambiguous=True still means "we do not know what
    happened", which is precisely the row a human must look at."""
    assert dead_letter.should_record(error_code="CONNECTOR_UNKNOWN", ambiguous=True) is True


def test_plain_failure_is_a_dead_letter() -> None:
    assert dead_letter.should_record(error_code="CONNECTOR_UNAVAILABLE", ambiguous=False) is True


def test_success_is_not_a_dead_letter() -> None:
    assert dead_letter.should_record(error_code=None, ambiguous=False) is False


def test_operation_digest_is_stable_across_key_order() -> None:
    """Without `sort_keys` a dict that serialises in a different insertion
    order yields a different digest, and the grouping this exists for
    silently stops working."""
    first = dead_letter.operation_digest(
        tool_name="crm.update_account", parameters={"a": 1, "b": 2}
    )
    second = dead_letter.operation_digest(
        tool_name="crm.update_account", parameters={"b": 2, "a": 1}
    )
    assert first == second


def test_operation_digest_does_not_contain_the_payload() -> None:
    """The digest is what makes a dead letter safe to store: the arguments can
    be customer-derived, and this row is read by an operational endpoint and
    copied into backups."""
    digest = dead_letter.operation_digest(
        tool_name="crm.update_account", parameters={"account_ref": "acme-corp-42"}
    )
    assert "acme-corp-42" not in digest
    assert digest.startswith("crm.update_account:")


def test_operation_digest_differs_between_operations() -> None:
    first = dead_letter.operation_digest(tool_name="crm.update_account", parameters={"a": 1})
    second = dead_letter.operation_digest(tool_name="crm.update_account", parameters={"a": 2})
    assert first != second
