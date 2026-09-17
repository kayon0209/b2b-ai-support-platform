"""Unit tests for the observability HTTP router and metrics middleware.

Covers:
- HttpMetricsMiddleware records request counts by route template
- HttpMetricsMiddleware does not leak tenant identifiers into labels
- Error responses are still counted (not just 2xx)
- /metrics endpoint respects the exposure gate
"""

from __future__ import annotations

from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from observability_metrics import (
    PlatformMetrics,
    get_metrics,
    metric_sample_value,
    reset_default_metrics,
)
from platform_core.config import Settings
from platform_core.http_metrics import UNMATCHED_ROUTE, HttpMetricsMiddleware, route_template


def _sample(metrics: PlatformMetrics, name: str, **labels: object) -> float | None:
    str_labels = {k: str(v) for k, v in labels.items()}
    return metric_sample_value(metrics, name, **str_labels)


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(HttpMetricsMiddleware)

    @app.get("/v1/cases")
    def list_cases(request: Request) -> JSONResponse:
        return JSONResponse({"rows": []})

    @app.get("/v1/cases/{case_id}")
    def get_case(request: Request, case_id: str) -> JSONResponse:
        return JSONResponse({"id": case_id})

    @app.post("/v1/cases")
    def create_case(request: Request) -> JSONResponse:
        return JSONResponse({"created": True}, status_code=201)

    @app.get("/v1/cases-error")
    def cases_error(request: Request) -> JSONResponse:
        return JSONResponse({"error": "bad"}, status_code=400)

    return app


# --- HttpMetricsMiddleware ------------------------------------------------


def test_http_middleware_counts_successful_requests() -> None:
    reset_default_metrics()
    app = _make_app()
    client = TestClient(app)
    resp = client.get("/v1/cases")
    assert resp.status_code == 200
    metrics = get_metrics()
    val = _sample(
        metrics,
        "platform_http_requests_total",
        method="GET",
        route="/v1/cases",
        status="200",
        outcome="ok",
    )
    assert val == 1.0
    reset_default_metrics()


def test_http_middleware_counts_errors() -> None:
    reset_default_metrics()
    app = _make_app()
    client = TestClient(app)
    resp = client.get("/v1/cases-error")
    assert resp.status_code == 400
    metrics = get_metrics()
    val = _sample(
        metrics,
        "platform_http_requests_total",
        method="GET",
        route="/v1/cases-error",
        status="400",
        outcome="error",
    )
    assert val == 1.0
    reset_default_metrics()


def test_http_middleware_uses_route_template_not_path() -> None:
    """Path parameters must not become metric labels - they are unbounded
    identifiers that would create a memory leak and could leak tenant data."""
    reset_default_metrics()
    app = _make_app()
    client = TestClient(app)
    client.get("/v1/cases/abc-123")
    client.get("/v1/cases/def-456")
    metrics = get_metrics()
    val = _sample(
        metrics,
        "platform_http_requests_total",
        method="GET",
        route="/v1/cases/{case_id}",
        status="200",
        outcome="ok",
    )
    assert val == 2.0
    reset_default_metrics()


def test_http_middleware_does_not_track_head_options() -> None:
    """HEAD/OPTIONS are probes/preflights and should not inflate request rates."""
    reset_default_metrics()
    app = _make_app()
    client = TestClient(app)
    client.head("/v1/cases")
    metrics = get_metrics()
    val = _sample(metrics, "platform_http_requests_total", method="HEAD", route="/v1/cases")
    assert val is None
    reset_default_metrics()


def test_http_middleware_no_uuid_in_labels() -> None:
    """No label value should contain a UUID that looks like a path parameter."""
    reset_default_metrics()
    app = _make_app()
    client = TestClient(app)
    client.get("/v1/cases/550e8400-e29b-41d4-a716-446655440000")
    from prometheus_client import generate_latest

    body = generate_latest(get_metrics().registry).decode("utf-8")
    assert "550e8400-e29b-41d4-a716-446655440000" not in body
    reset_default_metrics()


# --- route_template helper ------------------------------------------------


def test_route_template_returns_matched_route_path() -> None:
    """The route_template function reads the matched route from scope."""
    reset_default_metrics()
    app = _make_app()
    client = TestClient(app)
    captured_routes: list[str] = []

    @app.get("/v1/test-capture")
    def capture(request: Request) -> JSONResponse:
        captured_routes.append(route_template(request))
        return JSONResponse({"ok": True})

    client.get("/v1/test-capture")
    assert "/v1/test-capture" in captured_routes
    reset_default_metrics()


def test_route_template_returns_unmatched_when_no_route() -> None:
    """When no route is matched, the template is the unmatched bucket."""
    request = Request({"type": "http", "method": "GET", "path": "/nonexistent", "route": None})
    assert route_template(request) == UNMATCHED_ROUTE


# --- Observability router ------------------------------------------------


def test_metrics_endpoint_serves_when_enabled_in_local() -> None:
    reset_default_metrics()
    get_metrics().observe_run(outcome="completed", route="knowledge_qa", latency_seconds=0.5)
    from platform_core.observability_router import router as obs_router

    app = FastAPI()
    app.include_router(obs_router)
    client = TestClient(app)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("text/plain")
    assert b"platform_" in resp.content
    reset_default_metrics()


def test_metrics_endpoint_404_when_disabled_in_production() -> None:
    reset_default_metrics()
    from platform_core.observability_router import router as obs_router

    app = FastAPI()
    app.include_router(obs_router)

    disabled = Settings(
        environment="production",
        metrics_enabled=False,
    )
    with patch("platform_core.observability_router.get_settings", return_value=disabled):
        client = TestClient(app)
        resp = client.get("/metrics")
        assert resp.status_code == 404
    reset_default_metrics()


def test_metrics_endpoint_404_explicit_disable_in_local() -> None:
    reset_default_metrics()
    from platform_core.observability_router import router as obs_router

    app = FastAPI()
    app.include_router(obs_router)

    disabled = Settings(
        environment="local",
        metrics_enabled=False,
    )
    with patch("platform_core.observability_router.get_settings", return_value=disabled):
        client = TestClient(app)
        resp = client.get("/metrics")
        assert resp.status_code == 404
    reset_default_metrics()


def test_metrics_enabled_defaults_on_in_local() -> None:
    from platform_core.observability_router import metrics_enabled

    s = Settings(environment="local")
    with patch("platform_core.observability_router.get_settings", return_value=s):
        assert metrics_enabled() is True


def test_metrics_enabled_defaults_off_in_production() -> None:
    from platform_core.observability_router import metrics_enabled

    s = Settings(environment="production")
    with patch("platform_core.observability_router.get_settings", return_value=s):
        assert metrics_enabled() is False
