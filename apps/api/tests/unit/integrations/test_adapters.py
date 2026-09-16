"""Unit tests: connector SDK, CRM adapter, Jira adapter, IM notification
(tickets 27-28, 31-32) via httpx.MockTransport."""

import asyncio

import httpx

from platform_core.integrations.crm import CrmReadAdapter
from platform_core.integrations.im import ImNotificationAdapter
from platform_core.integrations.jira import JiraAdapter
from platform_core.integrations.resilience import retry_delays
from platform_core.integrations.sdk import ConnectorContext


def _run(coro):
    return asyncio.run(coro)


def _ctx(configuration: dict) -> ConnectorContext:
    return ConnectorContext(
        tenant_id="t1",
        connector_id="c1",
        credentials={"api_token": "secret-token", "user_email": "svc@test.local"},
        configuration=configuration,
    )


def _patch_transport(monkeypatch, handler) -> None:
    import platform_core.integrations.sdk as sdk

    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(sdk.httpx, "AsyncClient", factory)


# --- Retry schedule ---


def test_retry_delays_exponential_capped() -> None:
    delays = retry_delays(4)
    assert delays[0] == 0.25 and delays[1] == 0.5 and delays[2] == 1.0
    assert delays[-1] <= 4.0


# --- CRM adapter (ticket 28) ---


def test_crm_account_projection(monkeypatch) -> None:
    seen_headers = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen_headers.update(req.headers)
        return httpx.Response(
            200,
            json={
                "name": "Acme Corp",
                "tier": "enterprise",
                "contract_status": "active",
                "entitlements": [{"name": "premium-support"}, {"name": "sla-24h"}],
            },
        )

    _patch_transport(monkeypatch, handler)
    adapter = CrmReadAdapter(
        _ctx(
            {
                "base_url": "http://crm.test",
                "accounts_path": "/accounts/{id}",
            }
        )
    )
    summary = _run(adapter.get_account("acme-1"))

    assert summary is not None
    assert summary.name == "Acme Corp"
    assert summary.tier == "enterprise"
    assert summary.entitlements == ("premium-support", "sla-24h")
    assert summary.external_ref == "acme-1"
    # credentials injected server-side via headers, never in canonical data
    assert "secret-token" in str(seen_headers)


def test_crm_contact_drops_full_email(monkeypatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "name": "Alice",
                "email": "alice@acme.test",
                "account_id": "acme-1",
            },
        )

    _patch_transport(monkeypatch, handler)
    adapter = CrmReadAdapter(_ctx({"base_url": "http://crm.test"}))
    contact = _run(adapter.get_contact("c-9"))

    assert contact is not None
    assert contact.email_domain == "acme.test"
    assert "alice@" not in repr(contact)  # full email never in projection


def test_crm_cache_hit_within_ttl(monkeypatch) -> None:
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"name": "Cached", "tier": None})

    _patch_transport(monkeypatch, handler)
    adapter = CrmReadAdapter(_ctx({"base_url": "http://crm.test"}), cache_ttl_seconds=300)
    first = _run(adapter.get_account("a-1"))
    second = _run(adapter.get_account("a-1"))
    assert len(calls) == 1  # second read served from cache
    assert first is second


def test_crm_auth_failure_maps_to_needs_reauth(monkeypatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    _patch_transport(monkeypatch, handler)
    adapter = CrmReadAdapter(_ctx({"base_url": "http://crm.test"}))
    summary = _run(adapter.get_account("a-2"))
    assert summary is None


# --- Jira adapter (ticket 31) ---


def test_jira_create_issue_returns_key_and_url(monkeypatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"id": "10001", "key": "SUP-42"})

    _patch_transport(monkeypatch, handler)
    adapter = JiraAdapter(_ctx({"base_url": "http://jira.test", "project_key": "SUP"}))
    result = _run(adapter.create_issue("Export broken", "Cannot export", "CASE-7"))

    assert result["ok"] is True
    assert result["issue_key"] == "SUP-42"
    assert result["url"] == "http://jira.test/browse/SUP-42"


def test_jira_postcondition_verified_by_read(monkeypatch) -> None:
    created_key = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/issue"):
            resp = httpx.Response(201, json={"id": "10001", "key": "SUP-42"})
            created_key["k"] = True
            return resp
        if created_key.get("k"):
            return httpx.Response(200, json={"key": "SUP-42", "fields": {}})
        return httpx.Response(404)

    _patch_transport(monkeypatch, handler)
    adapter = JiraAdapter(_ctx({"base_url": "http://jira.test", "project_key": "SUP"}))
    output = _run(adapter.create_issue("t", "d", "c1"))
    verified = _run(adapter.verify_postcondition("jira.create_issue", {}, output))
    assert verified is True


def test_jira_postcondition_unknown_when_read_fails(monkeypatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/issue"):
            return httpx.Response(201, json={"id": "1", "key": "SUP-1"})
        return httpx.Response(503)  # cannot verify

    _patch_transport(monkeypatch, handler)
    adapter = JiraAdapter(_ctx({"base_url": "http://jira.test", "project_key": "SUP"}))
    output = _run(adapter.create_issue("t", "d", "c1"))
    verified = _run(adapter.verify_postcondition("jira.create_issue", {}, output))
    assert verified is None  # UNKNOWN, never success


def test_jira_search_before_create(monkeypatch) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = req.read().decode()
        assert "summary" in body or "jql" in body
        return httpx.Response(
            200,
            json={
                "issues": [
                    {
                        "id": "1",
                        "key": "SUP-7",
                        "fields": {"summary": "Export broken", "status": {"name": "Open"}},
                    },
                ]
            },
        )

    _patch_transport(monkeypatch, handler)
    adapter = JiraAdapter(_ctx({"base_url": "http://jira.test", "project_key": "SUP"}))
    issues = _run(adapter.search_issues("Export"))
    assert len(issues) == 1
    assert issues[0].key == "SUP-7"
    assert issues[0].status == "Open"


# --- IM notification adapter (ticket 32) ---


def test_im_notification_posts_redacted_payload(monkeypatch) -> None:
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = req.read().decode()
        return httpx.Response(200, json={"ok": True})

    _patch_transport(monkeypatch, handler)
    adapter = ImNotificationAdapter(_ctx({"webhook_url": "http://im.test/hook"}))
    result = _run(
        adapter.send_notification(
            title="New P1 case",
            body_text="Customer reports login failures",
            case_ref="CASE-9",
            deep_link="http://admin.test/cases/9",
        )
    )

    assert result["ok"] is True
    assert "New P1 case" in seen["body"]
    assert "CASE-9" in seen["body"]


def test_im_notification_truncates_long_body(monkeypatch) -> None:
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = req.read().decode()
        return httpx.Response(200, json={"ok": True})

    _patch_transport(monkeypatch, handler)
    adapter = ImNotificationAdapter(_ctx({"webhook_url": "http://im.test/hook"}))
    _run(adapter.send_notification(title="x", body_text="word " * 400))
    assert len(seen["body"]) < 2000  # truncated, not the full 2KB+ text


def test_im_not_configured_fails_cleanly() -> None:
    adapter = ImNotificationAdapter(_ctx({}))
    result = _run(adapter.send_notification(title="x", body_text="y"))
    assert result["ok"] is False
    assert result["error_code"] == "IM_NOT_CONFIGURED"
