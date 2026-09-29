"""Integration tests: audit service wired into case commands (ticket 24)."""

import os
import uuid

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = os.environ.get(
    "APP_TEST_DATABASE_URL",
    "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform",
)
TENANT_A = "01900000-0000-7000-8000-000000000001"


def _run(coro):
    import asyncio

    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


@pytest.fixture(scope="module", autouse=True)
def seed_tenant() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, 'audit-test', 'Audit Tenant', 'active') "
                "ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT_A},
        )
    yield
    with admin.begin() as conn:
        conn.execute(text("DELETE FROM audit_events WHERE tenant_id = :t"), {"t": TENANT_A})
        conn.execute(text("DELETE FROM cases WHERE tenant_id = :t"), {"t": TENANT_A})
        conn.execute(text("DELETE FROM tenants WHERE slug = 'audit-test'"))
    admin.dispose()


def test_case_command_writes_audit_event() -> None:
    from platform_core.audit.service import record as audit_record
    from platform_core.cases.service import CaseService
    from platform_core.db import create_engine
    from platform_core.identity.tenant_context import TenantContext

    async def scenario() -> int:
        engine = create_engine(APP_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        tid = uuid.UUID(TENANT_A)
        ctx = TenantContext(
            tenant_id=tid, actor_id=uuid.uuid4(), actor_kind="user", role="support_agent"
        )
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_A}
            )
            svc = CaseService(session)
            case = await svc.create_case(tenant_id=tid, subject="audit me", priority="p2")
            await audit_record(
                session,
                ctx=ctx,
                action="case.create",
                resource_type="case",
                resource_id=case.id,
                after={"status": "new", "priority": "p2"},
            )
            await session.commit()

            # commit() ended the transaction; re-apply RLS context.
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_A}
            )
            # transition and audit in the same transaction
            case = await svc.apply_command(
                tenant_id=tid,
                case_id=case.id,
                command="transition",
                expected_version=1,
                parameters={"target": "triaged"},
            )
            await audit_record(
                session,
                ctx=ctx,
                action="case.transition",
                resource_type="case",
                resource_id=case.id,
                before={"status": "new"},
                after={"status": "triaged"},
            )
            await session.commit()

        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_A}
            )
            from platform_core.audit.models import AuditEvent

            rows = (
                (await session.execute(select(AuditEvent).order_by(AuditEvent.occurred_at)))
                .scalars()
                .all()
            )
            n = len(rows)
            actions = [r.action for r in rows]
            before_hash_present = rows[-1].before_hash is not None
        await engine.dispose()
        assert actions == ["case.create", "case.transition"]
        assert before_hash_present
        return n

    n = _run(scenario())
    assert n == 2


def test_denied_authorization_is_auditable() -> None:
    from platform_core.audit.service import record as audit_record
    from platform_core.db import create_engine
    from platform_core.identity.tenant_context import TenantContext

    async def scenario() -> bool:
        engine = create_engine(APP_URL)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        tid = uuid.UUID(TENANT_A)
        ctx = TenantContext(
            tenant_id=tid, actor_id=uuid.uuid4(), actor_kind="user", role="support_viewer"
        )
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT_A}
            )
            await audit_record(
                session,
                ctx=ctx,
                action="case.close",
                resource_type="case",
                decision="denied",
                reason_code="ROLE_LACKS_ACTION",
            )
            await session.commit()
        await engine.dispose()
        return True

    assert _run(scenario()) is True
