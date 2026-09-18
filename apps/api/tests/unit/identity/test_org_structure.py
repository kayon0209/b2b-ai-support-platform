"""Unit tests: contract tier -> SLA policy, and slug/tier validation.

Both halves are pure functions on purpose. The rule that decides how long a
customer's response window is has to be verifiable without a database, because
the failure mode is silent: an account with a tier nobody intended to honour
produces a plausible-looking deadline, not an error.
"""

import pytest

from platform_core.cases.models import (
    DEFAULT_SLA,
    TIER_TARGET_MULTIPLIERS,
    sla_policy_for_tier,
)
from platform_core.identity import org


def test_the_standard_tier_is_the_policy_that_preceded_tiers() -> None:
    """The migration must not silently change any existing Case's clock.

    Before tiers existed every Case got `DEFAULT_SLA`. If `standard` mapped to
    anything else, adding the column would have re-timed every open Case in
    every tenant.
    """
    policy = sla_policy_for_tier("standard")
    assert policy.first_response_minutes == DEFAULT_SLA.first_response_minutes
    assert policy.resolution_minutes == DEFAULT_SLA.resolution_minutes


def test_an_absent_tier_resolves_to_standard_not_the_tightest() -> None:
    """A Case with no account - which is every Case created before this field -
    must keep the deadline it would have had yesterday."""
    for tier in (None, ""):
        policy = sla_policy_for_tier(tier)
        assert policy.first_response_minutes == DEFAULT_SLA.first_response_minutes
        assert policy.resolution_minutes == DEFAULT_SLA.resolution_minutes


def test_an_unknown_tier_falls_back_rather_than_raising() -> None:
    """Defence in depth: the column carries a CHECK constraint and the API
    validates, so an unknown value here means data predating the constraint.
    Failing closed means *loosening* the clock, never tightening it - a
    too-tight window fires escalations nobody agreed to."""
    policy = sla_policy_for_tier("platinum-plus")
    assert policy.first_response_minutes == DEFAULT_SLA.first_response_minutes


def test_tiers_shorten_and_lengthen_the_window() -> None:
    strategic = sla_policy_for_tier("strategic")
    enterprise = sla_policy_for_tier("enterprise")
    basic = sla_policy_for_tier("basic")

    assert strategic.first_response_minutes == 15
    assert enterprise.first_response_minutes == 30
    assert basic.first_response_minutes == 120
    # Ordered, which is the whole point of a tier.
    assert (
        strategic.first_response_minutes
        < enterprise.first_response_minutes
        < DEFAULT_SLA.first_response_minutes
        < basic.first_response_minutes
    )


def test_a_tier_never_produces_a_zero_length_window() -> None:
    """A zero-minute target makes every Case instantly breached, which reads as
    a broken SLA rather than as a policy."""
    for tier in TIER_TARGET_MULTIPLIERS:
        policy = sla_policy_for_tier(tier)
        assert policy.first_response_minutes >= 1
        assert policy.resolution_minutes >= 1


@pytest.mark.parametrize("status", ["pending", "suspended", "churned"])
def test_an_inactive_contract_does_not_keep_the_tier_window(status: str) -> None:
    """An account that stopped paying does not keep a 15-minute
    first-response target. The escalation it would fire is one nobody agreed
    to honour."""
    policy = sla_policy_for_tier("strategic", contract_status=status)
    assert policy.first_response_minutes == DEFAULT_SLA.first_response_minutes


def test_the_tier_does_not_change_which_states_pause_the_clock() -> None:
    """Pausing is a property of the support workflow, not of what the customer
    bought: two customers running the same workflow must pause at the same
    points."""
    for tier in TIER_TARGET_MULTIPLIERS:
        assert sla_policy_for_tier(tier).running_states == DEFAULT_SLA.running_states


# --- slug and tier validation ----------------------------------------------


def test_slug_is_lowercased_and_trimmed() -> None:
    assert org.normalise_slug("  Support-EMEA  ") == "support-emea"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "with space",
        "sla/hash",
        "-leading-dash",
        "emoji-🙂",
        "a" * 64,
    ],
)
def test_invalid_slugs_are_refused(bad: str) -> None:
    with pytest.raises(org.OrgError):
        org.normalise_slug(bad)


def test_normalisation_is_idempotent() -> None:
    """Whatever normalisation does, applying it twice must not change the
    answer - otherwise `UNIQUE (tenant_id, slug)` compares a value the
    application can still move."""
    assert org.normalise_slug("SUPPORT") == org.normalise_slug("support")
    assert org.normalise_slug(org.normalise_slug("  Support  ")) == "support"


def test_tier_and_status_validation_reject_the_closed_set() -> None:
    assert org.validate_tier("strategic") == "strategic"
    assert org.validate_contract_status("churned") == "churned"
    with pytest.raises(org.OrgError):
        org.validate_tier("platinum")
    with pytest.raises(org.OrgError):
        org.validate_contract_status("cancelled")
