"""Read-tool receipts normalised into cards (feature list 4A.3 / 4A.4).

Why this exists
---------------
`orchestrator` already publishes a read tool's result onto the conversation as
a `tool` turn: the sanitised receipt, as JSON, in `text_redacted`. That is the
data layer, and it is not a surface. A customer asking "where is my order"
needs the stages and the dates - a list of dated nodes - not a JSON document,
and printing the raw receipt as a chat bubble is what the timeline did before
this module existed.

Normalising on the **read** side rather than storing a second copy is
deliberate: the receipt already exists, historical turns get cards for free,
and no migration is involved. More importantly, the rule for "what counts as a
renderable card" then lives in exactly one place, instead of being re-derived
by every surface that shows a receipt - which is how two surfaces that are
supposed to agree quietly stop agreeing.

Two shapes, one output
----------------------
The adapters disagree about the envelope, and only this module has to know:

    demo_erp   {"order_id": ..., "status": ..., "nodes": [...], "fetched_at": ...}
    real       {"record": {"order_id": ..., ...}, "resource": "orders", "fetched_at": ...}

Both carry the record itself; the real one nests it under `record`. A card
built from only one shape is a card that works in the demo and disappears in
production, so both are unwrapped here.

What it refuses to do
---------------------
- **No whitelist bypass.** Only fields named below reach the customer. A
  receipt is provider data of unknown shape; echoing it back is how a field
  nobody reviewed ends up on a customer's screen.
- **No invented freshness.** `fetched_at` is parsed or dropped - never guessed,
  never defaulted to "now". A card that claims live data it does not have is
  worse than one with no freshness label.
- **No card for an unrecognised payload.** `None` is returned and the surface
  falls back to what it did before, rather than rendering a generic field dump.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

# A node/state/label is display text. Bounded so a pathological provider
# payload cannot become the thing that decides how large a response is.
_MAX_TEXT = 64
_MAX_PROVENANCE = 32

# Resource (or tool) -> card kind. Keyed by both because the receipt's own
# `resource` field is present on the real adapter while `tool` is what
# `_publish_receipt` adds; either is enough to identify the card.
_TOOL_KINDS: dict[str, str] = {
    "order.get_status": "order_status",
    "orders": "order_status",
    "shipment.track": "shipment",
    "shipments": "shipment",
}

# Provider state -> the words a customer reads. The single source for both
# surfaces that describe a record: the card renders these labels, and the model
# is handed the same mapping as a glossary so its prose agrees with the card.
#
# It was two sources, and they disagreed in front of the customer. Measured
# 2026-09-23: the card said 生产中 and the sentence directly above it said
# `in_production`, because the labels lived only in the web client and the
# model was given the raw receipt. Same record, same turn, two vocabularies.
#
# An unlisted state is deliberately absent rather than defaulted: the card shows
# the provider's own wording for a state this platform does not know, which is
# honest, and the glossary simply omits it.
STATUS_LABELS: dict[str, str] = {
    "in_production": "生产中",
    "shipped": "已发货",
    "in_transit": "运输中",
    "delivered": "已签收",
    "pending": "待处理",
    "cancelled": "已取消",
}

# Fulfilment-stage state -> the words a customer reads, on the same terms.
NODE_STATE_LABELS: dict[str, str] = {
    "done": "已完成",
    "active": "进行中",
    "pending": "待执行",
}


def glossary_for(payload: str) -> str:
    """The label mapping for the states this receipt actually contains.

    Returned as text for the model's evidence block. Only the values present
    are listed: a glossary of every state the platform knows is noise, and it
    would invite the model to mention a state the record does not have.

    Returns "" when nothing needs translating, so the caller can append it
    unconditionally.
    """
    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    record = _record(parsed)

    wanted: list[tuple[str, str]] = []
    status = _text(record.get("status"))
    if status and status in STATUS_LABELS:
        wanted.append((status, STATUS_LABELS[status]))
    for node in _nodes(record.get("nodes")):
        state = node.get("state")
        if state and state in NODE_STATE_LABELS:
            pair = (state, NODE_STATE_LABELS[state])
            if pair not in wanted:
                wanted.append(pair)
    if not wanted:
        return ""
    pairs = ", ".join(f"{raw}={label}" for raw, label in wanted)
    return (
        "\nThe record uses these provider state codes. When you answer, use the "
        f"customer-facing wording: {pairs}."
    )


def _text(value: Any, *, limit: int = _MAX_TEXT) -> str | None:
    """A non-empty trimmed string, or None. Never coerces arbitrary objects."""
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed[:limit] if trimmed else None


def _instant(value: Any) -> int | None:
    """Parse a timestamp to epoch seconds, or None.

    Accepts what the two adapters actually produce (ISO 8601 and epoch
    seconds) and nothing else. A value that cannot be read is dropped, because
    the alternative is a card that displays a date it invented.
    """
    if isinstance(value, bool):  # bool is an int subclass; not a timestamp
        return None
    if isinstance(value, (int, float)):
        seconds = int(value)
        return seconds if seconds > 0 else None
    text = _text(value)
    if not text:
        return None
    try:
        # `fromisoformat` handles "Z" from 3.11 onwards.
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def _record(payload: dict[str, Any]) -> dict[str, Any]:
    """The record itself, whichever envelope the adapter used."""
    inner = payload.get("record")
    return inner if isinstance(inner, dict) else payload


def _nodes(value: Any) -> list[dict[str, str]]:
    """The fulfilment stages as display rows.

    A node without a label is dropped rather than rendered as "—": the label is
    the whole content of the row. `state` is passed through as the provider
    stated it (the surface maps the states it knows and stays neutral about the
    rest) because inventing a state would be inventing progress.
    """
    if not isinstance(value, list):
        return []
    rows: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        label = _text(item.get("label"))
        if label is None:
            continue
        row: dict[str, str] = {"label": label}
        state = _text(item.get("state") or item.get("status"))
        if state is not None:
            row["state"] = state
        at = _instant(item.get("at"))
        if at is not None:
            row["at"] = datetime.fromtimestamp(at, tz=UTC).isoformat()
        rows.append(row)
    return rows


def build_card(receipt_text: str) -> dict[str, Any] | None:
    """Turn a published receipt into a card, or None if there is no card here.

    Returns None for: unparseable text, a payload with no recognisable kind, a
    not-found read, and an order card with no usable stage rows. Each of those
    is a case where a card would be a picture of nothing.
    """
    try:
        payload = json.loads(receipt_text)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    # An honest "the provider does not have this record" is already the
    # answer's job to state; a card for it would be a box saying "not found".
    if payload.get("found") is False:
        return None

    kind = _TOOL_KINDS.get(str(payload.get("tool") or "")) or _TOOL_KINDS.get(
        str(payload.get("resource") or "")
    )
    if kind is None:
        return None

    record = _record(payload)
    freshness = _instant(payload.get("fetched_at"))
    provenance = _text(payload.get("source"), limit=_MAX_PROVENANCE)

    if kind == "order_status":
        nodes = _nodes(record.get("nodes"))
        if not nodes:
            # The stages ARE the card. An order card without them is a title
            # and a status chip, which the answer's prose already says.
            return None
        for node in nodes:
            # The label the customer should read, sent with the card so the
            # client does not have to keep its own copy of the vocabulary. The
            # raw `state` stays for a client that has its own mapping and for
            # anything reading the payload rather than rendering it.
            state_label = NODE_STATE_LABELS.get(node.get("state", ""))
            if state_label:
                node["state_label"] = state_label
        card: dict[str, Any] = {
            "kind": kind,
            "title": _text(record.get("order_id")),
            "status": _text(record.get("status")),
            "nodes": nodes,
        }
        status_label = STATUS_LABELS.get(card["status"] or "")
        if status_label:
            card["status_label"] = status_label
        eta = _instant(record.get("eta"))
        if eta is not None:
            card["eta"] = datetime.fromtimestamp(eta, tz=UTC).isoformat()
        quantity = record.get("quantity")
        if isinstance(quantity, int) and not isinstance(quantity, bool):
            card["quantity"] = quantity
    else:
        card = {
            "kind": kind,
            "title": _text(record.get("shipment_id")),
            "status": _text(record.get("status")),
        }
        status_label = STATUS_LABELS.get(card["status"] or "")
        if status_label:
            card["status_label"] = status_label
        carrier = _text(record.get("carrier"))
        if carrier is not None:
            card["carrier"] = carrier
        tracking = _text(record.get("tracking_no"))
        if tracking is not None:
            card["tracking_no"] = tracking
        if card["title"] is None and tracking is None:
            # A shipment card's whole content is which shipment and how to
            # follow it. A status alone names neither.
            return None

    card["fetched_at"] = freshness
    card["provenance"] = provenance
    return card
