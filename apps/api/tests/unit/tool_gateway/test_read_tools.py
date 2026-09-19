"""Unit tests: read-tool selection, business adapters, internal case.read,
argument extraction (iteration plan 3.1/3.2)."""

from __future__ import annotations

from platform_core.agent_runtime.intent import classify
from platform_core.tool_gateway.selector import select_read_tools


def _scene(question: str) -> object:
    return classify(question).scene


def test_order_question_selects_order_tool_first() -> None:
    detection = classify("Where is my order 88123?")
    candidates = select_read_tools(detection, "Where is my order 88123?")
    assert candidates
    assert candidates[0].tool_name in {"order.get_status", "shipment.track"}


def test_invoice_question_selects_billing_tool() -> None:
    detection = classify("What is the current status of invoice 5511?")
    candidates = select_read_tools(detection, "What is the current status of invoice 5511?")
    assert candidates
    assert candidates[0].tool_name == "billing.get_invoice"


def test_unavailable_tools_are_dropped_not_downgraded() -> None:
    detection = classify("Where is my order 88123?")
    candidates = select_read_tools(detection, "Where is my order 88123?", available={"case.read"})
    assert all(c.tool_name == "case.read" for c in candidates) or not candidates


def test_selection_is_deterministic() -> None:
    question = "Where is my order 88123 and its shipment?"
    detection = classify(question)
    first = select_read_tools(detection, question)
    second = select_read_tools(detection, question)
    assert [c.tool_name for c in first] == [c.tool_name for c in second]


def test_unrelated_question_has_no_candidates() -> None:
    detection = classify("What is your refund policy?")
    assert select_read_tools(detection, "What is your refund policy?") == []


def test_scene_affinity_matches_intent() -> None:
    detection = classify("What is the current status of case 12345?")
    candidates = select_read_tools(detection, "What is the current status of case 12345?")
    assert any(c.tool_name == "case.read" for c in candidates)


def test_extract_tool_args_picks_first_entity() -> None:
    from platform_core.agent_runtime.orchestrator import _extract_tool_args

    assert _extract_tool_args("order.get_status", "Where is my order 88123?") == {
        "order_id": "88123"
    }
    assert _extract_tool_args("case.read", "What is the status of case 12345?") == {
        "case_ref": "12345"
    }
    assert _extract_tool_args("order.get_status", "Where is my order?") is None


def test_business_read_adapter_reads_one_record() -> None:
    import asyncio
    import json

    import httpx

    from platform_core.integrations.business_read import BusinessReadToolExecutor
    from platform_core.integrations.sdk import ConnectorContext

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200, json={"order_id": "88123", "status": "shipped", "eta": "2026-09-21"}
        )

    transport = httpx.MockTransport(handler)
    context = ConnectorContext(
        tenant_id="t1",
        connector_id="c1",
        credentials={"api_token": "secret"},
        configuration={"base_url": "https://orders.example.com"},
    )
    executor = BusinessReadToolExecutor(context)

    async def run() -> dict[str, object] | None:
        async def stubbed(method: str, url: str, **kw: object) -> object:
            captured["method"] = method
            captured["url2"] = url
            async with httpx.AsyncClient(transport=transport, base_url="") as client:
                resp = await client.request(method, url)
            from platform_core.integrations.sdk import ExecutionResult

            return ExecutionResult(ok=True, data=resp.json(), latency_ms=1, attempts=1)

        executor._adapter.http_request = stubbed  # type: ignore[method-assign]
        out = await executor.execute("order.get_status", {"order_id": "88123"}, "idem-1")
        verified = await executor.verify_postcondition(
            "order.get_status", {"order_id": "88123"}, out
        )
        return {"out": out, "verified": verified}

    result = asyncio.run(run())
    assert result is not None
    out = result["out"]
    assert isinstance(out, dict) and out["found"] is True
    assert out["record"]["status"] == "shipped"
    assert result["verified"] is True
    assert "api_token" not in json.dumps(out)
