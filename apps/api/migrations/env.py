"""Alembic environment.

Runs synchronously with psycopg3: migrations are DDL work, async adds no
value and ProactorEventLoop on Windows breaks psycopg async mode.
"""

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import Connection

# Source roots, added before importing platform_core.
#
# `prepend_sys_path = src` in alembic.ini covers apps/api/src only, but
# platform_core imports platform_policy and observability, so a checkout
# without an editable install fails with ModuleNotFoundError. Doing this in
# Python rather than the .ini keeps it platform-agnostic: the ini format has
# one separator for the whole value, and Windows uses ';' where POSIX uses ':'.
_HERE = Path(__file__).resolve().parent  # apps/api/migrations
_REPO = _HERE.parent.parent.parent  # repository root
for _root in (
    _HERE.parent / "src",  # apps/api/src
    _REPO / "packages" / "policy" / "src",
    _REPO / "packages" / "contracts" / "src",
    _REPO / "packages" / "observability" / "src",
):
    _path = str(_root)
    if _root.is_dir() and _path not in sys.path:
        sys.path.insert(0, _path)

from platform_core import orm_base  # noqa: E402
from platform_core.config import get_settings  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = orm_base.Base.metadata


def _database_url() -> str:
    url = get_settings().database_url
    # Sync psycopg driver for migrations.
    return url.replace("postgresql+psycopg://", "postgresql+psycopg://")


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_database_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        _run_migrations(connection)
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
