"""Alembic environment, wired for an async driver.

Alembic's generated ``env.py`` calls ``engine_from_config()``, which builds a
*synchronous* engine and therefore cannot use ``asyncpg``. The fix is to create an
async engine and run the migration through ``connection.run_sync``: Alembic's
migration logic stays synchronous, and only the connection is async.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from gateway.config import get_settings
from gateway.models import Base
from gateway.types import TZDateTime
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def render_item(type_: str, obj: object, autogen_context: object) -> str | bool:
    """Render our TypeDecorators as plain SQL types.

    Without this, autogenerate emits ``gateway.types.TZDateTime(...)`` into the
    migration, which then fails with a NameError — and, worse, would make every
    historical migration depend on application code that is free to change. A
    migration should describe SQL, not import the app.
    """
    if type_ == "type" and isinstance(obj, TZDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def _database_url() -> str:
    """Prefer -x url=..., then the application's own configuration."""
    if override := context.get_x_argument(as_dictionary=True).get("url"):
        return str(override)
    return get_settings().database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting.

    Useful when a DBA has to review or apply the change by hand.
    """
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        render_item=render_item,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        # Needed for SQLite, harmless elsewhere: ALTER is emulated by rebuilding
        # the table, and named constraints are required to do that.
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_item=render_item,
        compare_type=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    engine = create_async_engine(_database_url(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_do_run_migrations)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
