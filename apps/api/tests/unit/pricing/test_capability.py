"""Feature list 4B.2: the capability verdict never overstates what is known.

The assertion this file exists for: **a partially-configured matrix must not
answer `feasible`**. If the customer states trace width and layers, and the
matrix has no hole data, then "yes" is a claim about a dimension nobody
measured - and it is the claim a customer orders against. `unknown` sends it to
a person, which is the honest outcome.

The rest pin the two straightforward readings: outside the range is decisive
regardless of what else is unknown, and a dimension the customer never
mentioned produces no verdict at all (unstated is not "unknown to the matrix").
"""

from __future__ import annotations

from platform_core.pricing.capability import (
    FEASIBLE,
    OUT_OF_RANGE,
    UNKNOWN,
    CapabilityMatrix,
    CapabilityRange,
    CapabilityRequest,
    check_capability,
)

_MATRIX = CapabilityMatrix(
    version="v1",
    ranges={
        "min_trace_mm": CapabilityRange(0.1, 0.5),
        "min_spacing_mm": CapabilityRange(0.1, 0.5),
        "min_hole_mm": CapabilityRange(0.2, 6.0),
        "layers": CapabilityRange(1, 12),
        "thickness_mm": CapabilityRange(0.4, 3.2),
    },
    finishes=frozenset({"沉金", "喷锡", "OSP"}),
)


def test_a_design_inside_the_matrix_is_feasible() -> None:
    verdict = check_capability(
        CapabilityRequest(min_trace_mm=0.15, layers=4, thickness_mm=1.6), _MATRIX
    )
    assert verdict.status == FEASIBLE
    assert verdict.violations == ()


def test_a_trace_below_the_minimum_is_out_of_range_with_the_number() -> None:
    """The agent needs to know which number failed, not just that one did."""
    verdict = check_capability(CapabilityRequest(min_trace_mm=0.08), _MATRIX)
    assert verdict.status == OUT_OF_RANGE
    assert verdict.definitively_out is True
    assert any("0.08" in violation for violation in verdict.violations)


def test_a_value_above_the_maximum_is_out_of_range() -> None:
    verdict = check_capability(CapabilityRequest(layers=20), _MATRIX)
    assert verdict.status == OUT_OF_RANGE


def test_range_ends_are_inclusive() -> None:
    """0.1mm is manufacturable if the matrix says the minimum is 0.1mm."""
    assert check_capability(CapabilityRequest(min_trace_mm=0.1), _MATRIX).status == FEASIBLE
    assert check_capability(CapabilityRequest(layers=12), _MATRIX).status == FEASIBLE


def test_an_empty_matrix_never_says_feasible() -> None:
    """The unconfigured default must route everything to a person."""
    verdict = check_capability(CapabilityRequest(min_trace_mm=0.15), CapabilityMatrix())
    assert verdict.status == UNKNOWN
    assert verdict.status != FEASIBLE


def test_a_dimension_missing_from_the_matrix_downgrades_a_clean_design() -> None:
    """The guard: partial coverage cannot become a yes."""
    partial = CapabilityMatrix(version="v1", ranges={"min_trace_mm": CapabilityRange(0.1, 0.5)})
    verdict = check_capability(CapabilityRequest(min_trace_mm=0.15, layers=4), partial)
    assert verdict.status == UNKNOWN
    assert "layers" in verdict.unknown


def test_a_violation_outranks_an_unknown_dimension() -> None:
    """ "0.05mm is below our minimum" is complete even if holes are unconfigured."""
    partial = CapabilityMatrix(version="v1", ranges={"min_trace_mm": CapabilityRange(0.1, 0.5)})
    verdict = check_capability(CapabilityRequest(min_trace_mm=0.05, min_hole_mm=0.3), partial)
    assert verdict.status == OUT_OF_RANGE
    assert "min_hole_mm" in verdict.unknown


def test_an_unstated_dimension_produces_no_verdict() -> None:
    """Not mentioning finish is not the same as the matrix not covering it."""
    verdict = check_capability(CapabilityRequest(min_trace_mm=0.15), _MATRIX)
    assert verdict.status == FEASIBLE
    assert verdict.unknown == ()


def test_an_unsupported_surface_finish_is_out_of_range() -> None:
    verdict = check_capability(CapabilityRequest(surface_finish="化学镍金"), _MATRIX)
    assert verdict.status == OUT_OF_RANGE
    assert any("化学镍金" in violation for violation in verdict.violations)


def test_a_supported_surface_finish_passes() -> None:
    assert check_capability(CapabilityRequest(surface_finish="沉金"), _MATRIX).status == FEASIBLE


def test_a_matrix_without_finishes_cannot_judge_a_finish() -> None:
    no_finishes = CapabilityMatrix(version="v1", ranges=_MATRIX.ranges)
    verdict = check_capability(CapabilityRequest(surface_finish="沉金"), no_finishes)
    assert verdict.status == UNKNOWN
    assert "surface_finish" in verdict.unknown


def test_an_empty_request_is_unknown_not_feasible() -> None:
    """Approving a design nobody described would be a claim about nothing."""
    assert check_capability(CapabilityRequest(), _MATRIX).status == UNKNOWN


def test_the_verdict_carries_the_matrix_version_as_its_basis() -> None:
    """4B.5: a verdict without its source cannot be re-checked later."""
    verdict = check_capability(CapabilityRequest(min_trace_mm=0.15), _MATRIX)
    assert verdict.basis == "v1"


def test_the_default_version_is_marked_unconfigured() -> None:
    assert check_capability(CapabilityRequest(layers=4), CapabilityMatrix()).basis == (
        "unconfigured"
    )
