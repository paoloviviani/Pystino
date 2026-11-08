"""Migration 0081: privacy routing tables and the provider ``is_local`` flag (ADR 0113).

On SQLite: the default "Fast" profile is seeded once, existing providers are not
local, rules store JSON conditions and actions, profile names are unique, a
profile's fallback pointer clears when its target is deleted, and the downgrade
removes everything it added.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

import pytest
from test_migration_0067 import _alembic

T0 = "2026-11-08 12:00:00.000000"


def test_the_tables_the_default_profile_and_the_flag_arrive(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{path}"
    assert _alembic(url, "upgrade", "0080").returncode == 0
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT INTO providers (id, name, base_url, api_key_encrypted, api_key_hint, "
            "extra_headers, is_active, prefix, kind, billing_mode, created_at, updated_at, "
            "admin_disabled) VALUES (?, 'old', 'http://x', '', '', '{}', 1, '', 'provider', "
            f"'own_prices', '{T0}', '{T0}', 0)",
            (uuid.uuid4().hex,),
        )
        conn.commit()

    up = _alembic(url, "upgrade", "0081")
    assert up.returncode == 0, up.stderr[-2000:]

    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        # An existing provider is "cloud" until an administrator says otherwise.
        assert conn.execute("SELECT is_local FROM providers WHERE name = 'old'").fetchall() == [(0,)]
        profiles = conn.execute(
            "SELECT name, engine, fallback, is_default FROM detector_profiles"
        ).fetchall()
        assert profiles == [("Fast", "presidio", "refuse", 1)]

        conn.execute(
            "INSERT INTO privacy_rules (id, kind, name, enabled, observe_only, position, "
            "conditions, action, created_at, updated_at) VALUES (?, 'read', 'R', 1, 0, 0, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex,
                json.dumps([{"key": "filetype", "values": ["scan"], "negate": False}]),
                json.dumps({"type": "refuse"}),
                T0,
                T0,
            ),
        )
        stored = conn.execute("SELECT conditions, action FROM privacy_rules").fetchone()
        assert json.loads(stored[1]) == {"type": "refuse"}

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO detector_profiles (id, name, engine, labels, fallback, is_default, "
                f"description, created_at, updated_at) VALUES (?, 'Fast', 'x', '[]', 'refuse', 0, '', '{T0}', '{T0}')",
                (uuid.uuid4().hex,),
            )

        fast = conn.execute("SELECT id FROM detector_profiles").fetchone()[0]
        other = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO detector_profiles (id, name, engine, labels, fallback, fallback_profile_id, "
            f"is_default, description, created_at, updated_at) VALUES (?, 'M', 'gliner', '[]', "
            f"'fallback_to', ?, 0, '', '{T0}', '{T0}')",
            (other, fast),
        )
        conn.execute("DELETE FROM detector_profiles WHERE id = ?", (fast,))
        assert conn.execute(
            "SELECT fallback_profile_id FROM detector_profiles WHERE id = ?", (other,)
        ).fetchone() == (None,)


def test_the_downgrade_removes_what_the_upgrade_added(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{path}"
    assert _alembic(url, "upgrade", "0081").returncode == 0

    down = _alembic(url, "downgrade", "0080")
    assert down.returncode == 0, down.stderr[-2000:]

    with closing(sqlite3.connect(path)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not {"privacy_rules", "detector_profiles"} & tables
        columns = {r[1] for r in conn.execute("PRAGMA table_info(providers)")}
        assert "is_local" not in columns
