"""Migration 0075: per-minute price and the billed audio seconds.

Additive, three nullable columns; the downgrade drops them and the schema
upgrades again to head afterwards.
"""

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
    assert _alembic(url, "upgrade", "0074").returncode == 0
    assert "per_minute" not in _columns(path, "model_prices")
    assert "audio_seconds" not in _columns(path, "usage_records")

    up = _alembic(url, "upgrade", "0075")
    assert up.returncode == 0, up.stderr[-2000:]
    assert "per_minute" in _columns(path, "model_prices")
    usage = _columns(path, "usage_records")
    assert "audio_seconds" in usage
    assert "audio_seconds_source" in usage

    down = _alembic(url, "downgrade", "0074")
    assert down.returncode == 0, down.stderr[-2000:]
    assert "per_minute" not in _columns(path, "model_prices")
    assert "audio_seconds" not in _columns(path, "usage_records")
    assert _alembic(url, "upgrade", "head").returncode == 0
