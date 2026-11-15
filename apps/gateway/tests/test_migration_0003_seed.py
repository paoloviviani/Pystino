"""Migration 0003 seeds the `default` provider only from an explicit upstream.

A fresh database with no GATEWAY_UPSTREAM__BASE_URL gets no provider and no
models; a configured one still seeds; a database that already holds models keeps
the legacy default so they have a provider to point at.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from test_migration_0067 import _alembic


def _upgrade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base_url: str | None) -> Path:
    monkeypatch.delenv("GATEWAY_UPSTREAM__BASE_URL", raising=False)
    monkeypatch.delenv("GATEWAY_UPSTREAM__API_KEY", raising=False)
    if base_url is not None:
        monkeypatch.setenv("GATEWAY_UPSTREAM__BASE_URL", base_url)
    path = tmp_path / "m.db"
    done = _alembic(f"sqlite+aiosqlite:///{path}", "upgrade", "0003")
    assert done.returncode == 0, done.stderr[-2000:]
    return path


def _providers(path: Path) -> list[tuple[str, str]]:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT name, base_url FROM providers").fetchall()


def test_fresh_database_with_no_upstream_seeds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _upgrade(tmp_path, monkeypatch, None)
    assert _providers(path) == []
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT count(*) FROM models").fetchone() == (0,)


def test_a_configured_upstream_still_seeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _upgrade(tmp_path, monkeypatch, "https://llm.example.org/v1/")
    assert _providers(path) == [("default", "https://llm.example.org/v1")]


def test_upgrading_a_database_with_models_keeps_the_legacy_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GATEWAY_UPSTREAM__BASE_URL", raising=False)
    path = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{path}"
    assert _alembic(url, "upgrade", "0002").returncode == 0
    with closing(sqlite3.connect(path)) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(models)") if r[3] and r[4] is None]
        assert "id" in cols
        # Fill every NOT NULL column without a default; the values do not matter.
        values = {c: ("'x'" if c != "id" else "'a1'") for c in cols}
        conn.execute(
            f"INSERT INTO models ({','.join(values)}) VALUES ({','.join(values.values())})"  # noqa: S608
        )
        conn.commit()
    done = _alembic(url, "upgrade", "0003")
    assert done.returncode == 0, done.stderr[-2000:]
    assert _providers(path) == [("default", "https://api.cortecs.ai/v1")]
