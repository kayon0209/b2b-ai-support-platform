"""The quoting service: text in, band out, and where the table lives.

`RULES` is the tenant's price table. It ships **empty**, which means every
request declines and every quote is routed to a person - 4B.4's rule, not an
unfinished feature. Replacing it is a deployment decision that needs real
figures from the business; nothing in this module should ever be a plausible
guess, because a price is the kind of number people act on.
"""

from __future__ import annotations

from platform_core.pricing.engine import QuoteBand, RuleSet, quote
from platform_core.pricing.parse import parse_quote_request

RULES = RuleSet()


def quote_from_text(question: str) -> QuoteBand | None:
    """A reference band for a stated request, or None.

    None covers both "no table configured" and "the question did not say
    enough" - from the caller's point of view they are the same outcome, and
    collapsing them is deliberate: neither should produce a number.
    """
    request = parse_quote_request(question)
    if request is None:
        return None
    result = quote(request, RULES)
    return result if isinstance(result, QuoteBand) else None


def quote_label(question: str) -> str | None:
    """The band as something to put in front of a person who will quote.

    The basis travels with it (4B.5) so the figure can be explained later -
    which price-table version, which quantity break - rather than defended.
    """
    band = quote_from_text(question)
    if band is None:
        return None
    return f"quote_band={band.low_minor}-{band.high_minor} {band.currency} [{band.basis}]"
