"""Migration 0070: the withdrawn ``keycloak`` kind and ``keycloak_admin`` adapter.

Rows that name them move to ``generic`` / ``none`` with the dead credentials
cleared, and a group source fed by that adapter moves to ``none``; every other
row is untouched.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from test_migration_0067 import _alembic

T0 = "2026-10-22 12:00:00.000000"


def test_keycloak_rows_become_generic_and_other_rows_are_untouched(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{path}"
    assert _alembic(url, "upgrade", "0069").returncode == 0
    rows = [
        ("kc", "keycloak", "keycloak_admin", "secret", 1, "claim"),
        ("kd", "keycloak", "keycloak_admin", "secret", 1, "directory"),
        ("au", "authelia", "authelia_file", "cfg", 1, "directory"),
        ("sc", "entra", "scim", None, 0, "directory"),
    ]
    with closing(sqlite3.connect(path)) as conn:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(identity_providers)")]
        assert "sync_config_encrypted" in columns
        for name, kind, adapter, config, confirmed, groups in rows:
            conn.execute(
                "INSERT INTO identity_providers (id, name, issuer, client_id, "  # noqa: S608
                "client_secret_encrypted, kind, sync_adapter, sync_config_encrypted, "
                "sync_confirmed, group_source, created_at, updated_at) "
                f"VALUES ('{uuid.uuid4().hex}', '{name}', 'https://{name}.example.org', 'c', 'x', "
                f"'{kind}', '{adapter}', ?, {confirmed}, '{groups}', '{T0}', '{T0}')",
                (config,),
            )
        conn.commit()

    up = _alembic(url, "upgrade", "0070")
    assert up.returncode == 0, up.stderr[-2000:]
    with closing(sqlite3.connect(path)) as conn:
        got = conn.execute(
            "SELECT name, kind, sync_adapter, sync_config_encrypted, sync_confirmed, group_source "
            "FROM identity_providers ORDER BY name"
        ).fetchall()
    assert got == [
        ("au", "authelia", "authelia_file", "cfg", 1, "directory"),
        ("kc", "generic", "none", None, 0, "claim"),
        ("kd", "generic", "none", None, 0, "none"),
        ("sc", "entra", "scim", None, 0, "directory"),
    ]
    assert _alembic(url, "upgrade", "head").returncode == 0
