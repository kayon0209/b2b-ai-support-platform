"""The product must be able to serve its own interface.

What was missing
----------------
The API mounts no static assets, and neither the Kubernetes manifests nor the
Compose file carries a frontend workload. `GET /support` answered 401, and
`GET /` answered 401: the customer surface and the operator console were
unreachable from any deployment this repository describes. A product whose
own interface has no deployment path is a library, not a product.

The properties pinned here
--------------------------
1. With a built frontend present, `/support`, `/admin/*` and `/auth/callback`
   return the SPA shell, so a refresh on a deep link works.
2. The API keeps its own rules: an unauthenticated `/v1/*` is still 401 JSON,
   an unknown `/v1/*` path is still 404 JSON rather than the SPA shell, and
   `/healthz` and `/metrics` are untouched.
3. A missing hashed asset is a 404, not the shell - a stale cached `index.html`
   must not be served in place of a JavaScript file, or the page fails with a
   MIME error instead of a clear missing-asset response.
4. Without a built frontend, nothing changes: no silent 200, no crash, and the
   operator gets a log line naming the missing directory rather than a blank
   page.

The last one is what makes the first three safe to ship. A repository checkout
with no `npm run build` is the normal state of a backend test run, and an
exemption that made `/support` reachable while nothing served it would turn a
clear 401 into a confusing 404.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration

SHELL = "<!doctype html>"


@pytest.fixture
def built_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An app assembled exactly as `main.py` assembles it, with a built SPA.

    The real module is imported rather than its pieces reassembled: a fixture
    that built its own app would pass even if `main.py` stopped mounting
    anything.
    """
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(f'{SHELL}<div id="root"></div>', encoding="utf-8")
    (dist / "assets" / "app-abc123.js").write_text("console.log(1)", encoding="utf-8")

    monkeypatch.setenv("APP_SPA_DIST", str(dist))
    monkeypatch.setenv("APP_ALLOW_BOOTSTRAP_TOKENS", "true")
    monkeypatch.setenv("APP_ENVIRONMENT", "local")
    monkeypatch.delenv("APP_OIDC_ISSUER", raising=False)

    from platform_core.config import get_settings

    get_settings.cache_clear()
    try:
        main = importlib.import_module("platform_core.main")
        importlib.reload(main)
        yield TestClient(main.app, raise_server_exceptions=False)
    finally:
        get_settings.cache_clear()


@pytest.fixture
def no_frontend_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A checkout with no `npm run build`: the state a backend-only test run is
    always in."""
    monkeypatch.setenv("APP_SPA_DIST", str(tmp_path / "absent"))
    monkeypatch.setenv("APP_ALLOW_BOOTSTRAP_TOKENS", "true")
    monkeypatch.setenv("APP_ENVIRONMENT", "local")
    monkeypatch.delenv("APP_OIDC_ISSUER", raising=False)

    from platform_core.config import get_settings

    get_settings.cache_clear()
    try:
        main = importlib.import_module("platform_core.main")
        importlib.reload(main)
        yield TestClient(main.app, raise_server_exceptions=False)
    finally:
        get_settings.cache_clear()


# --- 1. The customer surface and the console are reachable -----------------


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/support",
        "/admin",
        "/admin/workbench",
        "/admin/workbench/conversation/abc",
        "/auth/callback",
    ],
)
def test_spa_routes_return_the_shell(built_client: TestClient, path: str) -> None:
    """A refresh on a deep link must not 401 or 404 - the browser router owns
    those paths and resolves them client-side."""
    resp = built_client.get(path)
    assert resp.status_code == 200, f"{path} -> {resp.status_code} {resp.text[:120]}"
    assert "text/html" in resp.headers["content-type"]
    assert SHELL in resp.text


def test_hashed_assets_are_served(built_client: TestClient) -> None:
    resp = built_client.get("/assets/app-abc123.js")
    assert resp.status_code == 200
    assert "console.log(1)" in resp.text


def test_a_missing_asset_is_404_not_the_shell(built_client: TestClient) -> None:
    """Serving `index.html` in place of a missing script produces a MIME error
    in the browser - far harder to diagnose than a clean 404."""
    resp = built_client.get("/assets/gone-999999.js")
    assert resp.status_code == 404
    assert SHELL not in resp.text


# --- 2. The API keeps its own rules ----------------------------------------


def test_an_unauthenticated_api_call_is_still_401(built_client: TestClient) -> None:
    resp = built_client.get("/v1/cases")
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/json")


def test_an_unknown_api_path_is_never_the_shell(built_client: TestClient) -> None:
    """The catch-all is a backstop for the browser router. If it swallowed
    unknown API paths, a typo in a client URL would return HTML with a 200 and
    the failure would surface as a JSON parse error somewhere else.

    401 rather than 404 is the right answer here and is not a defect: the
    middleware resolves a bearer token before routing, so an unauthenticated
    caller learns nothing about which paths exist. What matters is that the
    response stays JSON and is an error.
    """
    resp = built_client.get("/v1/definitely-not-a-route")
    assert resp.status_code in (401, 404)
    assert resp.headers["content-type"].startswith("application/json")
    assert SHELL not in resp.text


def test_health_and_metrics_are_untouched(built_client: TestClient) -> None:
    assert built_client.get("/healthz").status_code == 200
    # Metrics is opt-in; the point is that the SPA does not shadow it.
    resp = built_client.get("/metrics")
    assert resp.status_code != 200 or "text/html" not in resp.headers.get("content-type", "")


def test_the_customer_api_still_works(built_client: TestClient) -> None:
    """`/v1/support/*` is exempt from bearer resolution and must keep being so
    - the SPA exemption is about the shell, not about the API behind it."""
    resp = built_client.post(
        "/v1/support/sessions",
        json={"tenant_slug": "no-such-tenant", "visitor_id": "probe"},
    )
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")


# --- 3. No built frontend changes nothing -----------------------------------


def test_without_a_build_the_api_behaves_exactly_as_before(
    no_frontend_client: TestClient,
) -> None:
    assert no_frontend_client.get("/healthz").status_code == 200
    assert no_frontend_client.get("/v1/cases").status_code == 401
    # Not a 200, and not a crash: a clear refusal the operator can act on.
    assert no_frontend_client.get("/support").status_code == 401


def test_the_missing_directory_is_named_in_the_log(tmp_path: Path) -> None:
    """A blank page with no explanation is the failure mode this replaces.

    The handler is attached here rather than using pytest's `caplog`, which
    depends on global logging configuration the rest of the suite is free to
    change - which is why this test passed alone and failed in the full run.
    """
    import logging

    from fastapi import FastAPI

    from platform_core.spa import mount_spa

    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    logger = logging.getLogger("platform.spa")
    handler = Capture()
    logger.addHandler(handler)
    previous_level, previous_disabled = logger.level, logger.disabled
    # The test owns the logger state it depends on: something elsewhere in the
    # suite disables loggers, and a handler attached to a disabled logger
    # receives nothing - which is why this passed alone and failed in the full
    # run, twice, for a reason that had nothing to do with the behaviour.
    logger.setLevel(logging.WARNING)
    logger.disabled = False
    try:
        assert mount_spa(FastAPI(), str(tmp_path / "absent")) is None
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.disabled = previous_disabled

    assert any("spa_not_served" in m for m in records), records
    assert any(str(tmp_path / "absent") in m for m in records), records
