from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError


class _Connection:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def execute(self, _statement: Any) -> None:
        if self.error is not None:
            raise self.error


class _Engine:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    @asynccontextmanager
    async def connect(self):
        yield _Connection(self.error)


def test_readyz_reports_database_ready_without_exposing_connection_details(
    monkeypatch,
) -> None:
    from platform_core import main

    monkeypatch.setattr(main.db, "app_role_url", lambda: "test-dsn")
    monkeypatch.setattr(main.db, "get_engine", lambda _url: _Engine())
    response = TestClient(main.app).get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok"}}


def test_readyz_fails_closed_and_keeps_database_error_private(monkeypatch) -> None:
    from platform_core import main

    secret_marker = "private-dsn-marker"
    monkeypatch.setattr(main.db, "app_role_url", lambda: "test-dsn")
    monkeypatch.setattr(main.db, "get_engine", lambda _url: _Engine(RuntimeError(secret_marker)))
    response = TestClient(main.app).get("/readyz")
    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"database": "unavailable"},
    }
    assert secret_marker not in response.text


def test_healthz_remains_a_liveness_probe_when_database_is_unavailable(monkeypatch) -> None:
    from platform_core import main

    monkeypatch.setattr(main.db, "app_role_url", lambda: "test-dsn")
    monkeypatch.setattr(main.db, "get_engine", lambda _url: _Engine(RuntimeError("offline")))
    response = TestClient(main.app).get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_authenticated_case_read_fails_closed_when_database_is_unavailable(monkeypatch) -> None:
    from platform_core import main
    from platform_core.cases import router as cases_router
    from platform_core.identity.middleware import TenantContextMiddleware
    from platform_core.identity.tenant_context import TenantContext

    secret_marker = "private-db-dsn-marker"
    tenant_context = TenantContext(
        tenant_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        actor_kind="user",
        role="support_viewer",
    )

    class Resolver:
        async def __call__(self, _request):
            return tenant_context

    @asynccontextmanager
    async def database_unavailable(_ctx):
        raise OperationalError("SELECT cases", {}, RuntimeError(secret_marker))
        yield _Connection()  # pragma: no cover - keeps this an async generator

    monkeypatch.setattr(cases_router, "tenant_session", database_unavailable)
    test_app = FastAPI()
    test_app.include_router(cases_router.router)
    test_app.add_exception_handler(OperationalError, main.database_operational_error)
    test_app.add_middleware(TenantContextMiddleware, resolver=Resolver())

    response = TestClient(test_app, raise_server_exceptions=False).get("/v1/cases")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert response.json()["trace_id"]
    assert secret_marker not in response.text
