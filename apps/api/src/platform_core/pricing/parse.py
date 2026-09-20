"""Pull the parameters a price table is keyed on out of a sentence.

Deliberately lexical (3.3 实体抽取). The alternative - letting a model fill in
the layer count and quantity - would put an invented input into a
deterministic calculation, and a price computed from a guessed quantity is
worse than no quote at all, because it looks just as precise.

Returns None when anything required is missing. A partial request is not
filled in from defaults: a quote for the wrong quantity is a promise, and the
engine's whole justification is that it can account for every figure.
"""

from __future__ import annotations

import re

from platform_core.pricing.engine import QuoteRequest

_LAYERS = re.compile(r"(\d+)\s*层")
# "100x80", "100×80", "100*80" - mm by convention in this trade.
_DIMENSIONS = re.compile(r"(\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+(?:\.\d+)?)")
_QUANTITY = re.compile(r"(\d+)\s*(?:片|pcs|PCS|件|套|块)")
# Board thickness, matched only in the explicit "板厚 1.6mm" order.
#
# An earlier version also accepted "1.6mm 板厚", which matched a *dimension*
# followed by the next word - "100x80mm 板厚1.6mm" read the thickness as 80.
# A wrong thickness is a wrong multiplier in the price.
_THICKNESS = re.compile(r"(?:板厚|厚度)\s*(\d+(?:\.\d+)?)\s*mm")
_EXPEDITE = re.compile(r"加急|expedite|express", re.IGNORECASE)

_FINISHES: tuple[tuple[str, str], ...] = (
    ("沉金", "ENIG"),
    ("化金", "ENIG"),
    ("ENIG", "ENIG"),
    ("喷锡", "HASL"),
    ("HASL", "HASL"),
    ("有铅", "HASL"),
    ("无铅喷锡", "HASL"),
    ("OSP", "OSP"),
    ("抗氧化", "OSP"),
)


def _finish(text: str) -> str | None:
    for token, canonical in _FINISHES:
        if token.lower() in text.lower():
            return canonical
    return None


def parse_quote_request(text: str) -> QuoteRequest | None:
    """A QuoteRequest from the sentence, or None if it cannot be read.

    None is the common case and is not an error: most pricing questions do not
    state a quantity or a finish, and those go to a person regardless.
    """
    layers = _LAYERS.search(text)
    dimensions = _DIMENSIONS.search(text)
    quantity = _QUANTITY.search(text)
    thickness = _THICKNESS.search(text)
    finish = _finish(text)

    if not layers or not dimensions or not quantity or thickness is None or finish is None:
        return None

    thickness_value = float(thickness.group(1) or "0")
    if thickness_value <= 0:
        return None

    return QuoteRequest(
        layers=int(layers.group(1)),
        length_mm=float(dimensions.group(1)),
        width_mm=float(dimensions.group(2)),
        quantity=int(quantity.group(1)),
        thickness_mm=thickness_value,
        surface_finish=finish,
        lead_time_tier="expedite" if _EXPEDITE.search(text) else "standard",
    )
