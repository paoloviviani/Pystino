"""Migration 0060: capabilities became per-group flags.

Before it, who held a capability was the deployment setting (`everyone` |
`groups`) folded with the flags; after it, the flag alone decides and the
setting is the default new groups are created with. The upgrade must not
change what is in force: where `everyone` was folded, every existing group
gets the flag (that is what was in force); where `groups` was folded, the
stored flags stand. The resulting defaults land in one appended
carry-forward row, so the poll keeps reading them. The test builds the
pre-0060 tables raw, runs the migration through Alembic's Operations, and
checks the flags and the row — including the environment baseline with no
stored row, and the legacy `everyone`/`groups` spellings a 0.7.x row may
carry.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "0060_capabilities_per_group.py"
)

DDL = """
CREATE TABLE oidc_config (
    id CHAR(32) PRIMARY KEY,
    auto_provision BOOLEAN,
    unknown_user_policy VARCHAR(16),
    join_mode VARCHAR(16),
    required_groups JSON,
    group_fallback VARCHAR(16),
    groups_claim VARCHAR(255),
    admin_groups JSON,
    group_mappings JSON,
    code_agents_mode VARCHAR(16),
    api_keys_mode VARCHAR(16),
    reason VARCHAR(500) NOT NULL,
    created_by CHAR(32),
    created_at DATETIME NOT NULL,
    CONSTRAINT ck_oidc_config_code_agents_mode
        CHECK (code_agents_mode IS NULL OR code_agents_mode IN ('everyone', 'groups')),
    CONSTRAINT ck_oidc_config_api_keys_mode
        CHECK (api_keys_mode IS NULL OR api_keys_mode IN ('everyone', 'groups'))
);
CREATE TABLE groups (
    id CHAR(32) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT 1,
    code_agents_allowed BOOLEAN NOT NULL DEFAULT 0,
    api_keys_allowed BOOLEAN NOT NULL DEFAULT 0
)
"""

T0 = "2026-10-01 12:00:00.000000"
T1 = "2026-10-14 12:00:00.000000"
REASON = "migration 0060: capabilities became per-group"


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0060", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def run_migration(monkeypatch: pytest.MonkeyPatch):
    """Raw SQLite engines at the pre-0060 shape, with the upgrade run; env
    scrubbed so each test names exactly the baseline it means."""
    engines: list[sa.Engine] = []
    monkeypatch.delenv("GATEWAY_ACCESS__CODE_AGENTS", raising=False)
    monkeypatch.delenv("GATEWAY_ACCESS__API_KEYS", raising=False)

    def build(
        *,
        row: dict[str, str | None] | None = None,
        env: dict[str, str] | None = None,
    ) -> sa.Engine:
        for key, value in (env or {}).items():
            monkeypatch.setenv(key, value)
        engine = sa.create_engine("sqlite://")
        engines.append(engine)
        with engine.begin() as conn:
            for statement in DDL.split(";"):
                if statement.strip():
                    conn.execute(sa.text(statement))
            conn.execute(
                sa.text(
                    "INSERT INTO groups (id, name, code_agents_allowed, api_keys_allowed) VALUES "
                    "(:a, 'agents', 1, 0), (:b, 'chat', 0, 0)"
                ),
                {"a": uuid.uuid4().hex, "b": uuid.uuid4().hex},
            )
            if row is not None:
                conn.execute(
                    sa.text(
                        "INSERT INTO oidc_config (id, join_mode, code_agents_mode, "
                        "api_keys_mode, reason, created_at) VALUES (:id, :join, :code, "
                        ":api, 'seed', :at)"
                    ),
                    {
                        "id": uuid.uuid4().hex,
                        "join": row.get("join_mode"),
                        "code": row.get("code_agents_mode"),
                        "api": row.get("api_keys_mode"),
                        "at": T0,
                    },
                )
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            _load_migration().upgrade()
        return engine

    yield build

    for engine in engines:
        engine.dispose()


def _flags(engine: sa.Engine) -> dict[str, tuple[int, int]]:
    with engine.connect() as conn:
        return {
            name: (code, api)
            for name, code, api in conn.execute(
                sa.text("SELECT name, code_agents_allowed, api_keys_allowed FROM groups")
            ).all()
        }


def _newest_modes(engine: sa.Engine) -> dict[str, object]:
    with engine.connect() as conn:
        return dict(
            conn.execute(sa.text("SELECT * FROM oidc_config ORDER BY created_at DESC LIMIT 1"))
            .mappings()
            .one()
        )


def test_everyone_turns_every_existing_group_on(run_migration) -> None:
    """No row, no env: `everyone` was in force, so every group gets the flag —
    the upgrade changes nothing anyone holds. The appended row records on/on."""
    engine = run_migration()
    assert _flags(engine) == {"agents": (1, 1), "chat": (1, 1)}
    newest = _newest_modes(engine)
    assert newest["reason"] == REASON
    assert newest["code_agents_mode"] == "on"
    assert newest["api_keys_mode"] == "on"


def test_groups_mode_keeps_the_stored_flags(run_migration) -> None:
    """A stored `groups` decision: flags stand, and the appended row records
    off/off while carrying the row's other answers forward."""
    engine = run_migration(
        row={"join_mode": "open", "code_agents_mode": "groups", "api_keys_mode": "groups"}
    )
    assert _flags(engine) == {"agents": (1, 0), "chat": (0, 0)}
    newest = _newest_modes(engine)
    assert newest["reason"] == REASON
    assert newest["code_agents_mode"] == "off"
    assert newest["api_keys_mode"] == "off"
    assert newest["join_mode"] == "open"


def test_env_baseline_answers_without_a_row(run_migration) -> None:
    """No row: the environment decides per capability — groups here for code
    (flags untouched), everyone for api (turned on)."""
    engine = run_migration(env={"GATEWAY_ACCESS__CODE_AGENTS": "groups"})
    assert _flags(engine) == {"agents": (1, 1), "chat": (0, 1)}
    newest = _newest_modes(engine)
    assert newest["code_agents_mode"] == "off"
    assert newest["api_keys_mode"] == "on"


def test_legacy_row_spellings_fold_without_behaviour_change(run_migration) -> None:
    """A 0.7.x row speaks `everyone`/`groups`; the upgrade reads what it
    meant, not what it spelled."""
    engine = run_migration(
        row={"join_mode": None, "code_agents_mode": "everyone", "api_keys_mode": "groups"}
    )
    assert _flags(engine) == {"agents": (1, 0), "chat": (1, 0)}
    newest = _newest_modes(engine)
    assert newest["code_agents_mode"] == "on"
    assert newest["api_keys_mode"] == "off"
