"""Migration 0071: the ``local_connectors`` capability.

Existing groups are switched ON (the feature is new and opt-out per group), the
column defaults to off for a row inserted without it, the policy default gets
its check constraint, and the downgrade removes both.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

import pytest
from test_migration_0067 import _alembic

T0 = "2026-10-23 12:00:00.000000"


def _groups(path: Path) -> list[tuple[str, int, int]]:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(
            "SELECT name, code_agents_allowed, local_connectors_allowed FROM groups ORDER BY name"
        ).fetchall()


def test_existing_groups_get_local_connectors_on(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{path}"
    assert _alembic(url, "upgrade", "0070").returncode == 0
    with closing(sqlite3.connect(path)) as conn:
        for name, agents in (("agents", 1), ("chat-only", 0)):
            conn.execute(
                "INSERT INTO groups (id, name, source, is_active, created_at, updated_at, "  # noqa: S608
                "code_agents_allowed, api_keys_allowed) "
                f"VALUES ('{uuid.uuid4().hex}', '{name}', 'manual', 1, '{T0}', '{T0}', {agents}, 0)"
            )
        conn.commit()
        columns = {r[1] for r in conn.execute("PRAGMA table_info(groups)")}
        assert "local_connectors_allowed" not in columns

    up = _alembic(url, "upgrade", "0071")
    assert up.returncode == 0, up.stderr[-2000:]
    assert _groups(path) == [("agents", 1, 1), ("chat-only", 0, 1)]

    with closing(sqlite3.connect(path)) as conn:
        default = conn.execute(
            "SELECT dflt_value FROM pragma_table_info('groups') "
            "WHERE name = 'local_connectors_allowed'"
        ).fetchone()
        assert default == ("false",)
        with pytest.raises(sqlite3.IntegrityError, match="ck_oidc_config_local_connectors_mode"):
            conn.execute(
                "INSERT INTO oidc_config (id, created_at, local_connectors_mode) VALUES "
                "(lower(hex(randomblob(16))), '2026-10-23', 'nonsense')"
            )
        conn.execute(
            "INSERT INTO oidc_config (id, created_at, local_connectors_mode) VALUES "
            "(lower(hex(randomblob(16))), '2026-10-23', 'off')"
        )

    assert _alembic(url, "upgrade", "head").returncode == 0
    down = _alembic(url, "downgrade", "0070")
    assert down.returncode == 0, down.stderr[-2000:]
    with closing(sqlite3.connect(path)) as conn:
        groups = {r[1] for r in conn.execute("PRAGMA table_info(groups)")}
        policy = {r[1] for r in conn.execute("PRAGMA table_info(oidc_config)")}
    assert "local_connectors_allowed" not in groups
    assert "local_connectors_mode" not in policy
