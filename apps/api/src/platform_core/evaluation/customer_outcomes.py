"""Explicit customer confirmation and mature same-issue recontact metrics.

Silence is kept as an unconfirmed outcome. A recontact is counted only when a
trusted upstream linker has verified that it belongs to the same tenant,
customer, and issue; this module never infers identity from message text.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

ConfirmationStatus = Literal["not_requested", "confirmed", "rejected", "no_response", "pending"]
_CONFIRMATION_STATES = frozenset(
    {"not_requested", "confirmed", "rejected", "no_response", "pending"}
)
_SAFE_OBSERVATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


@dataclass(frozen=True)
class CustomerOutcomeObservation:
    """One privacy-minimized terminal outcome observation.

    `same_issue_recontact_at` may be supplied only after a tenant-scoped,
    server-side relationship check. No customer text or identity is accepted.
    """

    observation_id: str
    resolution_at: int
    observed_through: int
    confirmation_status: ConfirmationStatus
    platform_marked_resolved: bool
    same_issue_recontact_at: int | None = None
    recontact_link_verified: bool = False
    recontact_link_available: bool = False


def summarize_customer_outcomes(
    observations: Iterable[CustomerOutcomeObservation], *, recontact_window_seconds: int
) -> dict[str, object]:
    """Summarize explicit confirmation and fully observed recontact cohorts.

    The recontact rate uses only resolutions whose complete follow-up window
    has elapsed, avoiding right-censoring bias. A positive event in an immature
    window is exposed as a count but does not enter the rate denominator.
    """
    if type(recontact_window_seconds) is not int or recontact_window_seconds <= 0:
        raise ValueError("recontact window must be a positive integer number of seconds")

    rows = list(observations)
    ids: set[str] = set()
    for row in rows:
        if (
            not isinstance(row.observation_id, str)
            or not _SAFE_OBSERVATION_ID.fullmatch(row.observation_id)
            or row.observation_id in ids
        ):
            raise ValueError("observation ids must be present and unique")
        ids.add(row.observation_id)
        if row.confirmation_status not in _CONFIRMATION_STATES:
            raise ValueError("unknown confirmation status")
        if type(row.resolution_at) is not int or type(row.observed_through) is not int:
            raise ValueError("outcome timestamps must be integer UTC epoch seconds")
        if row.resolution_at < 0 or row.observed_through < row.resolution_at:
            raise ValueError("observation window must start at resolution and move forward")
        if type(row.platform_marked_resolved) is not bool:
            raise ValueError("platform_marked_resolved must be boolean")
        if type(row.recontact_link_available) is not bool:
            raise ValueError("recontact_link_available must be boolean")
        if row.same_issue_recontact_at is not None:
            if type(row.same_issue_recontact_at) is not int:
                raise ValueError("recontact timestamp must be integer UTC epoch seconds")
            if not row.recontact_link_verified:
                raise ValueError("recontact must have a verified same-issue link")
            if not row.recontact_link_available:
                raise ValueError("recontact cannot be measured without a complete link")
            if not row.resolution_at <= row.same_issue_recontact_at <= row.observed_through:
                raise ValueError("recontact timestamp must fall inside the observed window")

    requested = [row for row in rows if row.confirmation_status != "not_requested"]
    responses = [row for row in requested if row.confirmation_status in {"confirmed", "rejected"}]
    confirmed = sum(row.confirmation_status == "confirmed" for row in requested)
    rejected = sum(row.confirmation_status == "rejected" for row in requested)
    no_response = sum(row.confirmation_status == "no_response" for row in requested)
    pending = sum(row.confirmation_status == "pending" for row in requested)
    silence_marked_resolved = sum(
        row.confirmation_status == "no_response" and row.platform_marked_resolved
        for row in requested
    )

    mature = [
        row for row in rows if row.observed_through - row.resolution_at >= recontact_window_seconds
    ]
    mature_recontact_rows = [row for row in mature if row.recontact_link_available]
    immature_recontact_rows = [
        row
        for row in rows
        if row.recontact_link_available
        and row.observed_through - row.resolution_at < recontact_window_seconds
    ]
    immature_recontacts = sum(
        row.same_issue_recontact_at is not None
        and row.same_issue_recontact_at - row.resolution_at <= recontact_window_seconds
        for row in immature_recontact_rows
    )
    mature_recontacts = sum(
        row.same_issue_recontact_at is not None
        and row.same_issue_recontact_at - row.resolution_at <= recontact_window_seconds
        for row in mature_recontact_rows
    )
    linked_recontact_cohort = len(mature_recontact_rows)

    return {
        "eligible_resolutions": len(rows),
        "confirmation_requested": len(requested),
        "confirmation_responses": len(responses),
        "customer_confirmed": confirmed,
        "customer_rejected": rejected,
        "customer_no_response": no_response,
        "awaiting_confirmation": pending,
        "confirmation_not_requested": len(rows) - len(requested),
        "explicit_confirmation_rate_of_requests": (
            round(confirmed / len(requested), 4) if requested else None
        ),
        "explicit_confirmation_rate_of_responses": (
            round(confirmed / len(responses), 4) if responses else None
        ),
        "silence_marked_resolved_without_confirmation": silence_marked_resolved,
        "recontact_window_seconds": recontact_window_seconds,
        "mature_recontact_cohort": linked_recontact_cohort,
        "recontact_link_available_count": sum(row.recontact_link_available for row in rows),
        "recontact_link_unavailable_count": sum(not row.recontact_link_available for row in rows),
        "same_issue_recontacts_within_window": mature_recontacts,
        "same_issue_recontact_rate": (
            round(mature_recontacts / linked_recontact_cohort, 4)
            if linked_recontact_cohort
            else None
        ),
        "immature_recontacts_excluded_from_rate": immature_recontacts,
        "silence_is_confirmation": False,
    }


__all__ = ["CustomerOutcomeObservation", "summarize_customer_outcomes"]
