"""Migration 0069: ``providers.catalogue_id``, the link to a catalogue entry.

Existing rows must stay unlinked (a guessed link would attach region and
retention claims nobody made), the column must take an id afterwards, and the
downgrade must drop it without losing the rows. The PostgreSQL leg runs when
``TEST_DATABASE_URL`` is set, in a scratch database of its own.
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

T0 = "2026-10-22 12:00:00.000000"


def test_up_and_down_on_sqlite(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{path}"
    assert _alembic(url, "upgrade", "0068").returncode == 0
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT INTO providers (id, name, base_url, api_key_encrypted, api_key_hint, "  # noqa: S608
            "extra_headers, is_active, prefix, kind, billing_mode, created_at, updated_at) "
            f"VALUES ('{uuid.uuid4().hex}', 'old', 'https://x/v1', '', '', '{{}}', 1, '', "
            f"'PROVIDER', 'OWN_PRICES', '{T0}', '{T0}')"
        )
        conn.commit()

    up = _alembic(url, "upgrade", "0069")
    assert up.returncode == 0, up.stderr[-2000:]
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute(
            "SELECT name, catalogue_id FROM providers WHERE name = 'old'"
        ).fetchall() == [("old", None)]
        conn.execute("UPDATE providers SET catalogue_id = 'mistral' WHERE name = 'old'")
        conn.commit()
        assert conn.execute("SELECT catalogue_id FROM providers WHERE name = 'old'").fetchall() == [
            ("mistral",)
        ]

    down = _alembic(url, "downgrade", "0068")
    assert down.returncode == 0, down.stderr[-2000:]
    with closing(sqlite3.connect(path)) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(providers)")]
        assert "catalogue_id" not in columns
        assert conn.execute("SELECT name FROM providers WHERE name = 'old'").fetchall() == [
            ("old",)
        ]
    assert _alembic(url, "upgrade", "head").returncode == 0


_PG = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture()
def scratch_postgres() -> Iterator[Callable[[], str]]:
    import asyncpg
    from conftest import _parse_admin_url

    parts = _parse_admin_url()
    name = f"t_mig0069_{uuid.uuid4().hex[:8]}"

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
    assert _alembic(url, "upgrade", "0068").returncode == 0

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

    asyncio.run(
        query(
            "INSERT INTO providers (id, name, base_url, api_key_encrypted, api_key_hint, "  # noqa: S608
            "extra_headers, is_active, prefix, kind, billing_mode, created_at, updated_at) "
            f"VALUES ('{uuid.uuid4()}', 'old', 'https://x/v1', '', '', '{{}}', true, '', "
            "'provider', 'own_prices', now(), now())"
        )
    )
    up = _alembic(url, "upgrade", "0069")
    assert up.returncode == 0, up.stderr[-2000:]
    assert asyncio.run(query("SELECT name, catalogue_id FROM providers WHERE name = 'old'")) == [
        ("old", None)
    ]
    asyncio.run(query("UPDATE providers SET catalogue_id = 'mistral' WHERE name = 'old'"))
    assert asyncio.run(query("SELECT catalogue_id FROM providers WHERE name = 'old'")) == [
        ("mistral",)
    ]

    down = _alembic(url, "downgrade", "0068")
    assert down.returncode == 0, down.stderr[-2000:]
    columns = asyncio.run(
        query("SELECT column_name FROM information_schema.columns WHERE table_name = 'providers'")
    )
    assert ("catalogue_id",) not in columns
    assert asyncio.run(query("SELECT name FROM providers WHERE name = 'old'")) == [("old",)]
    assert _alembic(url, "upgrade", "head").returncode == 0
