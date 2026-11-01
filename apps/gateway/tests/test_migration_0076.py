"""Migration 0076: the per-extra-megapixel image rate. One nullable column."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from test_migration_0067 import _alembic


def _columns(path: Path, table: str) -> list[str]:
    with closing(sqlite3.connect(path)) as conn:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def test_up_and_down_on_sqlite(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{path}"
    assert _alembic(url, "upgrade", "0075").returncode == 0
    assert "per_extra_megapixel" not in _columns(path, "model_prices")

    up = _alembic(url, "upgrade", "0076")
    assert up.returncode == 0, up.stderr[-2000:]
    assert "per_extra_megapixel" in _columns(path, "model_prices")

    down = _alembic(url, "downgrade", "0075")
    assert down.returncode == 0, down.stderr[-2000:]
    assert "per_extra_megapixel" not in _columns(path, "model_prices")
    assert "per_minute" in _columns(path, "model_prices")
    assert _alembic(url, "upgrade", "head").returncode == 0
