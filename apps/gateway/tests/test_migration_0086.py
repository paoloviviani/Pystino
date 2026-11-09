"""Migration 0086: the "Allow remote classifiers" setting, off for every existing row."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from test_migration_0067 import _alembic


def test_the_setting_is_off_by_default_and_reverses(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{path}"
    assert _alembic(url, "upgrade", "0085").returncode == 0
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("INSERT INTO privacy_settings (id, updated_at) VALUES (1, '2026-11-09')")
        conn.commit()
    up = _alembic(url, "upgrade", "0086")
    assert up.returncode == 0, up.stderr[-2000:]
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT allow_remote_classifiers FROM privacy_settings").fetchall() == [
            (0,)
        ]
    down = _alembic(url, "downgrade", "0085")
    assert down.returncode == 0, down.stderr[-2000:]
    with closing(sqlite3.connect(path)) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(privacy_settings)")}
    assert "allow_remote_classifiers" not in cols
