"""Unit tests: tenant context invariants (ticket 3).

Core security property: TenantContext can only be constructed from
server-side resolution; middleware fails closed without credentials.
"""

import uuid

import pytest
from fastapi import FastAPI, Request
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
    """A resolvable token yields the context the resolver returned.

    The resolver is stubbed because real resolution needs Postgres: this
    unit test pins the *middleware* contract (resolve, attach, expose to the
    request), not the lookup. The lookup itself is covered against a real
    database in `tests/integration/test_membership_resolution.py`.

    A previous version of this test used the real `bootstrap_token_resolver`
    against a non-existent slug and asserted 200, which only passed because
    the resolver synthesised a tenant id with `uuid5` instead of reading the
    database. That is exactly the bug the integration suite now pins.
    """
    resolved = uuid.uuid4()
    actor = uuid.uuid4()

    async def resolver(request: object) -> TenantContext:
        return TenantContext(
            tenant_id=resolved, actor_id=actor, actor_kind="user", role="support_agent"
        )

    app = FastAPI()

    @app.get("/whoami")
    def whoami() -> dict:
        ctx = get_tenant_context()
        return {"actor_id": str(ctx.actor_id), "tenant_id": str(ctx.tenant_id)}

    app.add_middleware(TenantContextMiddleware, resolver=resolver)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/whoami", headers={"Authorization": f"Bearer pt_acme_{actor}"})
    assert resp.status_code == 200
    body = resp.json()
    # The context comes from the resolver, not from any client-supplied field.
    assert body["actor_id"] == str(actor)
    assert body["tenant_id"] == str(resolved)


def test_unresolvable_token_is_401_not_a_synthetic_context() -> None:
    """The real resolver against a slug that does not exist must fail closed.

    This is the regression guard for the uuid5 synthesis: the old code
    returned a role-less context for *any* well-formed token, so the caller
    saw 200 and then a 403 from every endpoint. A token that cannot be
    matched to a real membership must be rejected at the door.
    """
    app = FastAPI()

    @app.get("/whoami")
    def whoami() -> dict:
        return {"ok": True}

    app.add_middleware(TenantContextMiddleware, resolver=bootstrap_token_resolver)
    client = TestClient(app, raise_server_exceptions=False)

    actor = uuid.uuid4()
    resp = client.get(
        "/whoami",
        headers={"Authorization": f"Bearer pt_no-such-tenant-slug_{actor}"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "AUTH_UNRESOLVED"


def test_middleware_exposes_context_on_request_state() -> None:
    """`request.state.tenant_context` is set for endpoints that read it there.

    The module-level global does not survive into the endpoint's task under
    Starlette's task-per-request model, so the state attribute is the
    reliable channel and must keep working.
    """
    resolved = uuid.uuid4()

    async def resolver(request: object) -> TenantContext:
        return TenantContext(tenant_id=resolved, actor_id=None, actor_kind="system")

    app = FastAPI()

    @app.get("/state")
    def state(request: Request) -> dict:
        ctx = request.state.tenant_context
        return {"tenant_id": str(ctx.tenant_id)}

    app.add_middleware(TenantContextMiddleware, resolver=resolver)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/state", headers={"Authorization": "Bearer pt_any_whatever"})
    assert resp.status_code == 200
    assert resp.json()["tenant_id"] == str(resolved)


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
