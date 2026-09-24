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
    settings = get_settings()
    url = database_url or settings.database_url
    is_app_role = settings.app_database_url is not None and url == settings.app_database_url
    pool_size = settings.app_database_pool_size if is_app_role else settings.database_pool_size
    max_overflow = (
        settings.app_database_max_overflow if is_app_role else settings.database_max_overflow
    )
    return create_async_engine(
        url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
    )


# Engines are cached per URL and reused across requests: a pool that is built
# and disposed per session scope pays a fresh TCP connection + Postgres
# authentication on every request, and `pool_size` would never hold a
# connection. Keyed by URL so the owner role and the app role each get their
# own pool, and so a test pointing at a different database never shares one.
_engines: dict[str, AsyncEngine] = {}


def get_engine(database_url: str | None = None) -> AsyncEngine:
    """Return the cached engine for a URL, creating it on first use."""
    url = database_url or get_settings().database_url
    engine = _engines.get(url)
    if engine is None:
        engine = create_engine(url)
        _engines[url] = engine
    return engine


_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
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
    The bootstrap owner is a superuser and would bypass RLS entirely.

    The engine comes from the per-URL cache: creating and disposing one per
    call turned every request into a fresh connection + teardown."""
    factory = async_sessionmaker(get_engine(database_url), expire_on_commit=False)
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    global _session_factory
    for engine in _engines.values():
        await engine.dispose()
    _engines.clear()
    _session_factory = None


def app_role_url() -> str:
    """The non-bypass application role's URL.

    The bootstrap owner is a superuser with `rolbypassrls`, so any code path
    that connects with it silently stops enforcing row-level security. Every
    request and worker path uses this; only migrations and test cleanup use the
    owner. It lives here rather than in each caller because a second copy is how
    one of them ends up pointing at the owner.
    """
    settings = get_settings()
    if settings.app_database_url:
        return settings.app_database_url
    return settings.database_url.replace("platform:platform@", "platform_app:platform_app@")
