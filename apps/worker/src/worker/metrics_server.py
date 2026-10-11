"""Private Prometheus exposition for a single worker process.

The worker and API share metric definitions but not process memory. Each
worker pod therefore serves its own registry. The default bind is loopback;
the Kubernetes ConfigMap opts into pod networking, and its NetworkPolicy
allows only the monitoring namespace to scrape the otherwise unauthenticated
endpoint.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any

from observability_metrics import get_metrics, render_metrics

_VALID_QUEUES = frozenset(
    {"interactive", "semantic", "ingestion", "outbox", "release_evaluator", "retention", "sla"}
)


class _MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib HTTP protocol method
        if self.path != "/metrics":
            self.send_error(404)
            return
        payload = render_metrics()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: Any) -> None:
        # Scrape requests are frequent and carry no diagnostic value. Avoid
        # turning them into unstructured request logs.
        return


class WorkerMetricsServer:
    """Context-managed HTTP listener for one worker's Prometheus registry."""

    def __init__(self, *, queue: str, host: str, port: int) -> None:
        if queue not in _VALID_QUEUES:
            raise ValueError("unknown worker queue")
        self.queue = queue
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        if self._server is None:
            raise RuntimeError("metrics server has not started")
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def __enter__(self) -> WorkerMetricsServer:
        self._server = ThreadingHTTPServer((self.host, self.port), _MetricsHandler)
        self._server.daemon_threads = True
        self._thread = Thread(target=self._server.serve_forever, name="worker-metrics", daemon=True)
        self._thread.start()
        get_metrics().worker_process_up.labels(queue=self.queue).set(1)
        return self

    def close(self) -> None:
        if self._server is None:
            return
        get_metrics().worker_process_up.labels(queue=self.queue).set(0)
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None

    def __exit__(self, *_exc: object) -> None:
        self.close()


__all__ = ["WorkerMetricsServer"]
