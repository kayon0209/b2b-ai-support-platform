"""Unit tests: tenant context invariants (ticket 3).

Core security property: TenantContext can only be constructed from
server-side resolution; middleware fails closed without credentials.
"""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from platform_core.identity.middleware import TenantContextMiddleware, bootstrap_token_resolver
from platform_core.identity.tenant_context import (
    TenantContext,
    TenantContextError,
    clear_tenant_context,
    get_tenant_context,
    set_tenant_context,
)


def test_context_roundtrip() -> None:
    tid = uuid.uuid4()
    aid = uuid.uuid4()
    ctx = TenantContext(tenant_id=tid, actor_id=aid, actor_kind="user", role="support_agent")
    set_tenant_context(ctx)
    assert get_tenant_context() is ctx
    clear_tenant_context()


def test_unresolved_context_fails_closed() -> None:
    clear_tenant_context()
    with pytest.raises(TenantContextError):
        get_tenant_context()


def test_middleware_rejects_missing_auth() -> None:
    app = FastAPI()

    @app.get("/whoami")
    def whoami() -> dict:
        ctx = get_tenant_context()
        return {"tenant_id": str(ctx.tenant_id)}

    app.add_middleware(TenantContextMiddleware, resolver=bootstrap_token_resolver)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/whoami")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTH_UNRESOLVED"


def test_middleware_resolves_server_side_context() -> None:
    app = FastAPI()

    @app.get("/whoami")
    def whoami() -> dict:
        ctx = get_tenant_context()
        return {"actor_id": str(ctx.actor_id)}

    app.add_middleware(TenantContextMiddleware, resolver=bootstrap_token_resolver)
    client = TestClient(app, raise_server_exceptions=False)

    actor = uuid.uuid4()
    resp = client.get("/whoami", headers={"Authorization": f"Bearer pt_acme_{actor}"})
    assert resp.status_code == 200
    # tenant_id is derived from slug server-side (uuid5), not from any client field
    assert resp.json()["actor_id"] == str(actor)


def test_exempt_paths_skip_resolution() -> None:
    app = FastAPI()

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    app.add_middleware(TenantContextMiddleware, resolver=bootstrap_token_resolver)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/healthz")
    assert resp.status_code == 200


def test_malformed_tokens_rejected() -> None:
    app = FastAPI()
    app.add_middleware(TenantContextMiddleware, resolver=bootstrap_token_resolver)
    client = TestClient(app, raise_server_exceptions=False)

    for token in ("", "Bearer garbage", "Bearer xx_yy", "Bearer pt__not-a-uuid"):
        resp = client.get("/anything", headers={"Authorization": token} if token else {})
        assert resp.status_code == 401, f"token {token!r} should be rejected"
