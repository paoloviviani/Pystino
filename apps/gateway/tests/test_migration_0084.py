"""Migration 0084: detector switches, and AGE/ID/EMAIL formats set off.

The two columns are nullable (null: the older ``presidio_ner`` applies), and a policy that
acts on a format no recogniser ever found keeps it, turned off, with a log line.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from test_migration_0067 import _alembic
from test_migration_0083 import _policy, _rows


def test_never_detected_formats_are_kept_but_switched_off(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{path}"
    assert _alembic(url, "upgrade", "0083").returncode == 0
    with closing(sqlite3.connect(path)) as conn:
        _policy(
            conn,
            "Old",
            {
                "formats": {
                    "AGE": {"mode": "redact", "threshold": None},
                    "ID": {"mode": "off", "threshold": None},
                    "EMAIL": {"mode": "anonymise", "threshold": None},
                    "IBAN_CODE": {"mode": "redact", "threshold": None},
                },
                "entities": {"PERSON": {"mode": "redact", "threshold": None}},
            },
        )
        conn.commit()
    up = _alembic(url, "upgrade", "0084")
    assert up.returncode == 0, up.stderr[-2000:]
    assert "AGE" in (up.stderr + up.stdout) and "EMAIL" in (up.stderr + up.stdout)
    policy = json.loads(_rows(path, "redaction_policies")[0]["policy"])
    modes = {name: entry["mode"] for name, entry in policy["formats"].items()}
    # Kept (never dropped), off where they never matched anything; real formats untouched.
    assert modes == {"AGE": "off", "ID": "off", "EMAIL": "off", "IBAN_CODE": "redact"}
    assert policy["entities"]["PERSON"]["mode"] == "redact"
    columns = {row["name"] for row in _rows_pragma(path)}
    assert {"spacy_enabled", "gliner_enabled"} <= columns


def _rows_pragma(path: Path) -> list[dict[str, str]]:
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("PRAGMA table_info(redaction_config)")]


def test_it_downgrades(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{path}"
    assert _alembic(url, "upgrade", "0084").returncode == 0
    assert _alembic(url, "downgrade", "0083").returncode == 0
    assert "spacy_enabled" not in {row["name"] for row in _rows_pragma(path)}
