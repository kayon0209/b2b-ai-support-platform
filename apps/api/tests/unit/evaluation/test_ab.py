"""Feature list 8.6: experiment assignment is stable and actually splits.

The load-bearing properties:

- **Stability.** The same unit gets the same arm on every call, in every
  process. A unit that drifts between arms contributes to both and to neither,
  so this is what makes any comparison mean anything.
- **It splits by unit, not by tenant.** The negative case is asserted
  directly: two conversations in the same tenant must be able to land in
  different arms, because bucketing the tenant is the mistake that turns an
  experiment into a two-customer comparison.
- **Experiments do not correlate.** The same unit in two different experiments
  must not land in the same arm mechanically, or interactions between
  treatments hide.
"""

from __future__ import annotations

from collections import Counter

import pytest

from platform_core.evaluation.ab import Variant, assign_variant, variant_for_conversation


def test_assignment_is_stable_across_calls() -> None:
    arms = [Variant("control"), Variant("treatment")]
    first = assign_variant(experiment="e1", unit_id="u-1", variants=arms)
    second = assign_variant(experiment="e1", unit_id="u-1", variants=arms)
    assert first == second


def test_assignment_only_returns_a_declared_variant() -> None:
    arms = [Variant("control"), Variant("treatment")]
    for index in range(200):
        assert assign_variant(experiment="e1", unit_id=f"u-{index}", variants=arms) in {
            "control",
            "treatment",
        }


def test_an_even_split_is_roughly_even() -> None:
    arms = [Variant("control"), Variant("treatment")]
    counts = Counter(
        assign_variant(experiment="e1", unit_id=f"u-{index}", variants=arms)
        for index in range(2000)
    )
    # Hashing is not a balanced allocator, so this is a sanity band rather
    # than an equality - a real skew (say, 90/10) is what it must catch.
    assert 0.4 < counts["control"] / 2000 < 0.6


def test_unequal_weights_are_respected_as_ratios() -> None:
    arms = [Variant("control", 3), Variant("treatment", 1)]
    counts = Counter(
        assign_variant(experiment="e1", unit_id=f"u-{index}", variants=arms)
        for index in range(2000)
    )
    share = counts["control"] / 2000
    assert 0.68 < share < 0.82  # 3:1 -> ~0.75


def test_weights_may_be_fractions_that_do_not_sum_to_one() -> None:
    arms = [Variant("a", 0.67), Variant("b", 0.33)]
    names = {
        assign_variant(experiment="e", unit_id=f"u-{index}", variants=arms) for index in range(500)
    }
    assert names == {"a", "b"}


def test_two_units_in_the_same_tenant_can_differ() -> None:
    """The whole point: bucketing the tenant would put both in one arm."""
    arms = [Variant("control"), Variant("treatment")]
    assigned = {
        variant_for_conversation(
            experiment="e1", conversation_ref_id=f"conv-{index}", variants=arms
        )
        for index in range(40)
    }
    assert assigned == {"control", "treatment"}


def test_different_experiments_do_not_correlate() -> None:
    arms = [Variant("control"), Variant("treatment")]
    same = sum(
        1
        for index in range(500)
        if assign_variant(experiment="e1", unit_id=f"u-{index}", variants=arms)
        == assign_variant(experiment="e2", unit_id=f"u-{index}", variants=arms)
    )
    # Independent hashes agree about half the time; near-perfect agreement
    # would mean the experiment name is not in the hash input.
    assert 0.3 < same / 500 < 0.7


def test_a_single_variant_experiment_always_assigns_it() -> None:
    arms = [Variant("only")]
    assert assign_variant(experiment="e", unit_id="whatever", variants=arms) == "only"


def test_no_variants_is_an_error_not_a_default() -> None:
    """Silently returning a name for an empty experiment would mislabel data."""
    with pytest.raises(ValueError):
        assign_variant(experiment="e", unit_id="u", variants=[])


def test_non_positive_total_weight_is_an_error() -> None:
    with pytest.raises(ValueError):
        assign_variant(experiment="e", unit_id="u", variants=[Variant("a", 0)])
