"""Unit tests for the platform metrics instruments (observability_metrics.py).

These cover the safety properties that are easy to get wrong in a metrics
module:

- No tenant-identifying label ever reaches a metric (security requirement).
- Out-of-vocabulary label values are rejected rather than silently created.
- observe_* helpers validate their inputs before calling the instruments.
- The default registry is isolated per process (resettable for tests).
- render_metrics produces a parseable Prometheus text payload.
"""

from __future__ import annotations

import pytest
from prometheus_client import CollectorRegistry

from observability_metrics import (
    PlatformMetrics,
    get_metrics,
    metric_sample_value,
    platform_range_labels,
    render_metrics,
    reset_default_metrics,
)

# --- Registry isolation --------------------------------------------------


def test_metrics_instance_uses_its_own_registry() -> None:
    """Two PlatformMetrics instances must not share a registry."""
    reg_a = CollectorRegistry()
    reg_b = CollectorRegistry()
    metrics_a = PlatformMetrics(registry=reg_a)
    metrics_b = PlatformMetrics(registry=reg_b)
    assert metrics_a.runs_total is not metrics_b.runs_total


def test_get_metrics_returns_singleton() -> None:
    reset_default_metrics()
    a = get_metrics()
    b = get_metrics()
    assert a is b


def test_reset_default_metrics_clears_singleton() -> None:
    """Tests can reset the singleton so state does not leak."""
    reset_default_metrics()
    first = get_metrics()
    reset_default_metrics()
    second = get_metrics()
    assert first is not second


# --- Label vocabularies --------------------------------------------------


def test_platform_range_labels_returns_closed_vocabularies() -> None:
    assert "knowledge_qa" in platform_range_labels("run_route")
    assert "completed" in platform_range_labels("run_outcome")
    assert "supported" in platform_range_labels("citation_status")
    assert "interactive" in platform_range_labels("worker_queue")


def test_platform_range_labels_rejects_unknown_kind() -> None:
    with pytest.raises(KeyError, match="unknown metric label kind"):
        platform_range_labels("tenant_id")


# --- Label validation ----------------------------------------------------


def test_observe_run_rejects_invalid_outcome() -> None:
    metrics = PlatformMetrics()
    with pytest.raises(ValueError, match="run_outcome label"):
        metrics.observe_run(outcome="hacked", route="knowledge_qa", latency_seconds=1.0)


def test_observe_run_rejects_invalid_route() -> None:
    metrics = PlatformMetrics()
    with pytest.raises(ValueError, match="run_route label"):
        metrics.observe_run(outcome="completed", route="injected", latency_seconds=1.0)


def test_observe_run_accepts_valid_values() -> None:
    metrics = PlatformMetrics()
    metrics.observe_run(
        outcome="completed",
        route="knowledge_qa",
        latency_seconds=1.5,
        citation_count=3,
    )
    val = metric_sample_value(
        metrics, "platform_agent_runs_total", outcome="completed", route="knowledge_qa"
    )
    assert val == 1.0


def test_observe_run_records_abstention_reason() -> None:
    metrics = PlatformMetrics()
    metrics.observe_run(
        outcome="abstained",
        route="knowledge_qa",
        latency_seconds=0.8,
        abstain_reason="NO_EVIDENCE",
    )
    val = metric_sample_value(
        metrics, "platform_agent_abstentions_total", reason_code="NO_EVIDENCE"
    )
    assert val == 1.0


# --- Retrieval metrics ---------------------------------------------------


def test_observe_retrieval_records_latency_and_count() -> None:
    metrics = PlatformMetrics()
    metrics.observe_retrieval(latency_seconds=0.25, candidate_count=8)
    val = metric_sample_value(metrics, "platform_retrieval_candidates_count")
    assert val is not None
    assert val == 1.0


def test_observe_retrieval_accepts_zero_candidates() -> None:
    """Empty retrieval is a valid signal (drives abstention)."""
    metrics = PlatformMetrics()
    metrics.observe_retrieval(latency_seconds=0.01, candidate_count=0)
    val = metric_sample_value(metrics, "platform_retrieval_candidates_count")
    assert val is not None


# --- Model metrics -------------------------------------------------------


def test_observe_model_call_ok_records_latency() -> None:
    metrics = PlatformMetrics()
    metrics.observe_model_call(operation="generate", outcome="ok", latency_seconds=2.1)
    val = metric_sample_value(
        metrics,
        "platform_model_call_latency_seconds_count",
        operation="generate",
        outcome="ok",
    )
    assert val == 1.0


def test_observe_model_call_error_records_error_code() -> None:
    metrics = PlatformMetrics()
    metrics.observe_model_call(
        operation="generate", outcome="error", latency_seconds=0.5, error_code="TIMEOUT"
    )
    val = metric_sample_value(metrics, "platform_model_errors_total", error_code="TIMEOUT")
    assert val == 1.0


def test_observe_model_call_records_tokens() -> None:
    metrics = PlatformMetrics()
    metrics.observe_model_call(
        operation="generate",
        outcome="ok",
        latency_seconds=1.0,
        prompt_tokens=150,
        completion_tokens=42,
    )
    assert metric_sample_value(metrics, "platform_model_tokens_total", direction="prompt") == 150.0
    assert (
        metric_sample_value(metrics, "platform_model_tokens_total", direction="completion") == 42.0
    )


# --- Citation validation -------------------------------------------------


def test_citation_validation_total_tracks_supported_vs_unsupported() -> None:
    metrics = PlatformMetrics()
    metrics.citation_validation_total.labels(status="supported").inc()
    metrics.citation_validation_total.labels(status="supported").inc()
    metrics.citation_validation_total.labels(status="unsupported").inc()
    assert (
        metric_sample_value(metrics, "platform_agent_citation_validation_total", status="supported")
        == 2.0
    )
    assert (
        metric_sample_value(
            metrics, "platform_agent_citation_validation_total", status="unsupported"
        )
        == 1.0
    )


# --- Lease conflicts -----------------------------------------------------


def test_lease_conflicts_are_counted() -> None:
    metrics = PlatformMetrics()
    metrics.lease_conflicts_total.inc()
    metrics.lease_conflicts_total.inc(2)
    assert metric_sample_value(metrics, "platform_agent_lease_conflicts_total") == 3.0


# --- Inbox and ingestion -------------------------------------------------


def test_inbox_events_total_tracks_results() -> None:
    metrics = PlatformMetrics()
    metrics.inbox_events_total.labels(result="completed").inc()
    metrics.inbox_events_total.labels(result="failed").inc()
    assert metric_sample_value(metrics, "platform_inbox_events_total", result="completed") == 1.0
    assert metric_sample_value(metrics, "platform_inbox_events_total", result="failed") == 1.0


def test_stale_claims_reclaimed_total_is_incrementable() -> None:
    metrics = PlatformMetrics()
    metrics.stale_claims_reclaimed_total.inc(5)
    assert metric_sample_value(metrics, "platform_stale_claims_reclaimed_total") == 5.0


# --- HTTP metrics --------------------------------------------------------


def test_http_requests_total_tracks_methods_and_routes() -> None:
    metrics = PlatformMetrics()
    metrics.http_requests_total.labels(
        method="GET", route="/v1/cases", status="200", outcome="ok"
    ).inc()
    metrics.http_requests_total.labels(
        method="POST", route="/v1/cases", status="403", outcome="denied"
    ).inc()
    assert (
        metric_sample_value(
            metrics,
            "platform_http_requests_total",
            method="GET",
            route="/v1/cases",
            status="200",
            outcome="ok",
        )
        == 1.0
    )
    assert (
        metric_sample_value(
            metrics,
            "platform_http_requests_total",
            method="POST",
            route="/v1/cases",
            status="403",
            outcome="denied",
        )
        == 1.0
    )


# --- Policy denials ------------------------------------------------------


def test_policy_denials_total_tracks_by_action_and_reason() -> None:
    metrics = PlatformMetrics()
    metrics.policy_denials_total.labels(action="case.read", reason_code="tenant_mismatch").inc()
    assert (
        metric_sample_value(
            metrics,
            "platform_policy_denials_total",
            action="case.read",
            reason_code="tenant_mismatch",
        )
        == 1.0
    )


# --- Render --------------------------------------------------------------


def test_render_metrics_produces_prometheus_text() -> None:
    metrics = PlatformMetrics()
    metrics.observe_run(outcome="completed", route="knowledge_qa", latency_seconds=0.5)
    body = render_metrics(metrics)
    assert isinstance(body, bytes)
    text = body.decode("utf-8")
    assert "platform_agent_runs_total" in text
    assert "platform_retrieval_latency_seconds" in text


def test_render_metrics_default_uses_process_registry() -> None:
    reset_default_metrics()
    get_metrics().observe_run(outcome="abstained", route="human_required", latency_seconds=0.1)
    body = render_metrics()
    assert isinstance(body, bytes)
    assert b"platform_agent_runs_total" in body
    reset_default_metrics()
