"""A public reference price table, with its provenance attached.

This exists because a quoting engine with no table quotes nothing, which makes
it impossible to exercise - and because the alternative is inventing numbers.
Neither is acceptable, so the table below is built from published figures and
every quote it produces names where it came from.

**This is not the customer's price list.** It is public, budgetary, industry
data for standard FR4 work, and the basis string on every band says so. It must
be replaced by figures from the business before anyone quotes a customer from
it. The point of shipping it is that a number with a source and a date can be
judged; a plausible number with neither cannot.

Source: PCBSync, "PCB Manufacturing Cost" (pcbsync.com/pcb-manufacturing-cost),
updated 2026-08-20. Figures are indicative FOB Shenzhen ranges, USD, excluding
shipping and duties, and the page states they typically land within ±15% of a
firm quote.

Derived, not copied: the source publishes per-board prices at a 100x100mm
reference board and relative quantity multipliers, while the engine is
`setup + per_cm2 * area * quantity`. The per-cm2 rates below are fitted so the
engine reproduces the published per-board figures for that reference board;
the quantity multipliers and finish multipliers are taken directly.
"""

from __future__ import annotations

from platform_core.pricing.engine import LayerRule, RuleSet

VERSION = "public-reference-2026.08"

PROVENANCE: dict[str, str] = {
    "source": "PCBSync - PCB Manufacturing Cost (pcbsync.com/pcb-manufacturing-cost)",
    "source_updated": "2026-08-20",
    "retrieved": "2026-09-21",
    "currency": "USD",
    "basis_note": (
        "public budgetary industry data for standard FR4; not a contracted "
        "price list - confirm with the business before quoting"
    ),
}

# Quantity breaks are the source's relative per-board costs, used as published:
# 5=1.00, 25=0.74, 100=0.58, 500=0.45, 1000+=0.38 of the prototype baseline.
_QUANTITY_BREAKS: tuple[tuple[int, float], ...] = (
    (5, 1.0),
    (25, 0.74),
    (100, 0.58),
    (500, 0.45),
    (1000, 0.38),
)

# Finish multipliers, mid-range of the published surcharges over HASL:
# lead-free HASL +3-6%, OSP +2-5%, ENIG +12-20%, immersion silver +8-12%,
# ENEPIG +20-35%, hard gold +25-60%.
FINISHES: dict[str, float] = {
    "HASL": 1.0,
    "HASL_LF": 1.045,
    "OSP": 1.035,
    "IMMERSION_SILVER": 1.10,
    "ENIG": 1.16,
    "ENEPIG": 1.275,
    "HARD_GOLD": 1.425,
}

# Board thickness: the source gives **no** price effect for thickness - it is
# not among its cost drivers. So the only thickness this table can price is the
# standard 1.6mm, and anything else declines to a human rather than being
# extrapolated. That is the engine working as designed (4B.4: 非标一律转人工),
# not a gap to be papered over with a guess.
THICKNESSES: dict[float, float] = {1.6: 1.0}


def build_reference_ruleset() -> RuleSet:
    """The table, as a RuleSet the engine can consume.

    Only the standard lead-time tier is defined. Expedite is absent
    deliberately: the published premium spans +30-150%, wide enough that any
    single figure would be a guess, and 4B.4 routes 加急 to a person anyway.
    """
    return RuleSet(
        version=VERSION,
        currency="USD",
        layers={
            # Fitted so 100x100mm at 10pcs lands in the published $4.50-8.00
            # for 2-layer, $12-20 for 4-layer and $22-36 for 6-layer.
            (2, "standard"): LayerRule(
                setup_minor=2500,  # $25, simple 2-layer tooling (low end)
                per_cm2_minor=4,
                breaks=_QUANTITY_BREAKS,
            ),
            (4, "standard"): LayerRule(
                setup_minor=9000,  # $90, multilayer tooling (low end)
                per_cm2_minor=7,
                breaks=_QUANTITY_BREAKS,
            ),
            (6, "standard"): LayerRule(
                setup_minor=9000,
                per_cm2_minor=14,
                breaks=_QUANTITY_BREAKS,
            ),
        },
        finishes=dict(FINISHES),
        thicknesses=dict(THICKNESSES),
        # Wider than the engine default: the source is a range, and a narrow
        # band on top of range data would overstate the precision.
        spread=0.15,
    )
