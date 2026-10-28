"""Migration 0074: ``quota_resets.user_id``.

Additive: every existing reset stays a whole-rule reset (null user). The
downgrade deletes the per-user rows before dropping the column, because left
behind they would read as whole-rule resets and zero everyone. The foreign key
cascades, so an erased user takes their per-user resets with them. The
PostgreSQL leg runs when ``TEST_DATABASE_URL`` is set, in a scratch database of
its own.
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

T0 = "2026-10-28 12:00:00.000000"


def _insert(conn: sqlite3.Connection, table: str, **given: object) -> str:
    """A row with every NOT NULL column filled, whatever the schema at this
    revision demands, so the test does not break when a column is added."""
    values: dict[str, str] = {k: repr(v) for k, v in given.items()}
    ident = uuid.uuid4().hex
    for _cid, name, ctype, notnull, default, _pk in conn.execute(f"PRAGMA table_info({table})"):
        if name in values or not notnull or default is not None:
            continue
        kind = ctype.upper()
        if name == "id":
            values[name] = f"'{ident}'"
        elif "DATETIME" in kind:
            values[name] = f"'{T0}'"
        elif "INT" in kind or "BOOL" in kind or "NUMERIC" in kind:
            values[name] = "1"
        else:
            values[name] = f"'{ident[:12]}'"
    if "id" not in values:
        values["id"] = f"'{ident}'"
    conn.execute(
        f"INSERT INTO {table} ({', '.join(values)}) "  # noqa: S608
        f"VALUES ({', '.join(values.values())})"
    )
    return ident


def test_up_and_down_on_sqlite(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{path}"
    assert _alembic(url, "upgrade", "0073").returncode == 0
    with closing(sqlite3.connect(path)) as conn:
        rule = _insert(
            conn,
            "limit_rules",
            scope="global",
            metric="requests",
            applies_to="each_user",
            window_seconds=3600,
            limit_value="5",
        )
        _insert(conn, "quota_resets", rule_id=rule, effective_at=T0)
        conn.commit()

    up = _alembic(url, "upgrade", "0074")
    assert up.returncode == 0, up.stderr[-2000:]
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        # History stands as a whole-rule reset.
        assert conn.execute("SELECT user_id FROM quota_resets").fetchall() == [(None,)]
        user = _insert(conn, "users", issuer="https://idp.test", subject="s1")
        _insert(conn, "quota_resets", rule_id=rule, effective_at=T0, user_id=user)
        conn.commit()
        assert conn.execute("SELECT count(*) FROM quota_resets").fetchone() == (2,)
        indexes = [row[1] for row in conn.execute("PRAGMA index_list(quota_resets)")]
        assert "ix_quota_resets_rule_user" in indexes
        # Erasing the user deletes their reset and leaves the whole-rule one.
        conn.execute("DELETE FROM users WHERE id = ?", (user,))
        conn.commit()
        assert conn.execute("SELECT user_id FROM quota_resets").fetchall() == [(None,)]
        user = _insert(conn, "users", issuer="https://idp.test", subject="s2")
        _insert(conn, "quota_resets", rule_id=rule, effective_at=T0, user_id=user)
        conn.commit()

    down = _alembic(url, "downgrade", "0073")
    assert down.returncode == 0, down.stderr[-2000:]
    with closing(sqlite3.connect(path)) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(quota_resets)")]
        assert "user_id" not in columns
        # The per-user row went first: it would otherwise now reset everyone.
        assert conn.execute("SELECT count(*) FROM quota_resets").fetchone() == (1,)
    assert _alembic(url, "upgrade", "head").returncode == 0


_PG = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture()
def scratch_postgres() -> Iterator[Callable[[], str]]:
    import asyncpg
    from conftest import _parse_admin_url

    parts = _parse_admin_url()
    name = f"t_mig0074_{uuid.uuid4().hex[:8]}"

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
    assert _alembic(url, "upgrade", "0073").returncode == 0

    async def query(sql: str) -> list[tuple[object, ...]]:
        conn = await asyncpg.connect(
            host=parts["host"],
            port=parts["port"],
            user=parts["user"],
            password=parts["password"],
            database=url.rsplit("/", 1)[1],
        )
        try:
            return [tuple(r) for r in await conn.fetch(sql)]
        finally:
            await conn.close()

    def insert(table: str, **given: str) -> str:
        """A row with every NOT NULL column filled from the live schema."""
        needed = asyncio.run(
            query(
                "SELECT column_name, data_type FROM information_schema.columns "  # noqa: S608
                f"WHERE table_name = '{table}' AND is_nullable = 'NO' AND column_default IS NULL"
            )
        )
        ident = str(uuid.uuid4())
        fill = dict(given)
        fill.setdefault("id", f"'{ident}'")
        for name, kind in needed:
            if name in fill:
                continue
            if "timestamp" in str(kind):
                fill[name] = "now()"
            elif kind in ("integer", "bigint", "numeric", "smallint"):
                fill[name] = "1"
            elif kind == "boolean":
                fill[name] = "false"
            else:
                fill[name] = f"'{ident[:12]}'"
        asyncio.run(
            query(
                f"INSERT INTO {table} ({', '.join(fill)}) "  # noqa: S608
                f"VALUES ({', '.join(fill.values())})"
            )
        )
        return fill["id"]

    rule = insert(
        "limit_rules",
        scope="'global'",
        metric="'requests'",
        applies_to="'each_user'",
        window_seconds="3600",
        limit_value="5",
    )
    insert("quota_resets", rule_id=rule)

    up = _alembic(url, "upgrade", "0074")
    assert up.returncode == 0, up.stderr[-2000:]
    assert asyncio.run(query("SELECT user_id FROM quota_resets")) == [(None,)]
    user = insert("users", issuer="'https://idp.test'", subject="'s1'")
    insert("quota_resets", rule_id=rule, user_id=user)
    assert asyncio.run(query("SELECT count(*) FROM quota_resets")) == [(2,)]
    indexes = asyncio.run(
        query("SELECT indexname FROM pg_indexes WHERE tablename = 'quota_resets'")
    )
    assert ("ix_quota_resets_rule_user",) in indexes
    # The foreign key cascades: erasing the user deletes their reset only.
    asyncio.run(query(f"DELETE FROM users WHERE id = {user}"))  # noqa: S608
    assert asyncio.run(query("SELECT user_id FROM quota_resets")) == [(None,)]
    user = insert("users", issuer="'https://idp.test'", subject="'s2'")
    insert("quota_resets", rule_id=rule, user_id=user)

    down = _alembic(url, "downgrade", "0073")
    assert down.returncode == 0, down.stderr[-2000:]
    columns = asyncio.run(
        query(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'quota_resets'"
        )
    )
    assert ("user_id",) not in columns
    assert asyncio.run(query("SELECT count(*) FROM quota_resets")) == [(1,)]
    assert _alembic(url, "upgrade", "head").returncode == 0
