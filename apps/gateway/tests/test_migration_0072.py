"""Migration 0072: ``search_prices`` and ``usage_records.search_price_id``.

Additive only, and it must not reprice history: a usage row that exists before
the revision keeps its cost and gets a null price reference, which is what a
report reads as "unpriced". The downgrade drops the table and the column
without losing the rows. The PostgreSQL leg runs when ``TEST_DATABASE_URL`` is
set, in a scratch database of its own.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import pytest
from test_migration_0067 import _alembic

T0 = "2026-10-26 12:00:00.000000"


def _insert_usage_row(conn: sqlite3.Connection, request_id: str) -> None:
    """A usage row with every NOT NULL column filled, whatever the schema at
    this revision demands, so the test does not break when a column is added."""
    values: dict[str, str] = {}
    for _cid, name, ctype, notnull, default, _pk in conn.execute(
        "PRAGMA table_info(usage_records)"
    ):
        if not notnull or default is not None:
            continue
        kind = ctype.upper()
        if name == "id":
            values[name] = f"'{uuid.uuid4().hex}'"
        elif name == "request_id":
            values[name] = f"'{request_id}'"
        elif name == "status":
            values[name] = "'completed'"
        elif name == "usage_source":
            values[name] = "'unavailable'"
        elif name == "cost":
            values[name] = "'1.5'"
        elif name == "currency":
            values[name] = "'EUR'"
        elif "DATETIME" in kind:
            values[name] = f"'{T0}'"
        elif "INT" in kind or "BOOL" in kind or "NUMERIC" in kind:
            values[name] = "0"
        else:
            values[name] = "'x'"
    conn.execute(
        f"INSERT INTO usage_records ({', '.join(values)}) "  # noqa: S608
        f"VALUES ({', '.join(values.values())})"
    )


def test_up_and_down_on_sqlite(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{path}"
    assert _alembic(url, "upgrade", "0071").returncode == 0
    with closing(sqlite3.connect(path)) as conn:
        _insert_usage_row(conn, "before")
        conn.commit()

    up = _alembic(url, "upgrade", "0072")
    assert up.returncode == 0, up.stderr[-2000:]
    with closing(sqlite3.connect(path)) as conn:
        # History is untouched: the cost stands and no price is claimed for it.
        assert conn.execute(
            "SELECT cost, search_price_id FROM usage_records WHERE request_id = 'before'"
        ).fetchall() == [(1.5, None)]
        price_id = uuid.uuid4().hex
        insert = (
            "INSERT INTO search_prices (id, backend, tier, per_request, currency, "
            "effective_from, source, created_at) VALUES (?, 'linkup', '', ?, 'EUR', ?, 'manual', ?)"
        )
        conn.execute(insert, (price_id, "0.007", T0, T0))
        conn.execute("UPDATE usage_records SET search_price_id = ?", (price_id,))
        conn.commit()
        # One price per backend, tier and moment.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, (uuid.uuid4().hex, "0.1", T0, T0))

    down = _alembic(url, "downgrade", "0071")
    assert down.returncode == 0, down.stderr[-2000:]
    with closing(sqlite3.connect(path)) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(usage_records)")]
        assert "search_price_id" not in columns
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master")]
        assert "search_prices" not in tables
        assert conn.execute("SELECT cost FROM usage_records").fetchall() == [(1.5,)]
    assert _alembic(url, "upgrade", "head").returncode == 0


_PG = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture()
def scratch_postgres() -> Iterator[Callable[[], str]]:
    import asyncpg
    from conftest import _parse_admin_url

    parts = _parse_admin_url()
    name = f"t_mig0072_{uuid.uuid4().hex[:8]}"

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
    assert _alembic(url, "upgrade", "0071").returncode == 0

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

    # An existing usage row, as the owner's deployment has: every NOT NULL
    # column without a default filled from the live schema.
    needed = asyncio.run(
        query(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'usage_records' AND is_nullable = 'NO' "
            "AND column_default IS NULL"
        )
    )
    fill: dict[str, str] = {}
    for name, kind in needed:
        if name == "id":
            fill[name] = f"'{uuid.uuid4()}'"
        elif name == "status":
            fill[name] = "'completed'"
        elif name == "usage_source":
            fill[name] = "'unavailable'"
        elif name == "cost":
            fill[name] = "1.5"
        elif name == "currency":
            fill[name] = "'EUR'"
        elif "timestamp" in str(kind):
            fill[name] = "now()"
        elif kind in ("integer", "bigint", "numeric", "smallint"):
            fill[name] = "0"
        elif kind == "boolean":
            fill[name] = "false"
        else:
            fill[name] = "'before'"
    asyncio.run(
        query(
            f"INSERT INTO usage_records ({', '.join(fill)}) "  # noqa: S608
            f"VALUES ({', '.join(fill.values())})"
        )
    )

    up = _alembic(url, "upgrade", "0072")
    assert up.returncode == 0, up.stderr[-2000:]
    # History untouched: cost stands, no price claimed for it.
    assert asyncio.run(query("SELECT cost, search_price_id FROM usage_records")) == [
        (Decimal("1.5"), None)
    ]
    asyncio.run(
        query(
            "INSERT INTO search_prices (id, backend, tier, per_request, currency, "  # noqa: S608
            "effective_from, source, created_at) "
            f"VALUES ('{uuid.uuid4()}', 'linkup', '', 0.007, 'EUR', now(), 'manual', now())"
        )
    )
    columns = asyncio.run(
        query(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'usage_records'"
        )
    )
    assert ("search_price_id",) in columns

    down = _alembic(url, "downgrade", "0071")
    assert down.returncode == 0, down.stderr[-2000:]
    columns = asyncio.run(
        query(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'usage_records'"
        )
    )
    assert ("search_price_id",) not in columns
    assert asyncio.run(query("SELECT cost FROM usage_records")) == [(Decimal("1.5"),)]
    assert _alembic(url, "upgrade", "head").returncode == 0
