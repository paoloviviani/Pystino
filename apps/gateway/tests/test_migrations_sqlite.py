"""The whole migration chain runs on SQLite.

The unit suite builds its schema with ``create_all`` and a deployment migrates
PostgreSQL, so nothing else ran ``alembic upgrade head`` on SQLite, and
``scripts/smoke_test.sh`` (which does) sat broken for weeks: 0035 spelled
``DROP TABLE … CASCADE`` and 0040 added a foreign key with a bare
``add_column``, and SQLite rejects both. A migration written for PostgreSQL
only is a bug here, because SQLite is the dialect the smoke test, the unit
suite and the quick start run on.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

_INI = Path(__file__).resolve().parents[1] / "alembic.ini"

# Dropped by 0035, when the vector store moved to the chat.
_DROPPED_BY_0035 = {
    "knowledge_chunks",
    "knowledge_documents",
    "knowledge_bases",
    "knowledge_config",
    "file_blobs",
    "files",
    "resource_shares",
}


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  (our own interpreter, fixed arguments)
        [sys.executable, "-m", "alembic", "-c", str(_INI), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_every_migration_applies_on_sqlite(tmp_path: Path) -> None:
    database = tmp_path / "migrated.db"
    result = _alembic("-x", f"url=sqlite+aiosqlite:///{database}", "upgrade", "head")
    assert result.returncode == 0, result.stderr[-2000:]

    # `alembic heads` prints "0055 (head)": the revision a finished chain stamps.
    head = _alembic("heads").stdout.split()[0]
    with closing(sqlite3.connect(database)) as connection:
        stamped = connection.execute("SELECT version_num FROM alembic_version").fetchall()
        tables = {
            name
            for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert stamped == [(head,)]
    assert not tables & _DROPPED_BY_0035
