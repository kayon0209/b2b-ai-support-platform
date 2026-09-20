"""Deterministic PCB quoting (feature list 4B).

Why this exists when "AI 不定价" is a hard constraint: the constraint is about
*the model*. A model that emits a price is stating a fact it has no source for,
and the platform cannot tell a plausible number from a right one. A rule engine
reading a configured price table is the opposite - every figure it returns is
traceable to a rule someone put there, which is exactly what 4B.5 asks to keep.

So the split is: **the engine may quote, the model may not.** If the model ever
produces a price, that is still a red-line violation (6.2) regardless of what
this module can do.

**No price table ships with this module.** `RuleSet` is supplied by the caller
and the default is empty, which routes every request to a human. That is not a
stub waiting to be finished - it is 4B.4's rule: 非标/加急/议价一律转人工. An
unconfigured table means every quote is non-standard, so every quote goes to a
person. Inventing a plausible price table would be the single worst thing this
module could do, because a price is the kind of number that gets believed.

Money is integer minor units throughout (AGENTS.md), and the answer is a
**band**, never a single figure: a quote is an estimate with a spread, and
presenting it as exact would overstate what the table supports.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- request ---------------------------------------------------------------


@dataclass(frozen=True)
class QuoteRequest:
    """What the customer is asking for, in the terms a price table is keyed on."""

    layers: int
    length_mm: float
    width_mm: float
    quantity: int
    thickness_mm: float
    surface_finish: str
    # standard | expedite - the two tiers 4B.3 prices differently.
    lead_time_tier: str = "standard"

    def area_cm2(self) -> float:
        return (self.length_mm / 10.0) * (self.width_mm / 10.0)


@dataclass(frozen=True)
class QuoteBand:
    """A reference range, not a price. `basis` says which rule produced it."""

    low_minor: int
    high_minor: int
    currency: str
    basis: str

    def as_dict(self) -> dict[str, object]:
        return {
            "low_minor": self.low_minor,
            "high_minor": self.high_minor,
            "currency": self.currency,
            "basis": self.basis,
        }


@dataclass(frozen=True)
class QuoteUnavailable:
    """Why no band could be produced - always safe to show an operator."""

    reason_code: str
    detail: str = ""


# --- rules -----------------------------------------------------------------


@dataclass(frozen=True)
class LayerRule:
    """Price for one layer count and lead-time tier.

    `setup_minor` is per order, `per_cm2_minor` is per board, and `breaks`
    scales the per-board part down as quantity rises (4B.3 阶梯价).
    """

    setup_minor: int
    per_cm2_minor: int
    # (min_quantity, multiplier) applied to the per-board part.
    breaks: tuple[tuple[int, float], ...] = ()

    def quantity_multiplier(self, quantity: int) -> float | None:
        """The multiplier for this quantity, or None if below every break.

        None is deliberate: a quantity under the lowest configured break is a
        request the table does not cover, and the honest answer is a person,
        not an extrapolation down to a price nobody agreed.
        """
        best: float | None = None
        for minimum, multiplier in sorted(self.breaks):
            if quantity >= minimum:
                best = multiplier
        return best


@dataclass(frozen=True)
class RuleSet:
    """A versioned price table. Empty by default - see the module docstring."""

    version: str = "unconfigured"
    currency: str = "CNY"
    # (layers, lead_time_tier) -> rule
    layers: dict[tuple[int, str], LayerRule] = field(default_factory=dict)
    # surface finish -> multiplier on the per-board part
    finishes: dict[str, float] = field(default_factory=dict)
    # board thickness (mm) -> multiplier
    thicknesses: dict[float, float] = field(default_factory=dict)
    # Half-width of the band, as a fraction of the computed figure.
    spread: float = 0.1

    def rule_for(self, layers: int, tier: str) -> LayerRule | None:
        return self.layers.get((layers, tier))


# --- quoting ---------------------------------------------------------------

_BASIS_SEPARATOR = "|"


def quote(request: QuoteRequest, rules: RuleSet) -> QuoteBand | QuoteUnavailable:
    """Price a request from the rule table, or decline.

    Declines are normal and frequent - an unconfigured table declines
    everything - and a decline always means "a person quotes this", never
    "here is a rough number".
    """
    if request.layers <= 0 or request.quantity <= 0:
        return QuoteUnavailable("QUOTE_INPUT_INVALID", "layers and quantity must be positive")
    if request.length_mm <= 0 or request.width_mm <= 0:
        return QuoteUnavailable("QUOTE_INPUT_INVALID", "board dimensions must be positive")

    rule = rules.rule_for(request.layers, request.lead_time_tier)
    if rule is None:
        # Includes the case of an expedite request against a standard-only
        # table: 加急 is 4B.4's explicit human-routing case.
        return QuoteUnavailable("QUOTE_RULE_MISSING", f"{request.layers}L/{request.lead_time_tier}")

    quantity_multiplier = rule.quantity_multiplier(request.quantity)
    if quantity_multiplier is None:
        return QuoteUnavailable("QUOTE_QUANTITY_BELOW_MINIMUM", str(request.quantity))

    finish_multiplier = rules.finishes.get(request.surface_finish)
    if finish_multiplier is None:
        return QuoteUnavailable("QUOTE_FINISH_UNKNOWN", request.surface_finish)

    thickness_multiplier = rules.thicknesses.get(request.thickness_mm)
    if thickness_multiplier is None:
        return QuoteUnavailable("QUOTE_THICKNESS_NONSTANDARD", f"{request.thickness_mm}mm")

    per_board = (
        rule.per_cm2_minor
        * request.area_cm2()
        * quantity_multiplier
        * finish_multiplier
        * thickness_multiplier
    )
    total = rule.setup_minor + per_board * request.quantity
    if total <= 0:
        return QuoteUnavailable("QUOTE_NON_POSITIVE", rules.version)

    midpoint = int(round(total))
    half = int(round(total * rules.spread))
    basis = _BASIS_SEPARATOR.join(
        (
            f"version={rules.version}",
            f"layers={request.layers}",
            f"tier={request.lead_time_tier}",
            f"qty_break={quantity_multiplier}",
            f"finish={request.surface_finish}",
            f"thickness={request.thickness_mm}",
        )
    )
    # A band must be a band: a zero-width interval would read as an exact
    # price, which is what this module exists not to claim.
    low = max(0, midpoint - half)
    high = max(low + 1, midpoint + half)
    return QuoteBand(low_minor=low, high_minor=high, currency=rules.currency, basis=basis)


def quote_or_human_reason(request: QuoteRequest, rules: RuleSet) -> tuple[QuoteBand | None, str]:
    """Convenience for callers that only need "band, or why not"."""
    result = quote(request, rules)
    if isinstance(result, QuoteBand):
        return result, ""
    return None, result.reason_code
