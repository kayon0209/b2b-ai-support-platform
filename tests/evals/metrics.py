"""Deterministic usage and cost aggregation for evaluation runs."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any


@dataclass(frozen=True)
class ModelUsageSample:
    prompt_tokens: int
    completion_tokens: int
    request_attempts: int
    valid_output: bool
    exact_intent_match: bool
    timed_out: bool
    business_goal_complete: bool | None = None


def summarize_model_usage(
    samples: Iterable[ModelUsageSample],
    *,
    prompt_cost_cents_per_1k: float,
    completion_cost_cents_per_1k: float,
    max_retries: int,
) -> dict[str, Any]:
    """Aggregate retries, failures, timeouts, tokens and estimated spend.

    Monetary values use integer USD cents. Per-success token averages retain
    useful resolution without inventing fractional cents. Failed-request token
    usage is provider-specific and is explicitly excluded from the estimate.
    """
    rows = list(samples)
    rates = (Decimal(str(prompt_cost_cents_per_1k)), Decimal(str(completion_cost_cents_per_1k)))
    if any(not rate.is_finite() or rate < 0 for rate in rates):
        raise ValueError("token cost rates must be finite and non-negative")
    if max_retries < 0:
        raise ValueError("max_retries must be non-negative")
    for row in rows:
        if min(row.prompt_tokens, row.completion_tokens, row.request_attempts) < 0:
            raise ValueError("usage counts must be non-negative")
        if row.request_attempts > max_retries + 1:
            raise ValueError("request attempts exceed the configured retry budget")

    prompt_tokens = sum(row.prompt_tokens for row in rows)
    completion_tokens = sum(row.completion_tokens for row in rows)
    request_attempts = sum(row.request_attempts for row in rows)
    retry_attempts = sum(max(0, row.request_attempts - 1) for row in rows)
    valid_outputs = sum(row.valid_output for row in rows)
    exact_intent_matches = sum(row.exact_intent_match for row in rows)
    timeouts = sum(row.timed_out for row in rows)

    exact_cost_cents = (
        Decimal(prompt_tokens) * rates[0] + Decimal(completion_tokens) * rates[1]
    ) / Decimal(1000)
    estimated_cost_cents = int(exact_cost_cents.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    goal_rows = [row for row in rows if row.business_goal_complete is not None]
    successful_goal_rows = [row for row in goal_rows if row.business_goal_complete is True]
    successful_goal_cost_cents = sum(
        (Decimal(row.prompt_tokens) * rates[0] + Decimal(row.completion_tokens) * rates[1])
        / Decimal(1000)
        for row in successful_goal_rows
    )
    cost_per_successful_goal = (
        (successful_goal_cost_cents / Decimal(len(successful_goal_rows))).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
        if successful_goal_rows
        else None
    )

    return {
        "model_request_attempts": request_attempts,
        "model_retry_attempts": retry_attempts,
        "max_retries_per_case": max_retries,
        "valid_outputs": valid_outputs,
        "invalid_or_unavailable_outputs": len(rows) - valid_outputs,
        "deadline_timeouts": timeouts,
        "total_prompt_tokens": prompt_tokens,
        "total_completion_tokens": completion_tokens,
        "exact_intent_match_cases": exact_intent_matches,
        "prompt_tokens_per_exact_intent_match": (
            round(prompt_tokens / exact_intent_matches, 3) if exact_intent_matches else None
        ),
        "completion_tokens_per_exact_intent_match": (
            round(completion_tokens / exact_intent_matches, 3) if exact_intent_matches else None
        ),
        "estimated_model_cost": {
            "currency": "USD",
            "minor_units": "cents",
            "total_cents": estimated_cost_cents,
            "below_cent_resolution": 0 < exact_cost_cents < 1,
            "rounding": "nearest_cent",
            "prompt_rate_cents_per_1k": str(rates[0]),
            "completion_rate_cents_per_1k": str(rates[1]),
            "usage_scope": (
                "provider-reported response tokens only; failed-request billing is unavailable"
                if retry_attempts
                else "provider-reported response tokens"
            ),
        },
        "business_goal_cost": {
            "measured_cases": len(goal_rows),
            "verified_successful_goals": len(successful_goal_rows),
            "estimated_cost_cents_per_successful_goal": (
                str(cost_per_successful_goal) if cost_per_successful_goal is not None else None
            ),
            "currency": "USD",
            "minor_units": "cents",
            "usage_scope": "provider-reported tokens for successful goals only",
            "failed_request_billing": "unavailable from token totals",
        },
    }
