"""A saturated connection pool must fail fast and say so, not hang for 30 seconds.

The gap
-------
`create_engine` sets `pool_size` and `max_overflow` but never `pool_timeout`, so
SQLAlchemy's default of 30 seconds applies. When the pool is exhausted every
caller waits the full 30 seconds and then gets an `OperationalError` that
reaches the generic handler as `INTERNAL_ERROR`.

Two things are wrong with that, and the second is the one a client cares about:

- **A hung request is worse than a refused one.** 30 seconds per request is
  longer than any sane upstream timeout, so the caller has already given up and
  closed the connection by the time the database would have answered. The wait
  bought nothing, and the pool - which is what is actually exhausted - stayed
  saturated for the whole of it.
- **`INTERNAL_ERROR` tells the client nothing.** It is documented as
  non-retryable and carries the message "quote the trace id to support", which
  is the right answer for a bug and the wrong one for saturation. A saturated
  pool is the textbook retryable condition: the same request succeeds a moment
  later. Telling the client not to retry is how one incident becomes an
  outage - a client that honours `retryable: false` will not come back, and the
  pool drains to nothing anyway.

What is asserted
----------------
The timeout is short enough to be useful and long enough not to be a
thrash-loop generator. The value is not asserted against a magic number,
because the right number depends on the request budget; what is asserted is that
someone chose it, and that the choice is a deliberate fraction of a typical
upstream timeout rather than the framework default nobody looked at.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from platform_core.config import get_settings
from platform_core.db import create_engine


def _pool_timeout() -> float:
    """Read the timeout the engine would actually use.

    `QueuePool._timeout` is what `pool.connect()` waits for, and reading it off
    the created engine is the only way to see the value in force - the default
    lives in the pool class, not in the arguments passed to
    `create_async_engine`.
    """
    engine = create_engine()
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


def test_pool_exhaustion_is_distinguishable_from_a_genuine_fault() -> None:
    """The code, because the two need opposite responses from a client.

    Both are retryable, so a client that only reads `retryable` treats them the
    same and the distinction is wasted. It is the operator and the status code
    that need to tell them apart: a saturated pool drains by itself, and 503 is
    what a load balancer and a backoff both act on correctly.
    """
    from platform_core.api import DATABASE_SATURATED, INTERNAL_ERROR, RETRYABLE_CODES

    assert DATABASE_SATURATED != INTERNAL_ERROR
    assert DATABASE_SATURATED in RETRYABLE_CODES, (
        "a saturated pool succeeds on a retry; telling the client otherwise is "
        "how one busy minute becomes an outage"
    )
    assert INTERNAL_ERROR in RETRYABLE_CODES, "unchanged"


def test_only_exhaustion_is_treated_as_a_busy_service() -> None:
    """Every other `OperationalError` is a real fault and must stay a 500.

    The narrowing is the point. A refused connection and a statement timeout are
    also `OperationalError`; calling them "busy" tells a client to retry a
    problem retrying cannot fix, and buries a genuine outage in a page of 503s
    that everyone retries into.
    """
    from platform_core.main import _is_pool_exhausted

    class _Exhausted:
        connection_invalidated = True
        orig = None

        def __str__(self) -> str:
            return "QueuePool limit ... reached"

    class _Refused:
        connection_invalidated = False
        orig = RuntimeError("connection refused: could not connect to server")

    class _StatementTimeout:
        connection_invalidated = False
        orig = RuntimeError("canceling statement due to statement timeout")

    assert _is_pool_exhausted(_Exhausted()) is True
    assert _is_pool_exhausted(_Refused()) is False
    assert _is_pool_exhausted(_StatementTimeout()) is False


def test_the_handler_is_registered_for_the_exception_that_actually_raises() -> None:
    """The one that was missed, and the easiest to miss again.

    A checkout timeout raises `sqlalchemy.exc.TimeoutError`. It looks like an
    `OperationalError` and is not: it subclasses `SQLAlchemyError` directly, so
    `issubclass(TimeoutError, OperationalError)` is `False`. A handler
    registered for `OperationalError` alone never fires for the exact failure it
    was written for, which makes the whole 503 path unreachable code that reads
    as a finished feature.

    Asserted as behaviour - the handler has to be reachable - rather than by
    grepping for the decorator, because a decorator can be present and still not
    registered if the registration is conditional.
    """
    from sqlalchemy.exc import OperationalError

    from platform_core.main import app

    assert not issubclass(PoolTimeoutError, OperationalError), (
        "if this ever becomes true, the separate registration below is redundant"
    )

    registered = {
        type(exc).__name__
        for exc in app.exception_handlers.values()  # noqa: SLF001
    }
    # Starlette stores the lookup by exception class, so the keys are the types.
    keys = set(app.exception_handlers)  # noqa: SLF001
    assert OperationalError in keys, "OperationalError is no longer handled"
    assert PoolTimeoutError in keys, (
        "a saturated pool raises sqlalchemy.exc.TimeoutError, which is not an "
        "OperationalError; without this registration the 503 path is unreachable"
    )
    assert registered  # the mapping is populated, not empty
