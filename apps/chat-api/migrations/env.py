"""Alembic environment, wired for an async driver.

The same shape as the gateway's, and for the same reason: Alembic's generated
``env.py`` builds a synchronous engine and cannot use ``asyncpg``, so the
connection is async and the migration logic is run through ``run_sync``.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from chat_api.config import Settings
from chat_api.models import Base
from chat_api.types import TZDateTime
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def render_item(type_: str, obj: object, autogen_context: object) -> str | bool:
    """Render our TypeDecorators as plain SQL types.

    Without this, autogenerate emits ``chat_api.types.TZDateTime(...)`` into the
    migration, which fails at runtime with ``NameError: name 'chat_api' is not
    defined`` — and, worse, would make every historical migration depend on
    application code that is free to change. A migration should describe SQL,
    not import the app.

    The gateway's env.py carries this for the same reason. Dropping it while
    copying that file is what produced the first migration here, and the
    NameError only appeared when the container ran it.
    """
    if type_ == "type" and isinstance(obj, TZDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def _database_url() -> str:
    if override := context.get_x_argument(as_dictionary=True).get("url"):
        return str(override)
    return Settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        render_item=render_item,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
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


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(_run_async_migrations())
