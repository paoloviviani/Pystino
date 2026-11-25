# ruff: noqa: E501  (fixed SQL built from test constants)
"""Migration 0091: stored GITLAB_TOKEN patterns match the whole routable token.

Only an entry whose regex equals the old template string exactly is rewritten, in an
administrator policy and in a user's own policy; an edited expression is left alone. The
downgrade restores the old expression on the entries it changed.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from test_migration_0067 import _alembic

T0 = "2026-11-25 12:00:00.000000"
OLD = "glpat-[A-Za-z0-9_-]{20,}"
NEW = "gl(pat|dt|rt|ptt|ft|imt|oas|cbt|soat|ffct|agent)-[A-Za-z0-9_-]{20,}(\\.[A-Za-z0-9_-]+)*"
EDITED = "glpat-[A-Za-z0-9_-]{30,}"


def _seed(path: Path) -> tuple[str, str]:
    """An admin policy and a user policy, each holding the old template and an edited one."""
    policy_id, user_policy_id = uuid.uuid4().hex, uuid.uuid4().hex
    user_id = uuid.uuid4().hex
    policy = {
        "patterns": [
            {"name": "GITLAB_TOKEN", "regex": OLD, "mode": "block"},
            {"name": "RENAMED", "regex": OLD, "mode": "redact"},
            {"name": "MINE", "regex": EDITED, "mode": "block"},
        ],
        "keywords": [],
    }
    rules = [
        {"kind": "pattern", "name": "GITLAB_TOKEN", "regex": OLD, "mode": "block"},
        {"kind": "pattern", "name": "MINE", "regex": EDITED, "mode": "block"},
        {"kind": "mode", "name": "PERSON", "regex": None, "mode": "redact"},
    ]
    with closing(sqlite3.connect(path)) as conn:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(redaction_policies)")]
        assert "policy" in columns
        conn.execute(
            "INSERT INTO redaction_policies (id, name, policy, is_active, reason, shared_with_users, created_at, updated_at) VALUES (?, 'P', ?, 1, '', 0, ?, ?)",
            (policy_id, json.dumps(policy), T0, T0),
        )
        conn.execute(
            "INSERT INTO users (id, issuer, subject, created_at, updated_at) VALUES (?, 'iss', 'sub', ?, ?)",
            (user_id, T0, T0),
        )
        conn.execute(
            "INSERT INTO user_policies (id, user_id, name, rules, created_at, updated_at) VALUES (?, ?, 'Mine', ?, ?, ?)",
            (user_policy_id, user_id, json.dumps(rules), T0, T0),
        )
        conn.commit()
    return policy_id, user_policy_id


def _regexes(path: Path) -> tuple[list[str | None], list[str | None]]:
    with closing(sqlite3.connect(path)) as conn:
        (policy,) = conn.execute("SELECT policy FROM redaction_policies").fetchone()
        (rules,) = conn.execute("SELECT rules FROM user_policies").fetchone()
    return (
        [p["regex"] for p in json.loads(policy)["patterns"]],
        [r["regex"] for r in json.loads(rules)],
    )


def test_exact_old_template_is_rewritten_and_restored(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{path}"
    assert _alembic(url, "upgrade", "0090").returncode == 0
    _seed(path)

    up = _alembic(url, "upgrade", "0091")
    assert up.returncode == 0, up.stderr[-2000:]
    assert _regexes(path) == ([NEW, NEW, EDITED], [NEW, EDITED, None])

    down = _alembic(url, "downgrade", "0090")
    assert down.returncode == 0, down.stderr[-2000:]
    assert _regexes(path) == ([OLD, OLD, EDITED], [OLD, EDITED, None])


def test_empty_tables_migrate_both_ways(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'm.db'}"
    assert _alembic(url, "upgrade", "0091").returncode == 0
    assert _alembic(url, "downgrade", "0090").returncode == 0
