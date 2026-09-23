"""Feature list 7.10: a score is recorded once, read back, and honest.

Against real Postgres because the invariants are the database's: the unique
constraint is what makes a re-tap safe, and the CHECK constraint is what keeps
every row on one scale. A fake session would assert neither.

The two behaviours worth guarding:

- **A second answer replaces the first.** People re-tap survey links. Inserting
  a second row (or rejecting the tap) both produce a wrong number - one
  double-counts the conversation, the other loses a real response.
- **An unasked-about window reports "unknown", not 0.** A 0% response rate and
  "nobody was surveyed" look identical on a dashboard and mean opposite things.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from platform_core.support_bridge.csat import (
    CsatError,
    csat_summary,
    record_response,
    survey_text,
)

pytestmark = pytest.mark.integration

ADMIN_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL",
    "postgresql+psycopg://platform:platform@localhost:5435/platform",
)
APP_URL = "postgresql+psycopg://platform_app:platform_app@localhost:5435/platform"

TENANT = "01900000-0000-7000-8000-0000000000da"
SLUG = "agent-csat"


def _run(coro):
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def _seed() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, status) VALUES "
                "(:id, :slug, 'CSAT', 'active') ON CONFLICT (slug) DO NOTHING"
            ),
            {"id": TENANT, "slug": SLUG},
        )
    admin.dispose()


def _clear() -> None:
    admin = create_engine(ADMIN_URL)
    with admin.begin() as conn:
        conn.execute(text("DELETE FROM csat_responses WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM agent_runs WHERE tenant_id = :t"), {"t": TENANT})
        conn.execute(text("DELETE FROM tenants WHERE slug = :slug"), {"slug": SLUG})
    admin.dispose()


async def _session():
    from sqlalchemy import text as sa_text

    from platform_core.db import create_engine as async_engine

    engine = async_engine(APP_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    session = factory()
    await session.execute(sa_text("SELECT set_config('app.tenant_id', :t, true)"), {"t": TENANT})
    return session


def _record(**kwargs):
    async def _inner():
        session = await _session()
        try:
            row = await record_response(session, tenant_id=uuid.UUID(TENANT), **kwargs)
            await session.commit()
            return row
        finally:
            await session.close()

    return _run(_inner())


@pytest.fixture(autouse=True)
def tenant() -> None:
    _clear()
    _seed()
    yield
    _clear()


def _conversation() -> uuid.UUID:
    return uuid.uuid4()


def test_a_score_is_recorded_and_read_back() -> None:
    conv = _conversation()
    row = _record(conversation_ref_id=conv, score=5, channel="web_chat")
    assert row.score == 5
    assert row.conversation_ref_id == conv


def test_a_second_answer_replaces_the_first_without_a_second_row() -> None:
    """A re-tap must not double-weight the conversation in every average."""
    conv = _conversation()
    _record(conversation_ref_id=conv, score=1)
    _record(conversation_ref_id=conv, score=5)
    summary = _run(_summary())
    assert summary.responses == 1
    assert summary.average == 5.0


def test_an_out_of_range_score_is_refused() -> None:
    for bad in (0, 6, -1, 99):
        with pytest.raises(CsatError):
            _record(conversation_ref_id=_conversation(), score=bad)


def test_a_non_integer_score_is_refused() -> None:
    with pytest.raises(CsatError):
        _record(conversation_ref_id=_conversation(), score="five")  # type: ignore[arg-type]


def test_the_average_reflects_every_response() -> None:
    for score in (5, 4, 3):
        _record(conversation_ref_id=_conversation(), score=score)
    summary = _run(_summary())
    assert summary.responses == 3
    assert summary.average == 4.0


def test_the_distribution_is_reported_with_the_average() -> None:
    """3.0 from everyone and 3.0 from half fives and half ones differ."""
    for score in (5, 1, 5, 1):
        _record(conversation_ref_id=_conversation(), score=score)
    summary = _run(_summary())
    assert summary.average == 3.0
    assert summary.distribution[5] == 2
    assert summary.distribution[1] == 2


def test_an_empty_window_reports_unknown_not_zero() -> None:
    summary = _run(_summary())
    assert summary.responses == 0
    assert summary.average is None


async def _summary():
    session = await _session()
    try:
        return await csat_summary(session, tenant_id=uuid.UUID(TENANT))
    finally:
        await session.close()


def test_the_database_also_refuses_an_out_of_range_score() -> None:
    """Defence in depth: the constraint holds even for a path nobody wrote.

    Inserted directly rather than through `record_response`, because the Python
    validation would reject 7 first and the database check would never run -
    a test that passes for the wrong reason is worse than no test.
    """
    admin = create_engine(ADMIN_URL)
    try:
        with pytest.raises(IntegrityError):
            with admin.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO csat_responses (id, tenant_id, conversation_ref_id, "
                        "score, created_at, updated_at) VALUES (:id, :t, :conv, 7, 0, 0)"
                    ),
                    {"id": str(uuid.uuid4()), "t": TENANT, "conv": str(_conversation())},
                )
    finally:
        admin.dispose()


def test_survey_text_is_adapted_to_the_channel() -> None:
    sms = survey_text("sms")
    assert "1-5" in sms
    assert "*" not in sms


def test_survey_text_survives_every_known_channel() -> None:
    for channel in ("sms", "wechat", "web_chat", "email"):
        assert "1-5" in survey_text(channel), channel
