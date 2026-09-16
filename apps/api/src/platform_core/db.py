"""Database engine and session management.

- Tenant-owned tables are protected by PostgreSQL RLS; the application role
  must not have BYPASSRLS.
- Tenant context is set per transaction from server-side resolution only
  (see identity.tenant_context).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from platform_core.config import get_settings


def create_engine(database_url: str | None = None) -> AsyncEngine:
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
