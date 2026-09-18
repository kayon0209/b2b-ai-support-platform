"""Unit tests: IM channel adapters and the Linear adapter (via MockTransport).

The tests that matter most here are the ones about **silent** failure. Both
providers in this file report a rejected request inside an HTTP 200:

- Feishu answers a discarded message with `{"code": 19024, ...}` and status 200;
- Linear answers a failed mutation with `{"errors": [...]}` and status 200, or
  with `{"data": {"issueCreate": {"success": false}}}`.

A status-only check reads all three as success, and the operator's first
symptom is "the on-call agent was never paged" or "the ticket does not exist" -
with the platform reporting that it was. Each of those paths has a test below
rather than a comment, because the failure is invisible in production too.
"""

import asyncio

import httpx
import pytest

from platform_core.integrations.im import (
    FeishuNotificationAdapter,
    ImNotificationAdapter,
    TeamsNotificationAdapter,
)
from platform_core.integrations.linear import LinearAdapter, LinearIssueSummary
from platform_core.integrations.sdk import ConnectorContext


def _run(coro):
    return asyncio.run(coro)


def _ctx(configuration: dict, credentials: dict | None = None) -> ConnectorContext:
    return ConnectorContext(
        tenant_id="t1",
        connector_id="c1",
        credentials=credentials or {"api_token": "lin_api_key"},
        configuration=configuration,
    )


def _patch_transport(monkeypatch, handler) -> None:
    import platform_core.integrations.sdk as sdk

    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(sdk.httpx, "AsyncClient", factory)


# --- IM payload shapes ------------------------------------------------------


def test_the_generic_adapter_sends_the_slack_shape(monkeypatch) -> None:
    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(__import__("json").loads(req.content.decode()))
        return httpx.Response(200, text="ok")

    _patch_transport(monkeypatch, handler)
    adapter = ImNotificationAdapter(_ctx({"webhook_url": "http://im.test/hook"}))

    out = _run(adapter.send_notification(title="Breach", body_text="p1 refund"))

    assert seen[0]["text"].startswith("*Breach*")
    assert "msg_type" not in seen[0]
    assert out["ok"] is True


def test_feishu_sends_its_own_shape(monkeypatch) -> None:
    """`{"text": ...}` is not a Lark message body, and a Lark bot rejects it."""
    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(__import__("json").loads(req.content.decode()))
        return httpx.Response(200, json={"code": 0, "msg": "success"})

    _patch_transport(monkeypatch, handler)
    adapter = FeishuNotificationAdapter(_ctx({"webhook_url": "http://lark.test/hook"}))

    out = _run(adapter.send_notification(title="Breach", body_text="p1 refund"))

    assert seen[0]["msg_type"] == "text"
    assert "Breach" in seen[0]["content"]["text"]
    assert "text" not in seen[0]
    assert out["ok"] is True


def test_feishu_does_not_report_success_for_a_discarded_message(monkeypatch) -> None:
    """The whole reason the Feishu adapter is not the generic one.

    Lark returns 200 for a message it refused. Reading the status alone would
    report a page that never arrived.
    """
    _patch_transport(
        monkeypatch,
        lambda req: httpx.Response(200, json={"code": 19024, "msg": "Invalid msg_type"}),
    )
    adapter = FeishuNotificationAdapter(_ctx({"webhook_url": "http://lark.test/hook"}))

    out = _run(adapter.send_notification(title="Breach"))

    assert out["ok"] is False


def test_feishu_accepts_the_legacy_status_code_field(monkeypatch) -> None:
    """Older deployments answer with `StatusCode`/`StatusMessage`."""
    _patch_transport(monkeypatch, lambda req: httpx.Response(200, json={"StatusCode": 0}))
    adapter = FeishuNotificationAdapter(_ctx({"webhook_url": "http://lark.test/hook"}))

    assert _run(adapter.send_notification(title="Breach"))["ok"] is True


def test_feishu_reports_unverified_rather_than_success_with_no_signal(monkeypatch) -> None:
    """A 2xx the provider did not confirm. Success would claim a delivery
    nobody observed; failure would claim a rejection nobody observed."""
    _patch_transport(monkeypatch, lambda req: httpx.Response(200, text="<html>ok</html>"))
    adapter = FeishuNotificationAdapter(_ctx({"webhook_url": "http://lark.test/hook"}))

    out = _run(adapter.send_notification(title="Breach"))

    assert out["ok"] is False
    assert out["error_code"] == "IM_UNVERIFIED"


def test_teams_sends_a_message_card(monkeypatch) -> None:
    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(__import__("json").loads(req.content.decode()))
        return httpx.Response(200, text="1")

    _patch_transport(monkeypatch, handler)
    adapter = TeamsNotificationAdapter(_ctx({"webhook_url": "http://teams.test/hook"}))

    out = _run(adapter.send_notification(title="Breach", body_text="p1"))

    assert seen[0]["@type"] == "MessageCard"
    # Required by the schema, and it is what the notification preview shows.
    assert seen[0]["summary"]
    assert out["ok"] is True


@pytest.mark.parametrize(
    "adapter_cls",
    [ImNotificationAdapter, FeishuNotificationAdapter, TeamsNotificationAdapter],
)
def test_an_unconfigured_webhook_is_refused_before_any_call(adapter_cls, monkeypatch) -> None:
    calls: list[httpx.Request] = []
    _patch_transport(monkeypatch, lambda req: calls.append(req) or httpx.Response(200))

    out = _run(adapter_cls(_ctx({})).send_notification(title="Breach"))

    assert out == {"ok": False, "error_code": "IM_NOT_CONFIGURED", "ambiguous": False}
    assert calls == []


# --- Linear -----------------------------------------------------------------

LINEAR_OK = {
    "data": {
        "issueCreate": {
            "success": True,
            "issue": {"id": "iss-1", "identifier": "ENG-42", "title": "Refund", "url": None},
        }
    }
}


def _linear(configuration: dict | None = None, credentials: dict | None = None) -> LinearAdapter:
    return LinearAdapter(_ctx(configuration or {"team_id": "team-1"}, credentials=credentials))


def test_linear_creates_an_issue_and_returns_its_identifier(monkeypatch) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=LINEAR_OK)

    _patch_transport(monkeypatch, handler)
    out = _run(_linear().create_issue(title="Refund missing", description="ctx", case_ref="c-9"))

    body = __import__("json").loads(seen[0].content.decode())
    assert "issueCreate" in body["query"]
    assert body["variables"]["teamId"] == "team-1"
    # The Case reference travels with the ticket, so an agent reading Linear can
    # find the conversation without a second system.
    assert "c-9" in body["variables"]["description"]
    assert out["ok"] is True
    assert out["issue_key"] == "ENG-42"


def test_linear_uses_a_bare_api_key_by_default(monkeypatch) -> None:
    """Linear rejects `Bearer <api_key>`; only an OAuth token is a bearer."""
    seen: list[httpx.Request] = []
    _patch_transport(
        monkeypatch, lambda req: seen.append(req) or httpx.Response(200, json=LINEAR_OK)
    )

    _run(_linear().create_issue(title="x"))

    assert seen[0].headers["authorization"] == "lin_api_key"


def test_linear_honours_an_explicit_scheme(monkeypatch) -> None:
    """An OAuth token *is* a bearer token, so the scheme is configuration
    rather than a hardcoded guess."""
    seen: list[httpx.Request] = []
    _patch_transport(
        monkeypatch, lambda req: seen.append(req) or httpx.Response(200, json=LINEAR_OK)
    )

    _run(
        _linear(credentials={"api_token": "oauth-token", "scheme": "Bearer"}).create_issue(
            title="x"
        )
    )

    assert seen[0].headers["authorization"] == "Bearer oauth-token"


def test_a_graphql_error_in_a_200_is_a_failure(monkeypatch) -> None:
    """GraphQL reports a rejected query with HTTP 200. Reading the status alone
    would report a ticket that was never filed."""
    _patch_transport(
        monkeypatch,
        lambda req: httpx.Response(200, json={"errors": [{"message": "Team not found"}]}),
    )

    out = _run(_linear().create_issue(title="Refund"))

    assert out["ok"] is False
    assert out["error_code"].startswith("CONNECTOR_REJECTED")


def test_a_graphql_auth_error_parks_the_connector(monkeypatch) -> None:
    """The code the auth-reporting wrapper recognises, so an expired key puts
    the connector in NEEDS_REAUTH instead of failing every call silently."""
    _patch_transport(
        monkeypatch,
        lambda req: httpx.Response(200, json={"errors": [{"message": "Authentication required"}]}),
    )

    out = _run(_linear().create_issue(title="Refund"))

    assert out["error_code"] == "CONNECTOR_AUTH_EXPIRED"


def test_a_rejected_mutation_is_not_reported_as_created(monkeypatch) -> None:
    """`success: false` inside a 200: a business rejection the transport cannot
    see."""
    _patch_transport(
        monkeypatch,
        lambda req: httpx.Response(200, json={"data": {"issueCreate": {"success": False}}}),
    )

    out = _run(_linear().create_issue(title="Refund"))

    assert out["ok"] is False
    assert out["error_code"] == "LINEAR_MUTATION_REJECTED"


def test_a_missing_team_is_refused_without_calling_the_provider(monkeypatch) -> None:
    """Linear requires a team. Sending the mutation anyway produces a GraphQL
    error the operator has to read to learn a configuration fact."""
    calls: list[httpx.Request] = []
    _patch_transport(
        monkeypatch, lambda req: calls.append(req) or httpx.Response(200, json=LINEAR_OK)
    )

    out = _run(LinearAdapter(_ctx({})).create_issue(title="Refund"))

    assert out == {
        "ok": False,
        "error_code": "LINEAR_TEAM_NOT_CONFIGURED",
        "ambiguous": False,
    }
    assert calls == []


def test_verification_reads_the_issue_back(monkeypatch) -> None:
    bodies: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(req.content.decode())
        bodies.append(body)
        if "mutation" in body["query"]:
            return httpx.Response(200, json=LINEAR_OK)
        return httpx.Response(200, json={"data": {"issue": {"id": "iss-1"}}})

    _patch_transport(monkeypatch, handler)
    created = _run(_linear().create_issue(title="Refund"))

    verified = _run(_linear().verify_postcondition("linear.create_issue", {}, created))

    assert verified is True
    assert len(bodies) == 2
    assert bodies[1]["variables"]["id"] == "iss-1"


def test_verification_is_unknown_when_the_read_cannot_be_trusted(monkeypatch) -> None:
    """UNKNOWN, not False: the gateway treats UNKNOWN as "a human must look"
    and False as "it did not happen", and those are different outcomes."""
    _patch_transport(
        monkeypatch,
        lambda req: httpx.Response(200, json={"errors": [{"message": "Authentication required"}]}),
    )

    verified = _run(
        _linear().verify_postcondition("linear.create_issue", {}, {"ok": True, "issue_id": "iss-1"})
    )

    assert verified is None


def test_fetch_projects_canonical_issues_and_a_cursor(monkeypatch) -> None:
    _patch_transport(
        monkeypatch,
        lambda req: httpx.Response(
            200,
            json={
                "data": {
                    "issues": {
                        "nodes": [
                            {
                                "id": "iss-1",
                                "identifier": "ENG-1",
                                "title": "Refund",
                                "url": "https://linear.app/x/ENG-1",
                                "state": {"name": "In Progress"},
                                "assignee": {"name": "Ada"},
                            }
                        ],
                        "pageInfo": {"hasNextPage": True, "endCursor": "cur-1"},
                    }
                }
            },
        ),
    )

    issues, cursor = _run(_linear().fetch("issues"))

    assert issues == [
        LinearIssueSummary(
            external_ref="iss-1",
            key="ENG-1",
            title="Refund",
            status="In Progress",
            assignee="Ada",
            url="https://linear.app/x/ENG-1",
        )
    ]
    assert cursor == "cur-1"


def test_fetch_reports_the_end_of_the_walk(monkeypatch) -> None:
    """`hasNextPage: false` must return no cursor: a stored `endCursor` there
    would make a resumable sync fetch the same last page forever."""
    _patch_transport(
        monkeypatch,
        lambda req: httpx.Response(
            200,
            json={
                "data": {
                    "issues": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False, "endCursor": "cur-1"},
                    }
                }
            },
        ),
    )

    issues, cursor = _run(_linear().fetch("issues"))

    assert issues == []
    assert cursor is None
