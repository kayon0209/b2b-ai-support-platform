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
        card: dict[str, Any] = {
            "kind": kind,
            "title": _text(record.get("order_id")),
            "status": _text(record.get("status")),
            "nodes": nodes,
        }
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
