"""Record identifiers a read tool's own data carries.

Why this is its own module
--------------------------
Two layers need the same answer to "does this utterance name a live record?",
and they must not disagree:

- `intent` decides the **route**. An utterance that names an order is a
  business-read question, not a knowledge question, and the route is what
  sends it down the tool path at all.
- `tool_gateway.selector` decides **which tool**. The identifier names the
  record, so it is the strongest subject evidence there is.

`selector` already imports from `agent_runtime`, so putting the table here
keeps that direction and gives both callers one source. Two copies would drift,
and the drift would be silent: the route would say "business read" while the
selector found no candidate, which is exactly the `TOOL_NO_CANDIDATE`
abstention this module was written to remove.

The defect it fixes
-------------------
Measured 2026-09-23, on the customer surface. A customer asked
「我的订单到哪了？」, the platform asked for the order number - and then treated
the reply `SO-9001` as a knowledge question, because the reply contains no
noun from any vocabulary and no interrogative shape. The tool was never
selected, so the order card was unreachable by the route the product itself
recommended. A record id is a *sharper* signal than a noun: it is the primary
key, not a word about the record.

Shape, and what it deliberately excludes
----------------------------------------
Prefix plus digits, anchored on a word boundary. Not "any token containing
digits": a quantity, a date, a phone tail and an amount all contain digits, and
treating those as record ids would send unrelated questions down the tool path
and let a stray number pick the order lookup.
"""

from __future__ import annotations

import re

# Tool name -> the pattern its own records match. The keys are read tools from
# `tool_gateway.selector`; a tool absent here simply has no id-shaped handle.
READ_IDENTIFIERS: dict[str, str] = {
    "order.get_status": r"\bso-?\d{2,}\b",
    "shipment.track": r"\bsh-?\d{2,}\b",
    "billing.get_invoice": r"\binv-?\d{2,}\b",
    "case.read": r"\bcase-?\d{2,}\b",
}


def tools_named_by_identifier(question: str) -> list[str]:
    """Read tools whose record ids appear in this question.

    Ordered by `READ_IDENTIFIERS`' declaration order, so the result is stable
    for a caller that only looks at the first element.
    """
    lowered = (question or "").lower()
    return [
        tool_name for tool_name, pattern in READ_IDENTIFIERS.items() if re.search(pattern, lowered)
    ]


def names_a_record(question: str) -> bool:
    """Whether the utterance names any live record by id."""
    return bool(tools_named_by_identifier(question))


__all__ = ["READ_IDENTIFIERS", "names_a_record", "tools_named_by_identifier"]
