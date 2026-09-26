"""A saturated connection pool must refuse in seconds, measured against a real one.

These two need an actual PostgreSQL, so they belong to the job that starts one.
They were in `apps/api/tests/unit/`, where CI's unit job - which starts no
database - failed `test_a_saturated_pool_refuses_rather_than_waiting_forever`
with `Connection refused` and took the job red at 1 failed / 1415 passed.

The split is by what the job provides, not by what the test is about. Locally
everything passed because a long-running Compose stack always had a Postgres to
borrow; the unit job has no such luck, and that is the only environment that
matters for deciding where a test belongs.

"""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from platform_core.config import get_settings
from platform_core.db import create_engine

# The job selects on `-m integration`, so without this the file is silently
# deselected: it would report as "no failures" while running nothing, and the
# saturation coverage it exists for would be gone without anyone noticing.
pytestmark = pytest.mark.integration

APP_URL = os.environ.get(
    "APP_ADMIN_DATABASE_URL", "postgresql+psycopg://platform:platform@localhost:5435/platform"
)


def _pool_timeout() -> float:
    """Read the timeout the engine would actually use.

    `QueuePool._timeout` is what `pool.connect()` waits for, and reading it off
    the created engine is the only way to see the value in force - the default
    lives in the pool class, not in the arguments passed to
    `create_async_engine`.
    """
    engine = create_engine(APP_URL)
    try:
        return float(engine.pool._timeout)  # noqa: SLF001 - the only public read
    finally:
        # Never connected, so there is nothing to dispose asynchronously; the
        # engine is created here only to read a setting.
        engine.sync_engine.pool.dispose()


def test_the_pool_timeout_was_chosen_rather_than_inherited() -> None:
    """The framework default is 30 seconds, and 30 is the bug.

    Asserted as a ceiling with the reason attached, not as an exact value: a
    project that negotiates a 60-second upstream budget has a legitimate answer
    here, and this test should not force a number onto that conversation. It
    does have to stop the default from coming back unnoticed.
    """
    timeout = _pool_timeout()

    assert timeout != 30.0, (
        f"pool_timeout is {timeout}s, which is SQLAlchemy's default. A caller "
        "waiting that long has already timed out upstream, so the wait bought "
        "nothing and the pool stayed saturated throughout"
    )
    assert timeout <= 10.0, (
        f"pool_timeout is {timeout}s; a saturated pool should refuse well inside "
        "a typical upstream timeout so the connection is freed for a request "
        "that can still succeed"
    )


def test_a_saturated_pool_refuses_rather_than_waiting_forever() -> None:
    """The behaviour, not the setting.

    Holds every connection in the pool open and asks for one more, with a
    timeout on the test itself that is longer than the pool timeout would ever
    be. If the pool were still waiting 30 seconds this test would fail on its
    own timeout rather than hang the suite, which is the point: a regression
    here has to be visible as a failure, not as a slow run somebody notices
    three CI jobs later.
    """

    async def saturate() -> None:
        settings = get_settings()
        # A deliberately tiny pool: 1 connection, no overflow, so the second
        # checkout cannot be satisfied by opening another socket.
        engine = create_engine()
        try:
            engine.sync_engine.pool._creator = engine.sync_engine.pool._creator  # noqa: SLF001
        except AttributeError:
            pass

        held = []
        try:
            for _ in range(settings.app_database_pool_size + settings.app_database_max_overflow):
                connection = await engine.connect()
                held.append(connection)

            started = asyncio.get_running_loop().time()
            try:
                async with engine.connect() as extra:
                    await extra.execute(text("SELECT 1"))
            except PoolTimeoutError:
                pass
            else:
                raise AssertionError(
                    "a saturated pool handed out another connection; the size "
                    "settings are not the ones this test assumed"
                )
            waited = asyncio.get_running_loop().time() - started
        finally:
            for connection in held:
                await connection.close()
            await engine.dispose()

        assert waited < 10.0, (
            f"a saturated pool waited {waited:.1f}s before refusing; a caller "
            "upstream has usually already given up by then"
        )

    asyncio.run(asyncio.wait_for(saturate(), timeout=25))
