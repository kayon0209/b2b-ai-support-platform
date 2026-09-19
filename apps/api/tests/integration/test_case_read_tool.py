"""Integration: case.read internal executor against the real schema."""

from __future__ import annotations

import asyncio
import os
import uuid as _uuid

import pytest
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)

TENANT = "01900000-0000-7000-8000-000000000043"


def test_case_read_executor_finds_by_subject_number() -> None:
    from platform_core.db import app_role_url, session_scope_with_url
    from platform_core.tool_gateway.case_read import CaseReadExecutor

    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES (:i, :s, :n, 'active') "
                "ON CONFLICT (slug) DO NOTHING"
            ),
            {"i": TENANT, "s": "case-read", "n": "Case Read"},
        )
        conn.execute(text("DELETE FROM cases WHERE tenant_id = :t"), {"t": TENANT})
        case_id = str(_uuid.uuid4())
        conn.execute(
            text(
                "INSERT INTO cases (id, tenant_id, subject, description, status, "
                "priority, version, opened_at, elapsed_running_seconds, "
                "last_state_changed_at) VALUES (:i, :t, 'case 12345 broken login', "
                "'', 'open', 'p2', 1, 0, 0, 0)"
            ),
            {"i": case_id, "t": TENANT},
        )
    admin.dispose()

    async def run() -> tuple[dict[str, object] | None, dict[str, object] | None]:
        from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant

        async with session_scope_with_url(app_role_url()) as session:
            ctx = TenantContext(tenant_id=_uuid.UUID(TENANT), actor_id=None, actor_kind="system")
            await apply_rls_tenant(session, ctx)
            executor = CaseReadExecutor(session)
            found = await executor.execute("case.read", {"case_ref": "12345"}, "k1")
            missing = await executor.execute("case.read", {"case_ref": "99999"}, "k2")
        return found, missing

    found, missing = asyncio.run(run())
    assert isinstance(found, dict) and found["found"] is True
    assert found["case"]["status"] == "open"  # type: ignore[index]
    assert isinstance(missing, dict) and missing["found"] is False


def test_case_read_never_sees_other_tenants() -> None:
    from platform_core.db import app_role_url, session_scope_with_url
    from platform_core.tool_gateway.case_read import CaseReadExecutor

    async def run() -> dict[str, object] | None:
        from platform_core.identity.tenant_context import TenantContext, apply_rls_tenant

        async with session_scope_with_url(app_role_url()) as session:
            ctx = TenantContext(
                tenant_id=_uuid.UUID("01900000-0000-7000-8000-000000000044"),
                actor_id=None,
                actor_kind="system",
            )
            await apply_rls_tenant(session, ctx)
            executor = CaseReadExecutor(session)
            return await executor.execute("case.read", {"case_ref": "12345"}, "k3")

    result = asyncio.run(run())
    assert isinstance(result, dict) and result["found"] is False
