"""Integration tests: PostgreSQL RLS tenant isolation.

Requires a running database with migrations applied:
  docker compose -f infra/compose/docker-compose.yml up -d ai-postgres
  alembic -c apps/api/migrations/alembic.ini upgrade head

Run: pytest apps/api/tests/integration -m integration

These use the real non-bypass role (platform_app) exactly as production
docs/testing-and-evaluation.md requires.
"""

import os
import uuid

import pytest
import sqlalchemy.exc
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration

DB_URL = os.environ.get(
    "APP_TEST_DATABASE_URL",
    "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform",
)
ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)

TENANT_A = "01900000-0000-7000-8000-000000000001"
TENANT_B = "01900000-0000-7000-8000-000000000002"


@pytest.fixture(scope="module", autouse=True)
def seeded_database() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, 'rls-test-a', 'Tenant A', 'active'), "
                "(:idb, 'rls-test-b', 'Tenant B', 'active') "
                "ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT_A, "idb": TENANT_B},
        )
    yield
    with admin.begin() as conn:
        conn.execute(text("DELETE FROM tenants WHERE slug IN ('rls-test-a', 'rls-test-b')"))
    admin.dispose()


def _app_engine() -> object:
    return create_engine(DB_URL)


@pytest.mark.zero_tolerance("cross_tenant_violations")
def test_tenant_sees_only_own_rows() -> None:
    uid = uuid.uuid4()
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, primary_email, display_name) "
                "VALUES (:id, :email, 'RLS Test User') ON CONFLICT (primary_email) DO NOTHING"
            ),
            {"id": uid, "email": f"rls-{uid}@test.local"},
        )
        conn.execute(
            text(
                "INSERT INTO memberships (id, tenant_id, user_id, role, status) "
                "VALUES (:id, :tid, :uid, 'support_agent', 'active')"
            ),
            {"id": uuid.uuid4(), "tid": TENANT_A, "uid": uid},
        )

    engine = _app_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": TENANT_A})
        rows = conn.execute(text("SELECT count(*) FROM memberships")).scalar()
        # Same transaction, other tenant context: must see zero rows.
        conn.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": TENANT_B})
        other_rows = conn.execute(text("SELECT count(*) FROM memberships")).scalar()
    engine.dispose()
    assert rows >= 1  # own rows visible
    assert other_rows == 0  # cross-tenant rows invisible

    with admin.begin() as conn:
        conn.execute(text("DELETE FROM memberships WHERE user_id = :id"), {"id": uid})
        conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": uid})


@pytest.mark.zero_tolerance("cross_tenant_violations")
def test_no_context_fails_closed() -> None:
    engine = _app_engine()
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT count(*) FROM memberships")).scalar()
    engine.dispose()
    assert rows == 0


@pytest.mark.zero_tolerance("cross_tenant_violations")
def test_cross_tenant_insert_blocked() -> None:
    engine = _app_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": TENANT_B})
        with pytest.raises(Exception, match="row-level security"):
            conn.execute(
                text(
                    "INSERT INTO memberships (id, tenant_id, user_id, role, status) "
                    "VALUES (:id, :tid, :uid, 'support_agent', 'active')"
                ),
                {"id": uuid.uuid4(), "tid": TENANT_A, "uid": uuid.uuid4()},
            )
    engine.dispose()


@pytest.mark.zero_tolerance("cross_tenant_violations")
def test_audit_events_append_only() -> None:
    engine = _app_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": TENANT_A})
        with pytest.raises(sqlalchemy.exc.ProgrammingError, match="permission denied"):
            conn.execute(text("UPDATE audit_events SET decision = 'tampered'"))
    engine.dispose()
