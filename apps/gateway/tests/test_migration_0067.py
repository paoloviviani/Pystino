"""Migration 0067: a global rule may apply to each user.

0061 let ``applies_to`` be ``each_member`` on a group rule only; 0067 widens the
same check so ``each_user`` is accepted on a global rule and nowhere else. It
runs through the real chain (a database at 0066, rows in it, then up and down)
rather than a hand-built table, because the thing at risk is the constraint
swap, which is dialect machinery: a batch rebuild on SQLite, a plain
DROP/ADD CONSTRAINT on PostgreSQL. The PostgreSQL leg runs when
``TEST_DATABASE_URL`` is set, in a scratch database of its own.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path

import pytest

_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
T0 = "2026-10-21 12:00:00.000000"


def _alembic(url: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  (our own interpreter, fixed arguments)
        [sys.executable, "-m", "alembic", "-c", str(_INI), "-x", f"url={url}", *args],
        capture_output=True,
        text=True,
        check=False,
    )


_INSERT = (
    "INSERT INTO limit_rules (id, name, scope, scope_id, metric, window_seconds, period, "
    "limit_value, is_active, created_at, updated_at, applies_to) "
    "VALUES ({id}, {name}, {scope}, {scope_id}, 'cost', NULL, 'month', 5, {active}, "
    "{at}, {at}, {applies_to})"
)


def _insert_sqlite(
    conn: sqlite3.Connection, scope: str, scope_id: str | None, applies_to: str
) -> None:
    conn.execute(
        _INSERT.format(
            id=f"'{uuid.uuid4().hex}'",
            name="'r'",
            scope=f"'{scope}'",
            scope_id="NULL" if scope_id is None else f"'{scope_id}'",
            active=1,
            at=f"'{T0}'",
            applies_to=f"'{applies_to}'",
        )
    )


def test_up_and_down_on_sqlite(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'm.db'}"
    assert _alembic(url, "upgrade", "0066").returncode == 0
    path = tmp_path / "m.db"

    with closing(sqlite3.connect(path)) as conn:
        # Under the old check a per-user rule cannot exist.
        with pytest.raises(sqlite3.IntegrityError):
            _insert_sqlite(conn, "global", None, "each_user")
        _insert_sqlite(conn, "group", uuid.uuid4().hex, "each_member")
        conn.commit()

    up = _alembic(url, "upgrade", "0067")
    assert up.returncode == 0, up.stderr[-2000:]
    with closing(sqlite3.connect(path)) as conn:
        _insert_sqlite(conn, "global", None, "each_user")
        conn.commit()
        # Still refused anywhere else, and the old rule survived the rebuild.
        for scope, scope_id in (("group", uuid.uuid4().hex), ("user", uuid.uuid4().hex)):
            with pytest.raises(sqlite3.IntegrityError):
                _insert_sqlite(conn, scope, scope_id, "each_user")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_sqlite(conn, "global", None, "each_member")
        (count,) = conn.execute("SELECT count(*) FROM limit_rules").fetchone()
        assert count == 2
        # One per-user rule per metric and window; the unique index still holds.
        with pytest.raises(sqlite3.IntegrityError):
            _insert_sqlite(conn, "global", None, "each_user")

    # The downgrade refuses while a per-user rule exists, and says why.
    refused = _alembic(url, "downgrade", "0066")
    assert refused.returncode != 0
    assert "apply to each user" in refused.stderr
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("DELETE FROM limit_rules WHERE applies_to = 'each_user'")
        conn.commit()
    down = _alembic(url, "downgrade", "0066")
    assert down.returncode == 0, down.stderr[-2000:]
    with closing(sqlite3.connect(path)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_sqlite(conn, "global", None, "each_user")
        (count,) = conn.execute("SELECT count(*) FROM limit_rules").fetchone()
        assert count == 1


_PG = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture()
def scratch_postgres() -> Iterator[Callable[[str], str]]:
    """A database of its own on the test server, dropped afterwards."""
    import asyncpg
    from conftest import _parse_admin_url

    parts = _parse_admin_url()
    name = f"t_mig0067_{uuid.uuid4().hex[:8]}"

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
            lambda _unused="": (
                f"postgresql+asyncpg://{parts['user']}:{parts['password']}"
                f"@{parts['host']}:{parts['port']}/{name}"
            )
        )
    finally:
        asyncio.run(run(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))


@pytest.mark.skipif(not _PG, reason="TEST_DATABASE_URL is not set")
def test_up_and_down_on_postgres(scratch_postgres: Callable[[str], str]) -> None:
    import asyncpg
    from conftest import _parse_admin_url

    url = scratch_postgres("")
    parts = _parse_admin_url()
    assert _alembic(url, "upgrade", "0066").returncode == 0

    async def exec_sql(statements: list[str]) -> list[str]:
        """Run each statement; report the SQLSTATE class of a refusal."""
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

    def insert(scope: str, applies_to: str, scope_id: str | None = None) -> str:
        return (
            "INSERT INTO limit_rules (id, name, scope, scope_id, metric, window_seconds, "  # noqa: S608
            "period, limit_value, is_active, created_at, updated_at, applies_to) VALUES "
            f"('{uuid.uuid4()}', 'r', '{scope}', "
            f"{'NULL' if scope_id is None else repr(scope_id)}, 'cost', NULL, 'month', 5, "
            f"true, now(), now(), '{applies_to}')"
        )

    group_id = str(uuid.uuid4())
    assert asyncio.run(exec_sql([insert("global", "each_user")])) == ["refused"]
    assert asyncio.run(exec_sql([insert("group", "each_member", group_id)])) == ["ok"]

    up = _alembic(url, "upgrade", "0067")
    assert up.returncode == 0, up.stderr[-2000:]
    assert asyncio.run(
        exec_sql(
            [
                insert("global", "each_user"),
                insert("global", "each_user"),
                insert("group", "each_user", str(uuid.uuid4())),
                insert("user", "each_user", str(uuid.uuid4())),
                insert("global", "each_member"),
            ]
        )
    ) == ["ok", "refused", "refused", "refused", "refused"]

    assert _alembic(url, "downgrade", "0066").returncode != 0
    asyncio.run(exec_sql(["DELETE FROM limit_rules WHERE applies_to = 'each_user'"]))
    down = _alembic(url, "downgrade", "0066")
    assert down.returncode == 0, down.stderr[-2000:]
    assert asyncio.run(exec_sql([insert("global", "each_user")])) == ["refused"]
    assert _alembic(url, "upgrade", "head").returncode == 0
