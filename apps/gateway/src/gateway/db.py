"""Async engine and session management."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import ConnectionPoolEntry, StaticPool

from gateway.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the async engine for the configured database."""
    kwargs: dict[str, Any] = {
        "echo": settings.database_echo,
        "pool_pre_ping": True,
    }

    if settings.database_url.startswith("sqlite"):
        # Tests only. SQLite has no pool sizing and needs FK enforcement turned
        # on explicitly, or ON DELETE clauses are silently ignored.
        if ":memory:" in settings.database_url:
            # Each new connection to ":memory:" gets its *own* empty database, so
            # without a single pooled connection the schema created by one session
            # is invisible to the next.
            kwargs["poolclass"] = StaticPool
            kwargs["connect_args"] = {"check_same_thread": False}
        engine = create_async_engine(settings.database_url, **kwargs)

        @event.listens_for(engine.sync_engine, "connect")
        def _enable_sqlite_fks(
            dbapi_connection: DBAPIConnection, _record: ConnectionPoolEntry
        ) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine

    return create_async_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        **kwargs,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        engine,
        expire_on_commit=False,  # objects stay usable after commit inside a request
        autoflush=False,
    )


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Short transaction helper.

    Used by the streaming path, which must never hold one transaction open for
    the whole life of a stream: a generation lasting minutes would pin a
    connection and block autovacuum on the usage table.
    """
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
