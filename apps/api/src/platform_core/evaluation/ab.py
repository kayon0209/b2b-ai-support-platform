"""Feature list 8.6: A/B experiments.

How this differs from the rollout flag next to it (9.3), because conflating the
two is how a rollout becomes a broken experiment:

- **9.3 rollout** answers *"is this feature on for this tenant?"* It is
  per-tenant, boolean, and moves in one direction - 10%, then 50%, then 100%.
  Its purpose is to limit blast radius.
- **8.6 A/B** answers *"which variant does this unit get?"* It is per-unit
  (a conversation, a contact), multi-way, and stays put for the length of the
  experiment. Its purpose is to compare outcomes.

A rollout cannot express A/B: bucketing a whole tenant into one arm means every
conversation in that tenant sees the same variant, and a comparison of two
tenants is a comparison of two customers - not of two variants.

The bucketing mirrors `flag_service.stable_bucket` deliberately: same hash, same
bucket space, and the experiment name in the hash input for the same reason the
flag key is - so a unit that lands in the treatment arm of one experiment is not
mechanically the same unit in the treatment arm of every other, which would
correlate the experiments and hide interactions.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from platform_core.knowledge.flag_service import BUCKET_SPACE


@dataclass(frozen=True)
class Variant:
    """One arm of an experiment. `weight` is relative, not a percentage."""

    name: str
    weight: float = 1.0


def _bucket(*, experiment: str, unit_id: str) -> int:
    """Map (experiment, unit) to [0, BUCKET_SPACE). Same shape as the flag
    bucketer, so an operator reasoning about one understands the other."""
    material = f"{experiment}:{unit_id}".encode()
    digest = hashlib.sha256(material).digest()
    return int.from_bytes(digest[:8], "big") % BUCKET_SPACE


def assign_variant(
    *,
    experiment: str,
    unit_id: str,
    variants: list[Variant],
) -> str:
    """The arm `unit_id` belongs to in `experiment`.

    Deterministic: the same unit always gets the same arm, which is the
    property that makes a comparison valid - a unit that switches arms
    mid-experiment contributes to both and to neither.

    Weights are relative ("2 to 1" and "0.67 to 0.33" behave identically), so an
    unequal split does not require the caller to keep the numbers summing to
    anything in particular.
    """
    if not variants:
        raise ValueError("an experiment needs at least one variant")
    total = sum(variant.weight for variant in variants)
    if total <= 0:
        raise ValueError("variant weights must sum to more than zero")

    point = _bucket(experiment=experiment, unit_id=unit_id) / BUCKET_SPACE
    cumulative = 0.0
    for variant in variants:
        cumulative += variant.weight / total
        if point < cumulative:
            return variant.name
    # Only reachable if floating-point accumulation lands a hair short of 1.0
    # on the last arm. Falling back to the last variant keeps the function
    # total rather than raising on a rounding artefact.
    return variants[-1].name


def variant_for_conversation(
    *, experiment: str, conversation_ref_id: str, variants: list[Variant]
) -> str:
    """Convenience wrapper: the unit for a support experiment is a conversation.

    Named separately from `assign_variant` so the call site says which unit it
    means. Bucketing by tenant here would be the mistake described in the
    module docstring, and a name that spells out "conversation" is what stops
    someone reusing this helper on a tenant id.
    """
    return assign_variant(experiment=experiment, unit_id=conversation_ref_id, variants=variants)


__all__ = ["Variant", "assign_variant", "variant_for_conversation"]
