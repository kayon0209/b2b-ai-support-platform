"""How a saturated connection pool is *reported* - no database required.

What lives here and what does not
---------------------------------
These three tests read configuration and decide which exception class a handler
is registered for. None of them opens a connection. The two that saturate a real
pool need a server, and they are in
`apps/api/tests/integration/test_database_pool_exhaustion.py`.

All five were in this file until CI's unit job failed one with `Connection
refused` and took the job red at 1 failed / 1415 passed. Locally everything had
always passed, because a long-running Compose stack keeps a Postgres around to
borrow - and that is precisely why the mistake survived being written, reviewed
and run many times. The unit job has no such luck, and it is the only
environment that decides where a test belongs.

The gap these cover
-------------------
`create_engine` originally set `pool_size` and `max_overflow` and never
`pool_timeout`, so SQLAlchemy's 30-second default applied and an exhausted pool
held every queued request for half a minute after the callers had gone.

The fix is the timeout, plus a `DATABASE_SATURATED` code at 503 that tells a
client and a load balancer the difference between a busy service and a bug.
These tests are about the reporting half of that, and they need no server.
"""

from __future__ import annotations


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
    problem that retrying cannot fix, and buries a genuine outage in a page of
    503s that everyone retries into.
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
    reachable.
    """
    from sqlalchemy.exc import OperationalError

    from platform_core.main import PoolTimeoutError, app

    assert not issubclass(PoolTimeoutError, OperationalError), (
        "if this ever becomes true, the separate registration below is redundant"
    )

    # Starlette keys the lookup by exception class, so the keys are the types.
    keys = set(app.exception_handlers)  # noqa: SLF001
    assert OperationalError in keys, "OperationalError is no longer handled"
    assert PoolTimeoutError in keys, (
        "a saturated pool raises sqlalchemy.exc.TimeoutError, which is not an "
        "OperationalError; without this registration the 503 path is unreachable"
    )
