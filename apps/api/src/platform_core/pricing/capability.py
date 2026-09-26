"""PCB process capability matrix (feature list 4B.2).

The question this answers is the one PCB support gets most often: *"can you
make this design?"* - a 0.1mm trace, a 0.2mm hole, six layers. Today the honest
answer is "ask a human", for every such question.

Same discipline as the quoting engine next door (4B.1), for the same reason:

- **No matrix ships with this module.** `CapabilityMatrix` is supplied by the
  caller and defaults to empty, which makes every dimension `unknown` and sends
  every question to a person. Fabricating a plausible capability table would be
  the worst thing this module could do - "yes, 0.1mm is fine" is the kind of
  answer a customer places an order against.
- **The verdict is a judgement about a *design*, not a promise about an
  order.** It says whether the numbers fall inside a configured range; it does
  not accept the job. The handoff still happens, which is why `out_of_range`
  carries the numbers that failed - the agent needs to know *which* number.

The rule that carries the design: **a partially-unknown design is never
"feasible"**. If a customer asks about trace width and layer count and the
matrix has no data for holes, the answer is not "feasible" - it is `unknown`,
because the platform does not know what it was not told. Reporting feasible on
a partial match is how a matrix quietly becomes a promise it cannot keep.
"""

from __future__ import annotations

from dataclasses import dataclass, field

FEASIBLE = "feasible"
OUT_OF_RANGE = "out_of_range"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class CapabilityRange:
    """The manufacturable span of one dimension, inclusive at both ends."""

    minimum: float
    maximum: float

    def contains(self, value: float) -> bool:
        return self.minimum <= value <= self.maximum


@dataclass(frozen=True)
class CapabilityRequest:
    """What the design says. `None` means the customer did not state it.

    Unstated is not the same as unknown-to-the-matrix: a customer who did not
    mention surface finish has not asked about it, so it produces no verdict.
    A dimension they *did* state that the matrix does not cover is the case that
    must not be answered "feasible".
    """

    min_trace_mm: float | None = None
    min_spacing_mm: float | None = None
    min_hole_mm: float | None = None
    layers: int | None = None
    thickness_mm: float | None = None
    surface_finish: str | None = None

    def stated(self) -> dict[str, float | int | str]:
        """Only the dimensions actually named, keyed by matrix field name."""
        found: dict[str, float | int | str] = {}
        for name in (
            "min_trace_mm",
            "min_spacing_mm",
            "min_hole_mm",
            "layers",
            "thickness_mm",
            "surface_finish",
        ):
            value = getattr(self, name)
            if value is not None:
                found[name] = value
        return found


@dataclass(frozen=True)
class CapabilityMatrix:
    """A versioned capability table. Empty by default - see the module docstring."""

    version: str = "unconfigured"
    # Numeric dimensions have a manufacturable range.
    ranges: dict[str, CapabilityRange] = field(default_factory=dict)
    # Surface finishes are a set of what the line can actually run, not a
    # range: "沉金 or 喷锡" cannot be expressed as a minimum and a maximum.
    finishes: frozenset[str] = frozenset()

    def range_for(self, dimension: str) -> CapabilityRange | None:
        return self.ranges.get(dimension)


@dataclass(frozen=True)
class CapabilityVerdict:
    """The answer, with the evidence that produced it."""

    status: str
    # Dimensions that fall outside the configured range, with the numbers, so
    # the person taking over can see which one failed without re-reading the
    # whole conversation.
    violations: tuple[str, ...] = ()
    # Dimensions the customer stated that the matrix cannot judge. Non-empty
    # means the status cannot be `feasible`.
    unknown: tuple[str, ...] = ()
    basis: str = "unconfigured"

    @property
    def definitively_out(self) -> bool:
        """True only when something is known to be unmanufacturable.

        A `handoff` is recommended in every non-feasible case, but the reason
        differs: this one has a concrete finding to report, while `unknown`
        means the platform simply cannot tell yet.
        """
        return self.status == OUT_OF_RANGE


_NUMERIC_DIMENSIONS = ("min_trace_mm", "min_spacing_mm", "min_hole_mm", "layers", "thickness_mm")


def check_capability(request: CapabilityRequest, matrix: CapabilityMatrix) -> CapabilityVerdict:
    """Whether a design falls inside a configured capability matrix.

    Evaluates every stated dimension. A single out-of-range dimension is
    decisive (the design cannot be made as specified), and any dimension the
    matrix cannot judge downgrades an otherwise-clean result to `unknown`.
    """
    stated = request.stated()
    if not stated:
        # Nothing to judge: not a refusal, just nothing asked. `feasible` would
        # be claiming approval for a design nobody described.
        return CapabilityVerdict(status=UNKNOWN, unknown=(), basis=matrix.version)

    violations: list[str] = []
    unknown: list[str] = []

    for dimension in _NUMERIC_DIMENSIONS:
        if dimension not in stated:
            continue
        value = float(stated[dimension])
        span = matrix.range_for(dimension)
        if span is None:
            unknown.append(dimension)
            continue
        if not span.contains(value):
            violations.append(f"{dimension}={value:g} outside {span.minimum:g}..{span.maximum:g}")

    if "surface_finish" in stated:
        finish = str(stated["surface_finish"])
        if not matrix.finishes:
            unknown.append("surface_finish")
        elif finish not in matrix.finishes:
            violations.append(f"surface_finish={finish} not in supported set")

    if violations:
        # Violations win over unknowns: "0.1mm is below our minimum" is a
        # complete answer even if holes were never configured.
        return CapabilityVerdict(
            status=OUT_OF_RANGE,
            violations=tuple(violations),
            unknown=tuple(unknown),
            basis=matrix.version,
        )
    if unknown:
        return CapabilityVerdict(
            status=UNKNOWN, violations=(), unknown=tuple(unknown), basis=matrix.version
        )
    return CapabilityVerdict(status=FEASIBLE, basis=matrix.version)


__all__ = [
    "FEASIBLE",
    "OUT_OF_RANGE",
    "UNKNOWN",
    "CapabilityMatrix",
    "CapabilityRange",
    "CapabilityRequest",
    "CapabilityVerdict",
    "check_capability",
]
