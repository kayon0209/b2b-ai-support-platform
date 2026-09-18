"""Database engine and session management.

- Tenant-owned tables are protected by PostgreSQL RLS; the application role
  must not have BYPASSRLS.
- Tenant context is set per transaction from server-side resolution only
  (see identity.tenant_context).
- psycopg's async mode cannot run on Windows' default ProactorEventLoop.
  Setting the policy at import time is not enough: `uvicorn` creates its loop
  before importing the app, so an already-running Proactor loop survives.
  `ensure_async_db_loop()` is therefore called before every engine is made,
  which converts a silent 401-on-every-request into either a working loop or
  an explicit, immediately diagnosable error.
"""

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Imported for its side effect: every mapped table is registered, so a
# foreign key pointing at another module's table always resolves. Without it
# the failure is import-order dependent and reads like a broken schema - see
# `models_registry`.
from platform_core import models_registry  # noqa: F401  (side-effect import)
from platform_core.config import get_settings

_UNSUPPORTED_LOOP = "ProactorEventLoop"


def ensure_async_db_loop() -> None:
    """Guarantee the running event loop can drive psycopg async.

    Called once per engine creation. On non-Windows platforms this is a
    no-op. On Windows it raises if a Proactor loop is already running,
    because silently continuing would make every DB-backed request fail at
    runtime with an auth-shaped 401 rather than a clear startup error.
    """
    if sys.platform != "win32":
        return

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        # No loop running yet: the policy above governs the one that will be
        # created.
        return

    if type(running).__name__ == _UNSUPPORTED_LOOP:
        raise RuntimeError(
            "psycopg async cannot run on a ProactorEventLoop. Start the API "
            "with the platform entrypoint (which selects a selector loop), "
            "e.g. `python -m platform_core.main`, instead of the bare "
            "`uvicorn platform_core.main:app` command on Windows."
        )


def create_engine(database_url: str | None = None) -> AsyncEngine:
    ensure_async_db_loop()
    url = database_url or get_settings().database_url
    return create_async_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=10)


engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global engine, _session_factory
    if _session_factory is None:
        engine = create_engine()
        _session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Unit of work: one transaction per scope, rollback on error."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope_with_url(database_url: str) -> AsyncIterator[AsyncSession]:
    """Session scope against an explicit URL (e.g. the non-bypass app role).
    The bootstrap owner is a superuser and would bypass RLS entirely."""
    scoped_engine = create_engine(database_url)
    factory = async_sessionmaker(scoped_engine, expire_on_commit=False)
    try:
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
    finally:
        await scoped_engine.dispose()


async def dispose_engine() -> None:
    global engine, _session_factory
    if engine is not None:
        await engine.dispose()
        engine = None
        _session_factory = None
