from __future__ import annotations

import pytest

from platform_core.evaluation.customer_outcomes import (
    CustomerOutcomeObservation,
    summarize_customer_outcomes,
)


def _observation(
    observation_id: str,
    *,
    status: str = "confirmed",
    resolved_at: int = 100,
    observed_through: int = 500,
    recontact_at: int | None = None,
    recontact_link_verified: bool = False,
    recontact_link_available: bool = False,
    platform_marked_resolved: bool = True,
) -> CustomerOutcomeObservation:
    return CustomerOutcomeObservation(
        observation_id=observation_id,
        resolution_at=resolved_at,
        observed_through=observed_through,
        confirmation_status=status,  # type: ignore[arg-type]
        platform_marked_resolved=platform_marked_resolved,
        same_issue_recontact_at=recontact_at,
        recontact_link_verified=recontact_link_verified,
        recontact_link_available=recontact_link_available,
    )


def test_confirmation_rates_distinguish_response_silence_and_pending() -> None:
    report = summarize_customer_outcomes(
        [
            _observation("confirmed", status="confirmed"),
            _observation("rejected", status="rejected"),
            _observation("silent", status="no_response"),
            _observation("pending", status="pending", platform_marked_resolved=False),
            _observation("not-requested", status="not_requested"),
        ],
        recontact_window_seconds=100,
    )

    assert report["confirmation_requested"] == 4
    assert report["confirmation_responses"] == 2
    assert report["customer_confirmed"] == 1
    assert report["customer_rejected"] == 1
    assert report["customer_no_response"] == 1
    assert report["awaiting_confirmation"] == 1
    assert report["explicit_confirmation_rate_of_requests"] == 0.25
    assert report["explicit_confirmation_rate_of_responses"] == 0.5
    assert report["silence_marked_resolved_without_confirmation"] == 1
    assert report["silence_is_confirmation"] is False


def test_recontact_rate_uses_only_mature_same_issue_cohorts() -> None:
    report = summarize_customer_outcomes(
        [
            _observation(
                "returned",
                recontact_at=150,
                recontact_link_verified=True,
                recontact_link_available=True,
            ),
            _observation("no-return", recontact_link_available=True),
            _observation(
                "outside-window",
                recontact_at=250,
                recontact_link_verified=True,
                recontact_link_available=True,
            ),
            _observation(
                "immature",
                resolved_at=1000,
                observed_through=1050,
                recontact_at=1040,
                recontact_link_verified=True,
                recontact_link_available=True,
            ),
        ],
        recontact_window_seconds=100,
    )

    assert report["mature_recontact_cohort"] == 3
    assert report["same_issue_recontacts_within_window"] == 1
    assert report["same_issue_recontact_rate"] == 0.3333
    assert report["immature_recontacts_excluded_from_rate"] == 1


def test_recontact_requires_a_verified_same_issue_link() -> None:
    with pytest.raises(ValueError, match="verified same-issue link"):
        summarize_customer_outcomes(
            [_observation("unlinked", recontact_at=150)],
            recontact_window_seconds=100,
        )


def test_empty_cohort_keeps_rates_unmeasured() -> None:
    report = summarize_customer_outcomes([], recontact_window_seconds=100)

    assert report["explicit_confirmation_rate_of_requests"] is None
    assert report["explicit_confirmation_rate_of_responses"] is None
    assert report["same_issue_recontact_rate"] is None


def test_unlinked_contacts_are_excluded_not_counted_as_no_recontact() -> None:
    report = summarize_customer_outcomes(
        [_observation("no-link")],
        recontact_window_seconds=100,
    )

    assert report["mature_recontact_cohort"] == 0
    assert report["recontact_link_unavailable_count"] == 1
    assert report["same_issue_recontact_rate"] is None


def test_duplicate_observations_and_inverted_windows_fail_closed() -> None:
    duplicate = _observation("same")
    with pytest.raises(ValueError, match="unique"):
        summarize_customer_outcomes([duplicate, duplicate], recontact_window_seconds=100)
    with pytest.raises(ValueError, match="move forward"):
        summarize_customer_outcomes(
            [_observation("inverted", resolved_at=200, observed_through=100)],
            recontact_window_seconds=100,
        )
