# ruff: noqa: S608  (fixed SQL built from test constants)
"""Migration 0080: users' own redaction rules and the redaction log column (ADR 0112).

On SQLite: existing usage rows get a null log entry, a user rule can never be
``off`` (the CHECK, beside the API's validation), a pattern needs a regex and a
mode rule has none, names are unique per user and kind, deleting the user takes
their rules with them, and the downgrade removes both.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

import pytest
from test_migration_0067 import _alembic

T0 = "2026-11-08 12:00:00.000000"


def _user(conn: sqlite3.Connection) -> str:
    uid = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO users (id, issuer, subject, created_at, updated_at) "
        f"VALUES ('{uid}', 'https://idp.test', '{uid}', '{T0}', '{T0}')"
    )
    return uid


def _rule(
    conn: sqlite3.Connection, user: str, kind: str, name: str, regex: str | None, mode: str
) -> None:
    conn.execute(
        "INSERT INTO user_redaction_rules (id, user_id, kind, name, regex, mode, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (uuid.uuid4().hex, user, kind, name, regex, mode, T0, T0),
    )


def test_the_table_and_the_column_arrive_and_enforce_what_a_user_may_write(
    tmp_path: Path,
) -> None:
    path = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{path}"
    assert _alembic(url, "upgrade", "0079").returncode == 0
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT INTO usage_records (id, request_id, status, model_name, streamed, "
            "prompt_tokens, completion_tokens, total_tokens, cached_prompt_tokens, "
            "reasoning_tokens, usage_source, cost, currency, redacted_entity_count, "
            f"created_at, updated_at) VALUES ('{uuid.uuid4().hex}', 'r', 'completed', 'm', 0, "
            f"0, 0, 0, 0, 0, 'unavailable', 0, 'EUR', 0, '{T0}', '{T0}')"
        )
        conn.commit()

    up = _alembic(url, "upgrade", "0080")
    assert up.returncode == 0, up.stderr[-2000:]

    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        # Existing rows have no log entry: a real NULL, not a JSON null.
        assert conn.execute("SELECT redaction_detail IS NULL FROM usage_records").fetchall() == [
            (1,)
        ]

        user = _user(conn)
        _rule(conn, user, "mode", "PERSON", None, "redact")
        _rule(conn, user, "pattern", "PROJECT", r"PRJ-\d+", "block")
        # Never off.
        with pytest.raises(sqlite3.IntegrityError):
            _rule(conn, user, "mode", "URL", None, "off")
        # Shape: a pattern has a regex, a mode rule has none.
        with pytest.raises(sqlite3.IntegrityError):
            _rule(conn, user, "pattern", "NOREGEX", None, "redact")
        with pytest.raises(sqlite3.IntegrityError):
            _rule(conn, user, "mode", "EMAIL_ADDRESS", "x", "redact")
        # Unique per user and kind.
        with pytest.raises(sqlite3.IntegrityError):
            _rule(conn, user, "mode", "PERSON", None, "block")
        other = _user(conn)
        _rule(conn, other, "mode", "PERSON", None, "block")

        conn.execute(f"DELETE FROM users WHERE id = '{user}'")
        assert conn.execute("SELECT count(*) FROM user_redaction_rules").fetchone() == (1,)


def test_the_downgrade_removes_both(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{path}"
    assert _alembic(url, "upgrade", "0080").returncode == 0

    down = _alembic(url, "downgrade", "0079")
    assert down.returncode == 0, down.stderr[-2000:]

    with closing(sqlite3.connect(path)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "user_redaction_rules" not in tables
        columns = {r[1] for r in conn.execute("PRAGMA table_info(usage_records)")}
        assert "redaction_detail" not in columns
