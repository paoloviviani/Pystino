"""Migration 0068: a user rule may override the global each-user ceiling.

Adds ``limit_rules.overrides_each_user``, a check that only a user rule may set
it, and rebuilds the uniqueness index with the flag in it. The index is the
point of the SQLite leg: batch mode drops an expression index with the old
table (the 0061/0067 trap), so a duplicate rule inserted after the upgrade is
what proves it came back. The PostgreSQL leg runs when ``TEST_DATABASE_URL`` is
set, in a scratch database of its own.
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

T0 = "2026-10-21 12:00:00.000000"


def _insert_sqlite(
    conn: sqlite3.Connection, scope: str, scope_id: str | None, *, override: int | None = None
) -> None:
    columns = (
        "id, name, scope, scope_id, metric, window_seconds, period, limit_value, is_active, "
        "created_at, updated_at, applies_to"
    )
    values = (
        f"'{uuid.uuid4().hex}', 'r', '{scope}', "
        f"{'NULL' if scope_id is None else repr(scope_id)}, 'cost', NULL, 'month', 5, 1, "
        f"'{T0}', '{T0}', 'aggregate'"
    )
    if override is not None:
        columns += ", overrides_each_user"
        values += f", {override}"
    conn.execute(f"INSERT INTO limit_rules ({columns}) VALUES ({values})")  # noqa: S608


def test_up_and_down_on_sqlite(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{path}"
    assert _alembic(url, "upgrade", "0067").returncode == 0
    user = uuid.uuid4().hex
    with closing(sqlite3.connect(path)) as conn:
        _insert_sqlite(conn, "user", user)
        conn.commit()

    up = _alembic(url, "upgrade", "0068")
    assert up.returncode == 0, up.stderr[-2000:]
    with closing(sqlite3.connect(path)) as conn:
        # The existing rule survived, as a plain rule.
        assert conn.execute("SELECT overrides_each_user FROM limit_rules").fetchall() == [(0,)]
        # An override sits beside the ordinary rule for the same person...
        _insert_sqlite(conn, "user", user, override=1)
        conn.commit()
        # ...but only one of each, so the index was recreated, flag included.
        for duplicate in (0, 1):
            with pytest.raises(sqlite3.IntegrityError):
                _insert_sqlite(conn, "user", user, override=duplicate)
        # The flag is refused off the user scope.
        for scope, scope_id in (("global", None), ("group", uuid.uuid4().hex)):
            with pytest.raises(sqlite3.IntegrityError):
                _insert_sqlite(conn, scope, scope_id, override=1)

    refused = _alembic(url, "downgrade", "0067")
    assert refused.returncode != 0
    assert "override the each-user ceiling" in refused.stderr
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("DELETE FROM limit_rules WHERE overrides_each_user")
        conn.commit()
    down = _alembic(url, "downgrade", "0067")
    assert down.returncode == 0, down.stderr[-2000:]
    with closing(sqlite3.connect(path)) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(limit_rules)")]
        assert "overrides_each_user" not in columns
        # The old index is back: one rule per scope, metric and window.
        with pytest.raises(sqlite3.IntegrityError):
            _insert_sqlite(conn, "user", user)
    assert _alembic(url, "upgrade", "head").returncode == 0


_PG = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture()
def scratch_postgres() -> Iterator[Callable[[], str]]:
    import asyncpg
    from conftest import _parse_admin_url

    parts = _parse_admin_url()
    name = f"t_mig0068_{uuid.uuid4().hex[:8]}"

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
def test_up_and_down_on_postgres(scratch_postgres: Callable[[], str]) -> None:
    import asyncpg
    from conftest import _parse_admin_url

    url = scratch_postgres()
    parts = _parse_admin_url()
    assert _alembic(url, "upgrade", "0067").returncode == 0

    async def exec_sql(statements: list[str]) -> list[str]:
        conn = await asyncpg.connect(
            host=parts["host"],
            port=parts["port"],
            user=parts["user"],
            password=parts["password"],
            database=url.rsplit("/", 1)[1],
        )
        outcomes: list[str] = []
        try:
            for statement in statements:
                try:
                    await conn.execute(statement)
                    outcomes.append("ok")
                except asyncpg.IntegrityConstraintViolationError:
                    outcomes.append("refused")
        finally:
            await conn.close()
        return outcomes

    def insert(scope: str, scope_id: str | None, override: bool | None = None) -> str:
        columns = (
            "id, name, scope, scope_id, metric, window_seconds, period, limit_value, "
            "is_active, created_at, updated_at, applies_to"
        )
        values = (
            f"'{uuid.uuid4()}', 'r', '{scope}', "
            f"{'NULL' if scope_id is None else repr(scope_id)}, 'cost', NULL, 'month', 5, "
            "true, now(), now(), 'aggregate'"
        )
        if override is not None:
            columns += ", overrides_each_user"
            values += f", {'true' if override else 'false'}"
        return f"INSERT INTO limit_rules ({columns}) VALUES ({values})"  # noqa: S608

    user = str(uuid.uuid4())
    assert asyncio.run(exec_sql([insert("user", user)])) == ["ok"]
    up = _alembic(url, "upgrade", "0068")
    assert up.returncode == 0, up.stderr[-2000:]
    assert asyncio.run(
        exec_sql(
            [
                insert("user", user, override=True),
                insert("user", user, override=True),
                insert("user", user),
                insert("global", None, override=True),
                insert("group", str(uuid.uuid4()), override=True),
            ]
        )
    ) == ["ok", "refused", "refused", "refused", "refused"]

    assert _alembic(url, "downgrade", "0067").returncode != 0
    asyncio.run(exec_sql(["DELETE FROM limit_rules WHERE overrides_each_user"]))
    down = _alembic(url, "downgrade", "0067")
    assert down.returncode == 0, down.stderr[-2000:]
    assert asyncio.run(exec_sql([insert("user", user)])) == ["refused"]
    assert _alembic(url, "upgrade", "head").returncode == 0
