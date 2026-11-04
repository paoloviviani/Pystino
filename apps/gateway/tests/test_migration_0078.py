# ruff: noqa: S608  (fixed SQL built from test constants)
"""Migration 0078: console access (``groups.console_allowed``, ``oidc_config.console_mode``).

Existing groups stay ON, the flag defaults on for a row inserted without it,
the mode column only takes ``on``/``off`` (and the legacy spellings), and the
downgrade removes both. The PostgreSQL leg runs when ``TEST_DATABASE_URL`` is set.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path

import pytest
from test_migration_0067 import _alembic

T0 = "2026-11-04 12:00:00.000000"


def _group_sql(name: str) -> str:
    return (
        "INSERT INTO groups (id, name, source, is_active, created_at, updated_at, "
        f"code_agents_allowed, api_keys_allowed) VALUES ('{uuid.uuid4().hex}', '{name}', "
        f"'manual', 1, '{T0}', '{T0}', 1, 1)"
    )


def test_existing_groups_keep_console_access_and_new_rows_default_on(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{path}"
    assert _alembic(url, "upgrade", "0077").returncode == 0
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(_group_sql("before"))
        conn.commit()

    up = _alembic(url, "upgrade", "0078")
    assert up.returncode == 0, up.stderr[-2000:]

    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT console_allowed FROM groups").fetchall() == [(1,)]
        conn.execute(_group_sql("after"))
        assert conn.execute(
            "SELECT name, console_allowed FROM groups ORDER BY name"
        ).fetchall() == [("after", 1), ("before", 1)]
        assert "console_mode" in {r[1] for r in conn.execute("PRAGMA table_info(oidc_config)")}


def test_console_mode_check_and_downgrade(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{path}"
    assert _alembic(url, "upgrade", "0078").returncode == 0
    with closing(sqlite3.connect(path)) as conn:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(oidc_config)")]
        assert "console_mode" in columns
        base = (
            "INSERT INTO oidc_config (id, reason, created_at, console_mode) "
            f"VALUES ('{{}}', '', '{T0}', {{}})"
        )
        conn.execute(base.format(uuid.uuid4().hex, "'off'"))
        conn.execute(base.format(uuid.uuid4().hex, "NULL"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(base.format(uuid.uuid4().hex, "'sometimes'"))

    down = _alembic(url, "downgrade", "0077")
    assert down.returncode == 0, down.stderr[-2000:]
    with closing(sqlite3.connect(path)) as conn:
        assert "console_mode" not in {r[1] for r in conn.execute("PRAGMA table_info(oidc_config)")}
        assert "console_allowed" not in {r[1] for r in conn.execute("PRAGMA table_info(groups)")}


_PG = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
def scratch_postgres() -> Iterator[Callable[[], str]]:
    import asyncpg
    from conftest import _parse_admin_url

    parts = _parse_admin_url()
    name = f"t_mig0078_{uuid.uuid4().hex[:8]}"

    async def run(sql: str) -> None:
        conn = await asyncpg.connect(
            host=parts["host"], port=parts["port"], user=parts["user"], password=parts["password"]
        )
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(run(f"CREATE DATABASE {name}"))
    try:
        yield (
            lambda: (
                f"postgresql+asyncpg://{parts['user']}:{parts['password']}"
                f"@{parts['host']}:{parts['port']}/{name}"
            )
        )
    finally:
        asyncio.run(run(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))


@pytest.mark.skipif(not _PG, reason="TEST_DATABASE_URL is not set")
def test_postgres_existing_groups_on_check_and_downgrade(
    scratch_postgres: Callable[[], str],
) -> None:
    import asyncpg
    from conftest import _parse_admin_url

    url = scratch_postgres()
    parts = _parse_admin_url()
    database = url.rsplit("/", 1)[1]
    assert _alembic(url, "upgrade", "0077").returncode == 0

    async def run(statement: str) -> list[asyncpg.Record]:
        conn = await asyncpg.connect(
            host=parts["host"],
            port=parts["port"],
            user=parts["user"],
            password=parts["password"],
            database=database,
        )
        try:
            return await conn.fetch(statement)
        finally:
            await conn.close()

    insert_group = (
        "INSERT INTO groups (id, name, source, is_active, created_at, updated_at) "
        "VALUES ('{}', '{}', 'manual', true, now(), now())"
    )
    asyncio.run(run(insert_group.format(uuid.uuid4(), "before")))
    up = _alembic(url, "upgrade", "0078")
    assert up.returncode == 0, up.stderr[-2000:]
    asyncio.run(run(insert_group.format(uuid.uuid4(), "after")))
    rows = asyncio.run(run("SELECT name, console_allowed FROM groups ORDER BY name"))
    assert [(r["name"], r["console_allowed"]) for r in rows] == [("after", True), ("before", True)]

    with pytest.raises(asyncpg.CheckViolationError):
        asyncio.run(
            run(
                "INSERT INTO oidc_config (id, reason, created_at, console_mode) "
                f"VALUES ('{uuid.uuid4()}', '', now(), 'sometimes')"
            )
        )

    down = _alembic(url, "downgrade", "0077")
    assert down.returncode == 0, down.stderr[-2000:]
    cols = asyncio.run(
        run("SELECT column_name FROM information_schema.columns WHERE table_name = 'groups'")
    )
    assert "console_allowed" not in {r["column_name"] for r in cols}
