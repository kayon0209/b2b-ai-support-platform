"""Feature list 7.7: escalation to a named team, safely.

Against real Postgres because both guards are about rows: the department must
exist, and re-escalating must not write.

The two behaviours that matter:

- **A destination that does not exist is refused, not written.** Routing a case
  to a slug with no `Department` row looks like a successful escalation and
  puts the case in a queue nobody reads. Asserted by checking the case is
  untouched afterwards.
- **Re-escalating to the same team is a no-op.** Escalating twice is normal;
  each write bumps `version`, so a second identical escalation would move the
  row out from under any caller holding the previous version.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from platform_core.cases.transfer import (
    TransferError,
    TransferTarget,
    target_from_text,
    transfer_case,
)

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"

TENANT = "01900000-0000-7000-8000-0000000000db"
SLUG = "agent-transfer"
ENGINEERING_DEPT = "01900000-0000-7000-8000-0000000000e1"


def _run(coro):
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def _seed() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, :slug, 'Transfer', 'active') ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT, "slug": SLUG},
        )
        # Only `engineering` exists on purpose: `quality` must be refused.
        conn.execute(
            text(
                "INSERT INTO departments (id, tenant_id, name, slug, created_at) "
                "VALUES (:id, :t, 'Engineering', 'engineering', 0) "
                "ON CONFLICT (tenant_id, slug) DO NOTHING"
            ),
            {"id": ENGINEERING_DEPT, "t": TENANT},
        )
    admin.dispose()


def _clear() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(text("DELETE FROM cases WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM departments WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM tenants WHERE slug = :slug"), {"slug": SLUG})
    admin.dispose()


def _make_case(*, team_ref: str | None = None) -> uuid.UUID:
    case_id = uuid.uuid4()
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO cases (id, tenant_id, subject, description, category, "
                "priority, status, team_ref, version, opened_at, last_state_changed_at) "
                "VALUES (:id, :t, 'sub', '', 'general', 'p2', 'new', :team, 1, :now, :now)"
            ),
            {"id": str(case_id), "t": TENANT, "team": team_ref, "now": int(time.time())},
        )
    admin.dispose()
    return case_id


def _team_of(case_id: uuid.UUID) -> str | None:
    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as conn:
            return conn.execute(
                text("SELECT team_ref FROM cases WHERE id = :id"), {"id": str(case_id)}
            ).scalar_one()
    finally:
        admin.dispose()


def _version_of(case_id: uuid.UUID) -> int:
    admin = create_engine(ADMIN_URL)
    try:
        with admin.begin() as conn:
            return conn.execute(
                text("SELECT version FROM cases WHERE id = :id"), {"id": str(case_id)}
            ).scalar_one()
    finally:
        admin.dispose()


async def _transfer(case_id, target: TransferTarget, **kwargs):
    from sqlalchemy import text as sa_text

    from platform_core.db import create_engine as async_engine

    engine = async_engine(APP_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await session.execute(
            sa_text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT}
        )
        result = await transfer_case(
            session, tenant_id=uuid.UUID(TENANT), case_id=case_id, target=target, **kwargs
        )
        await session.commit()
        return result


@pytest.fixture(autouse=True)
def tenant() -> None:
    _clear()
    _seed()
    yield
    _clear()


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("转工程", TransferTarget.ENGINEERING),
        ("帮我转品质部", TransferTarget.QUALITY),
        ("这个问题要转工厂", TransferTarget.FACTORY),
        ("escalate to engineering", TransferTarget.ENGINEERING),
        ("给财务处理", TransferTarget.FINANCE),
    ],
)
def test_a_business_destination_is_recognised(phrase: str, expected: TransferTarget) -> None:
    assert target_from_text(phrase) is expected


def test_a_phrase_with_no_destination_yields_none() -> None:
    """Guessing sends work to a team that was never asked to take it."""
    assert target_from_text("升级处理") is None
    assert target_from_text("") is None
    assert target_from_text(None) is None


def test_a_case_moves_to_an_existing_team() -> None:
    case_id = _make_case()
    row, changed = _run(_transfer(case_id, TransferTarget.ENGINEERING))
    assert changed is True
    assert row.team_ref == "engineering"
    assert _team_of(case_id) == "engineering"


def test_an_unknown_team_is_refused_and_nothing_is_written() -> None:
    """The guard: a slug with no department would be a queue nobody reads."""
    case_id = _make_case()
    with pytest.raises(TransferError) as excinfo:
        _run(_transfer(case_id, TransferTarget.QUALITY))
    assert excinfo.value.code == "TRANSFER_TEAM_UNKNOWN"
    assert _team_of(case_id) is None


def test_re_escalating_to_the_same_team_is_a_no_op() -> None:
    """Escalating twice is normal; the version must not move for nothing."""
    case_id = _make_case(team_ref="engineering")
    before = _version_of(case_id)
    _row, changed = _run(_transfer(case_id, TransferTarget.ENGINEERING))
    assert changed is False
    assert _version_of(case_id) == before


def test_a_missing_case_is_reported_not_created() -> None:
    with pytest.raises(TransferError) as excinfo:
        _run(_transfer(uuid.uuid4(), TransferTarget.ENGINEERING))
    assert excinfo.value.code == "TRANSFER_CASE_NOT_FOUND"
