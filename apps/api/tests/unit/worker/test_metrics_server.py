from __future__ import annotations

from http.client import HTTPConnection

import pytest

from observability_metrics import get_metrics, metric_sample_value, reset_default_metrics
from worker.metrics_server import WorkerMetricsServer


def test_worker_exposes_its_process_registry_and_only_the_metrics_path() -> None:
    reset_default_metrics()
    with WorkerMetricsServer(queue="interactive", host="127.0.0.1", port=0) as server:
        host, port = server.address
        connection = HTTPConnection(host, port, timeout=2)
        connection.request("GET", "/metrics")
        response = connection.getresponse()
        payload = response.read().decode()
        assert response.status == 200
        assert 'platform_worker_process_up{queue="interactive"} 1.0' in payload
        assert "platform_inbox_claim_age_seconds" in payload
        connection.close()

        connection = HTTPConnection(host, port, timeout=2)
        connection.request("GET", "/")
        response = connection.getresponse()
        response.read()
        assert response.status == 404
        connection.close()
    assert (
        metric_sample_value(get_metrics(), "platform_worker_process_up", queue="interactive") == 0.0
    )


def test_worker_metrics_server_rejects_unbounded_queue_labels() -> None:
    with pytest.raises(ValueError, match="unknown worker queue"):
        WorkerMetricsServer(queue="tenant-controlled", host="127.0.0.1", port=0)
