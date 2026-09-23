"""Unit tests: read-tool selection, business adapters, internal case.read,
argument extraction (iteration plan 3.1/3.2)."""

from __future__ import annotations

from platform_core.agent_runtime.identifiers import names_a_record
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


# --- Chinese live-data selection (audit 2026-09-23: the order card never
# appeared for a Chinese customer) ---------------------------------------------
#
# `intent.py` already records that five Chinese phrasings of "where is my
# order" used to route to `knowledge_qa` and select **zero** read tools. That
# was fixed on the routing side. The selection side was not: with no Chinese
# noun on `order.get_status`, every one of these scored only the scene weight
# - a four-way tie at 0.40 - and the tie-break was the alphabet, so
# `billing.get_invoice` won all five. The customer's question went to the
# invoice system, which has no such record, which is why the ticket-looking
# outcome was `TOOL_EXECUTION_UNVERIFIED` and a notice saying the ERP was
# down.
#
# These cases assert on the *selected tool*, not on the route: asserting the
# route is what let the original regression through.
_CN_ORDER_QUESTIONS = (
    "我的订单 SO-9001 到哪了？",
    "我的订单 SO-9001 到哪了",
    "SO-9001 什么时候发货",
    "帮我查一下订单 SO-9001 的状态",
    "订单 SO-9001 现在什么状态",
    "SO-9001 发货了吗",
)


def test_chinese_order_questions_select_the_order_tool() -> None:
    for question in _CN_ORDER_QUESTIONS:
        detection = classify(question)
        candidates = select_read_tools(detection, question)
        assert candidates, f"no read candidate for {question!r}"
        assert candidates[0].tool_name == "order.get_status", (
            f"{question!r} selected {candidates[0].tool_name}; "
            f"full order: {[c.tool_name for c in candidates]}"
        )


def test_chinese_order_question_beats_the_invoice_tool_on_a_tie() -> None:
    """The alphabet is not a policy: `b` must not beat `o`.

    `发货` is deliberately a noun of both `order.get_status` and
    `shipment.track`, so this question ties at 1.40 and only the tie-break
    decides. It must resolve to the order, because the order record is what
    carries the shipping node and the ETA.

    The fixture carries **no record id**, and that is load-bearing. It used to
    be `"SO-9001 什么时候发货"`, which tied only because nothing scored an
    identifier. Once `SO-9001` became evidence for `order.get_status` the
    question stopped being a tie, and this test failed on its own guard
    (`expected a genuine tie`) rather than passing for the wrong reason - which
    is what that guard is for. A tie-break test needs a question that ties.
    """
    question = "什么时候发货"
    candidates = select_read_tools(classify(question), question)
    tied = [c for c in candidates if c.score == candidates[0].score]
    assert len(tied) > 1, "expected a genuine tie, so the tie-break is what is under test"
    assert candidates[0].tool_name == "order.get_status"
    assert "billing.get_invoice" not in [c.tool_name for c in tied[:1]]


def test_the_tie_break_holds_when_four_tools_tie() -> None:
    """The widest tie the read vocabulary can produce.

    No noun matches, so every tool with an affinity for the scene scores the
    scene weight alone and all four tie at 0.40 - including
    `billing.get_invoice`, which is the tool the alphabetical tie-break used to
    hand every tied question to. Four candidates is where "the tie-break is a
    policy, not a formality" is actually under load.
    """
    question = "货什么时候发出"
    candidates = select_read_tools(classify(question), question)
    tied = [c for c in candidates if c.score == candidates[0].score]
    assert len(tied) > 1, "expected a genuine tie, so the tie-break is what is under test"
    assert candidates[0].tool_name == "order.get_status"
    assert "billing.get_invoice" not in [c.tool_name for c in tied[:1]]


def test_a_record_id_selects_its_tool_without_a_noun() -> None:
    """A bare record id is a business-read question, and it names its tool.

    Measured on the customer surface 2026-09-23. The platform asked a customer
    for their order number; they replied `SO-9001`; the reply contains no noun
    from any vocabulary and no interrogative, so it routed to the knowledge
    path and the order tool was never selected. The order card was therefore
    unreachable by the route the product itself had recommended.

    Both halves are asserted here because either alone leaves the defect: the
    **route** must be `business_read` (or `select_read_tools` returns nothing by
    construction), and the **tool** must be the order lookup.
    """
    for question, expected in (
        ("SO-9001", "order.get_status"),
        ("SO-9001 到哪了？", "order.get_status"),
        ("SH-7001 到哪了？", "shipment.track"),
        ("INV-9001 状态", "billing.get_invoice"),
    ):
        detection = classify(question)
        assert detection.route.value == "business_read", (
            f"{question!r} routed to {detection.route.value}; the selector never runs "
            "on a non-business-read route, so the tool could not be selected at all"
        )
        candidates = select_read_tools(detection, question)
        assert candidates, f"no read candidate for {question!r}"
        assert candidates[0].tool_name == expected, (
            f"{question!r} selected {candidates[0].tool_name}, expected {expected}"
        )


def test_a_quantity_is_not_a_record_id() -> None:
    """Digits alone must not select an order lookup.

    The identifier patterns are anchored on a prefix (`SO-`, `SH-`, `INV-`,
    `CASE-`) precisely so that a quantity, a date or a phone tail cannot send
    an unrelated question down the tool path. Asserted as its own case because
    the failure mode is silent: the tool would run, fail to find a record
    called "500", and abstain - which looks like a provider problem.
    """
    for question in ("500 件什么时候能到", "2026-09-26 发货吗"):
        assert not names_a_record(question), (
            f"{question!r} was read as naming a record; a bare number is not an id"
        )


def test_chinese_invoice_question_still_selects_the_invoice_tool() -> None:
    """The order fix must not swallow the invoice path.

    The phrasing carries the lookup frame (`查…状态`) on purpose. Written as
    "我的发票开好了吗" the question does not reach `business_read` at all -
    `_CN_LIVE_DATA` has no frame that matches it - so it would assert the
    routing gate instead of the selection under test, and fail for a reason
    that has nothing to do with this change.
    """
    question = "帮我查一下发票 INV-9001 的状态"
    candidates = select_read_tools(classify(question), question)
    assert candidates
    assert candidates[0].tool_name == "billing.get_invoice"


def test_chinese_parcel_question_selects_the_shipment_tool() -> None:
    question = "运单 SH-7001 的物流到哪了？"
    candidates = select_read_tools(classify(question), question)
    assert candidates
    assert candidates[0].tool_name == "shipment.track"


def test_read_selection_order_is_declared_not_alphabetical() -> None:
    """Every read tool that can score the same must have a declared rank.

    A tool missing from `_READ_PRIORITY` falls back to alphabetical ordering
    among the unranked, which is exactly the accident this change removes. The
    assertion is deliberately about the map, so adding a tool without ranking
    it fails here rather than in production.
    """
    from platform_core.tool_gateway.selector import (
        _READ_PRIORITY,
        _READ_SCENE_AFFINITY,
        _READ_SUBJECT_NOUNS,
    )

    assert set(_READ_SCENE_AFFINITY) == set(_READ_PRIORITY) == set(_READ_SUBJECT_NOUNS)
