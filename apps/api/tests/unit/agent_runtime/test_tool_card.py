"""Unit tests for the read-tool receipt -> card normaliser.

The cases here are the ones the two adapters actually disagree about, plus the
three rules the module promises: no field reaches the customer unless it is
named, freshness is never invented, and an unrecognised payload produces no
card rather than a generic dump.

Every one of these is a "silently renders wrong" case, which is why they are
worth pinning: a card that shows a plausible date nobody measured is worse than
no card, and nothing else in the suite would notice.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from platform_core.agent_runtime.tool_card import NODE_STATE_LABELS, build_card

DEMO_RECEIPT = {
    "order_id": "SO-9001",
    "status": "in_production",
    "nodes": [
        {"label": "下单", "status": "done", "at": "2026-09-14T10:00:00Z"},
        {"label": "工程确认", "status": "done", "at": "2026-09-15T09:20:00Z"},
        {"label": "生产", "status": "active", "at": "2026-09-17T08:00:00Z"},
        {"label": "出货", "status": "pending", "at": None},
    ],
    "eta": "2026-09-26T00:00:00Z",
    "quantity": 500,
    "found": True,
    "resource": "orders",
    "source": "demo",
    "fetched_at": "2026-09-21T00:00:00Z",
    "tool": "order.get_status",
}

REAL_RECEIPT = {
    "found": True,
    "resource": "orders",
    "record": {
        "order_id": "SO-9002",
        "status": "shipped",
        "nodes": [
            {"label": "下单", "status": "done", "at": "2026-09-08T11:00:00Z"},
            {"label": "出货", "status": "done", "at": "2026-09-16T15:30:00Z"},
        ],
        "eta": "2026-09-16T00:00:00Z",
        "quantity": 100,
        # What the ERP knows and the customer must not: only whitelisted
        # fields survive, so this is the field that proves it.
        "cost_price": 4200,
        "internal_notes": "customer escalated twice",
    },
    # The value the redactor would turn into `[PHONE]`; it parses here, and
    # `business_read` no longer emits it in this form (see its comment).
    "fetched_at": 1758403200,
    "tool": "order.get_status",
}


def _card(payload: dict[str, Any]) -> dict[str, Any] | None:
    return build_card(json.dumps(payload, ensure_ascii=False, default=str))


# --- The two envelopes ------------------------------------------------------


def test_the_demo_envelope_is_read_flat() -> None:
    card = _card(DEMO_RECEIPT)
    assert card is not None
    assert card["kind"] == "order_status"
    assert card["title"] == "SO-9001"
    assert card["status"] == "in_production"
    assert card["quantity"] == 500
    assert [node["label"] for node in card["nodes"]] == ["下单", "工程确认", "生产", "出货"]
    assert card["nodes"][2]["state"] == "active"
    # `status` on a node is what the adapter calls the node's state; the card
    # normalises it to one name so the surface has one thing to map.
    assert "status" not in card["nodes"][0]


def test_the_real_envelope_is_unwrapped() -> None:
    """A card built from only the demo shape is a card that disappears in
    production, which is exactly the failure this test exists to prevent."""
    card = _card(REAL_RECEIPT)
    assert card is not None
    assert card["title"] == "SO-9002"
    assert card["status"] == "shipped"
    assert len(card["nodes"]) == 2


def test_only_named_fields_reach_the_customer() -> None:
    card = _card(REAL_RECEIPT)
    assert card is not None
    allowed = {
        "kind",
        "title",
        "status",
        # `status_label` / the per-node `state_label` are named fields, not a
        # bypass: they are the customer-facing wording for a provider state code,
        # and they are produced by `STATUS_LABELS` / `NODE_STATE_LABELS` in
        # `tool_card` - never copied from the payload. Added 2026-09-23 so the
        # card and the answer's prose describe a record in the same vocabulary;
        # before that the card said 生产中 while the sentence above it said
        # `in_production`.
        "status_label",
        "nodes",
        "eta",
        "quantity",
        "carrier",
        "tracking_no",
        "fetched_at",
        "provenance",
    }
    assert set(card) <= allowed
    # The labels are ours, so they cannot carry provider text: a state code the
    # platform does not know must not become a label.
    for node in card["nodes"]:
        assert set(node) <= {"label", "state", "state_label", "at"}
        if "state_label" in node:
            assert node["state_label"] in NODE_STATE_LABELS.values()
    flattened = json.dumps(card, ensure_ascii=False)
    assert "cost_price" not in flattened
    assert "internal_notes" not in flattened
    assert "escalated" not in flattened


# --- Freshness --------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-21T00:00:00Z", 1789948800),
        ("2026-09-21T00:00:00+00:00", 1789948800),
        (1758403200, 1758403200),
        (1758403200.9, 1758403200),
    ],
)
def test_parsable_fetch_times_become_epoch_seconds(value: Any, expected: int) -> None:
    card = _card({**DEMO_RECEIPT, "fetched_at": value})
    assert card is not None
    assert card["fetched_at"] == expected


@pytest.mark.parametrize("value", ["[PHONE]", "not a date", "", None, True, 0, -5, {}])
def test_an_unreadable_fetch_time_is_dropped_not_guessed(value: Any) -> None:
    """Discarding is the honest answer. Defaulting to "now" would make every
    card claim the data is live, which is the one thing freshness is for."""
    card = _card({**DEMO_RECEIPT, "fetched_at": value})
    assert card is not None
    assert card["fetched_at"] is None


# --- Refusals ---------------------------------------------------------------


def test_an_order_card_needs_stages() -> None:
    """The stages are the card. Without them it is a title and a chip, which
    the answer's prose already says better."""
    assert _card({**DEMO_RECEIPT, "nodes": []}) is None
    assert _card({**DEMO_RECEIPT, "nodes": None}) is None
    without = {k: v for k, v in DEMO_RECEIPT.items() if k != "nodes"}
    assert _card(without) is None


def test_a_not_found_read_produces_no_card() -> None:
    """A record the provider does not have is the answer's sentence to say."""
    assert _card({"found": False, "resource": "orders", "id": "SO-9999"}) is None


def test_an_unknown_payload_produces_no_card() -> None:
    assert _card({"tool": "crm.update_account", "found": True, "ok": True}) is None
    assert _card({"found": True, "resource": "invoices", "total": 12}) is None


@pytest.mark.parametrize("text", ["", "not json", "[]", '"a string"', "null", "12"])
def test_unparseable_text_produces_no_card(text: str) -> None:
    assert build_card(text) is None


# --- Nodes ------------------------------------------------------------------


def test_a_stage_without_a_label_is_dropped() -> None:
    card = _card(
        {
            **DEMO_RECEIPT,
            "nodes": [
                {"status": "done"},
                {"label": "   "},
                {"label": "生产", "status": "active"},
                "not a node",
            ],
        }
    )
    assert card is not None
    # The surviving stage keeps its own label and state, plus the platform's
    # wording for the state (`state_label`). A stage whose *label* is blank is
    # still dropped: the label is the row's whole content.
    assert card["nodes"] == [{"label": "生产", "state": "active", "state_label": "进行中"}]


def test_stage_times_accept_both_encodings() -> None:
    card = _card(
        {
            **DEMO_RECEIPT,
            "nodes": [
                {"label": "a", "at": "2026-09-14T10:00:00Z"},
                {"label": "b", "at": 1758403200},
                {"label": "c", "at": "whenever"},
            ],
        }
    )
    assert card is not None
    assert card["nodes"][0]["at"] == "2026-09-14T10:00:00+00:00"
    assert card["nodes"][1]["at"] == "2025-09-20T21:20:00+00:00"
    # A time we cannot read is omitted: the row is still informative.
    assert "at" not in card["nodes"][2]


def test_a_boolean_is_not_a_quantity() -> None:
    card = _card({**DEMO_RECEIPT, "quantity": True})
    assert card is not None
    assert "quantity" not in card


# --- Shipments --------------------------------------------------------------


def test_a_shipment_card_carries_how_to_follow_it() -> None:
    card = _card(
        {
            "tool": "shipment.track",
            "resource": "shipments",
            "found": True,
            "shipment_id": "SH-7001",
            "carrier": "SF Express",
            "tracking_no": "SF1234567890",
            "status": "in_transit",
            "fetched_at": "2026-09-21T00:00:00Z",
        }
    )
    assert card is not None
    assert card["kind"] == "shipment"
    assert card["title"] == "SH-7001"
    assert card["carrier"] == "SF Express"
    assert card["tracking_no"] == "SF1234567890"
    # A shipment has no stage list; the card must not invent an empty one.
    assert card.get("nodes") is None


def test_a_shipment_with_nothing_identifying_produces_no_card() -> None:
    assert _card({"tool": "shipment.track", "found": True, "status": "in_transit"}) is None
