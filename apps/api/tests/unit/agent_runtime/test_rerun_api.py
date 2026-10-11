from __future__ import annotations

import asyncio
import hashlib
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from platform_core.agent_runtime import rerun, router
from platform_core.identity.tenant_context import TenantContext


class _Session:
    async def commit(self) -> None:
        return None


def _request(ctx: TenantContext, key: str | None) -> Request:
    headers = [(b"idempotency-key", key.encode())] if key is not None else []
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/conversations/agent-runs/failed/rerun",
            "query_string": b"",
            "headers": headers,
            "server": ("test", 80),
            "client": ("127.0.0.1", 1),
            "scheme": "http",
        }
    )
    request.state.tenant_context = ctx
    return request


def test_rerun_api_hashes_key_and_audits_human_operator_request(monkeypatch) -> None:
    tenant_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    run_id = uuid.uuid4()
    replacement_id = uuid.uuid4()
    ctx = TenantContext(
        tenant_id=tenant_id,
        actor_id=actor_id,
        actor_kind="user",
        role="support_admin",
    )
    calls: dict[str, Any] = {"rerun": [], "audit": []}

    @asynccontextmanager
    async def fake_tenant_session(_ctx):
        yield _Session()

    async def fake_rerun(_session, **kwargs):
        calls["rerun"].append(kwargs)
        return rerun.RerunRef(new_run_id=replacement_id, rerun_of=run_id, status="running")

    async def fake_audit(_session, **kwargs):
        calls["audit"].append(kwargs)
        return uuid.uuid4()

    monkeypatch.setattr(router, "tenant_session", fake_tenant_session)
    monkeypatch.setattr(rerun, "rerun_failed_run", fake_rerun)
    monkeypatch.setattr(router.audit_service, "record", fake_audit)
    raw_key = "operator-action-123"

    response = asyncio.run(router.rerun_agent_run(_request(ctx, raw_key), run_id))

    assert response["run_id"] == str(replacement_id)
    assert response["rerun_of"] == str(run_id)
    assert response["status"] == "running"
    assert calls["rerun"][0]["actor_ref"] == str(actor_id)
    assert calls["rerun"][0]["request_key_hash"] == hashlib.sha256(raw_key.encode()).hexdigest()
    assert calls["audit"][0]["action"] == "agent_run.rerun_requested"
    assert calls["audit"][0]["resource_id"] == run_id
    assert "operator-action-123" not in repr(calls["audit"])


def test_rerun_api_requires_an_idempotency_key(monkeypatch) -> None:
    ctx = TenantContext(
        tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), actor_kind="user", role="support_admin"
    )
    monkeypatch.setattr(router, "tenant_session", lambda _ctx: None)
    response = asyncio.run(router.rerun_agent_run(_request(ctx, None), uuid.uuid4()))
    assert isinstance(response, JSONResponse)
    assert response.status_code == 400


def test_rerun_api_requires_a_human_actor() -> None:
    ctx = TenantContext(
        tenant_id=uuid.uuid4(), actor_id=uuid.uuid4(), actor_kind="customer", role="support_admin"
    )
    response = asyncio.run(router.rerun_agent_run(_request(ctx, "request-key"), uuid.uuid4()))
    assert isinstance(response, JSONResponse)
    assert response.status_code == 403
