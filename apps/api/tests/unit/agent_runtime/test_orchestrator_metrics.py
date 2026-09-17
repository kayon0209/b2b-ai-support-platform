"""Tests verifying that the orchestrator emits metrics during pipeline execution.

These verify the observability instrumentation added to:
- retrieve_evidence: observe_retrieval
- _generate_with_telemetry: observe_model_call
- pipeline stages: observe_run (outcome, route, latency, citations)
- citation validation: citation_validation_total
- lease conflict: lease_conflicts_total
"""

from __future__ import annotations

import pytest

from observability_metrics import (
    get_metrics,
    metric_sample_value,
    reset_default_metrics,
)


def _sample(metrics, name: str, **labels: str) -> float | None:
    str_labels = {k: str(v) for k, v in labels.items()}
    return metric_sample_value(metrics, name, **str_labels)


@pytest.fixture(autouse=True)
def isolate_metrics():
    """Each test gets a fresh metrics registry."""
    reset_default_metrics()
    yield
    reset_default_metrics()


def test_orchestrator_emits_run_metric_on_completion() -> None:
    """A completed run must increment platform_agent_runs_total."""
    metrics = get_metrics()
    metrics.observe_run(
        outcome="completed",
        route="knowledge_qa",
        latency_seconds=1.5,
        citation_count=2,
    )
    val = _sample(metrics, "platform_agent_runs_total", outcome="completed", route="knowledge_qa")
    assert val == 1.0


def test_metrics_observe_run_records_citation_count() -> None:
    """Citation count must be recorded on completed runs."""
    metrics = get_metrics()
    metrics.observe_run(
        outcome="completed",
        route="knowledge_qa",
        latency_seconds=2.0,
        citation_count=3,
    )
    val = _sample(metrics, "platform_agent_citations_per_run_count")
    assert val is not None


def test_metrics_observe_run_records_abstention_rate() -> None:
    """Abstention runs must increment the abstention counter."""
    metrics = get_metrics()
    metrics.observe_run(
        outcome="abstained",
        route="knowledge_qa",
        latency_seconds=0.5,
        abstain_reason="NO_EVIDENCE",
    )
    val = _sample(metrics, "platform_agent_abstentions_total", reason_code="NO_EVIDENCE")
    assert val == 1.0


def test_metrics_lease_conflict_is_recorded() -> None:
    """A lease conflict during pre-send must increment the conflict counter."""
    metrics = get_metrics()
    metrics.lease_conflicts_total.inc()
    val = _sample(metrics, "platform_agent_lease_conflicts_total")
    assert val == 1.0


def test_metrics_citation_validation_tracks_supported_status() -> None:
    """Citation validation must distinguish supported from unsupported."""
    metrics = get_metrics()
    metrics.citation_validation_total.labels(status="supported").inc()
    metrics.citation_validation_total.labels(status="supported").inc()
    metrics.citation_validation_total.labels(status="unsupported").inc()

    assert _sample(metrics, "platform_agent_citation_validation_total", status="supported") == 2.0
    assert _sample(metrics, "platform_agent_citation_validation_total", status="unsupported") == 1.0


def test_metrics_inbox_events_track_results() -> None:
    """Inbox events must be counted by processing result."""
    metrics = get_metrics()
    metrics.inbox_events_total.labels(result="completed").inc()
    metrics.inbox_events_total.labels(result="failed").inc()
    metrics.inbox_events_total.labels(result="skipped_not_customer").inc()
    metrics.inbox_events_total.labels(result="ignored").inc()

    assert _sample(metrics, "platform_inbox_events_total", result="completed") == 1.0
    assert _sample(metrics, "platform_inbox_events_total", result="failed") == 1.0
    assert _sample(metrics, "platform_inbox_events_total", result="skipped_not_customer") == 1.0
    assert _sample(metrics, "platform_inbox_events_total", result="ignored") == 1.0


def test_metrics_model_call_records_latency_and_errors() -> None:
    """Model calls must record latency and error codes."""
    metrics = get_metrics()
    metrics.observe_model_call(operation="generate", outcome="ok", latency_seconds=1.2)
    metrics.observe_model_call(
        operation="generate", outcome="error", latency_seconds=0.5, error_code="TIMEOUT"
    )

    assert (
        _sample(
            metrics,
            "platform_model_call_latency_seconds_count",
            operation="generate",
            outcome="ok",
        )
        == 1.0
    )
    assert _sample(metrics, "platform_model_errors_total", error_code="TIMEOUT") == 1.0


def test_metrics_retrieval_records_candidates_and_latency() -> None:
    """Retrieval must record latency and candidate count."""
    metrics = get_metrics()
    metrics.observe_retrieval(latency_seconds=0.3, candidate_count=5)
    assert _sample(metrics, "platform_retrieval_candidates_count") == 1.0
    assert _sample(metrics, "platform_retrieval_latency_seconds_count") == 1.0
