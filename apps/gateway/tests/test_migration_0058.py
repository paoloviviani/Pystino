"""Migration 0058: self-mappings for groups claimed by name.

Before the mapping-only rule, any claimed group name equal to a local
group's name landed in it. The rule removes that resolution, so the upgrade
must restate it as what it always should have been spelled as: a self-
mapping (name -> name) for every local group claims were feeding. The test
builds the pre-0058 tables raw, seeds the deployment the upgrade story is
about — a manual group holding OIDC memberships, an imported group, a group
nobody's token names, and one already self-mapped — runs the migration
through Alembic's Operations, and checks the row it appends: one row,
carrying the previous row's answers forward, adding exactly the missing
self-mappings, and skipping the empty case entirely.
"""

from __future__ import annotations

import importlib.util
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from gateway.oidc_policy import OIDCPolicy

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "0058_groups_claimed_by_name_self_mappings.py"
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
    reason VARCHAR(500) NOT NULL,
    created_by CHAR(32),
    created_at TIMESTAMP NOT NULL
);
CREATE TABLE groups (
    id CHAR(32) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    source VARCHAR(16) NOT NULL
);
CREATE TABLE memberships (
    user_id CHAR(32),
    group_id CHAR(32),
    source VARCHAR(16) NOT NULL
)
"""

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
REASON = "migration 0058: self-mappings for groups claimed by name"


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0058", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def run_migration():
    """A raw SQLite engine with the pre-0058 tables, and the migration's
    upgrade() run against it. Yields the engine."""

    def build(*, seed: bool = True) -> sa.Engine:
        engine = sa.create_engine("sqlite://")
        with engine.begin() as conn:
            for statement in DDL.split(";"):
                if statement.strip():
                    conn.execute(sa.text(statement))
            if seed:
                _seed(conn)
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            # `Operations.context` installs the proxy that the migration
            # file's bare `op` object binds to (the 0055 harness's way).
            _load_migration().upgrade()
        return engine

    return build


def _seed(conn: sa.Connection) -> None:
    oidc_row = uuid.uuid4()
    conn.execute(
        sa.text(
            "INSERT INTO oidc_config (id, join_mode, required_groups, group_mappings, "
            "reason, created_at) VALUES (:id, 'group', :required, :mappings, 'gate', :at)"
        ),
        {
            "id": oidc_row.hex,
            "required": '["staff"]',
            "mappings": '[["acme/staff", "staff"], ["x", "x"]]',
            "at": T0.isoformat(),
        },
    )
    groups = {
        # (a): imported from the IdP, source oidc.
        "eng": uuid.uuid4(),
        # (b): manual, but claims were feeding it (an oidc membership).
        "staff": uuid.uuid4(),
        # Neither: manual and only manual members. No claim ever resolved
        # into it, so the upgrade adds nothing.
        "solo": uuid.uuid4(),
        # Already self-mapped by the row above: skipped.
        "x": uuid.uuid4(),
    }
    for name, group_id in groups.items():
        source = "oidc" if name in ("eng", "x") else "manual"
        conn.execute(
            sa.text(
                "INSERT INTO groups (id, name, source) VALUES (:id, :name, :source)"
            ),
            {"id": group_id.hex, "name": name, "source": source},
        )
    conn.execute(
        sa.text(
            "INSERT INTO memberships (user_id, group_id, source) VALUES (:u, :g, 'oidc')"
        ),
        {"u": uuid.uuid4().hex, "g": groups["staff"].hex},
    )
    conn.execute(
        sa.text(
            "INSERT INTO memberships (user_id, group_id, source) VALUES (:u, :g, 'manual')"
        ),
        {"u": uuid.uuid4().hex, "g": groups["solo"].hex},
    )


def _newest_config(conn: sa.Connection) -> dict[str, object]:
    return dict(
        conn.execute(
            sa.text(
                "SELECT * FROM oidc_config ORDER BY created_at DESC, id LIMIT 1"
            )
        )
        .mappings()
        .one()
    )


def test_the_upgrade_appends_one_row_with_the_missing_self_mappings(
    run_migration,
) -> None:
    engine = run_migration()
    with engine.connect() as conn:
        raw = conn.execute(
            sa.text(
                "SELECT created_at, group_mappings, reason FROM oidc_config "
                "ORDER BY created_at DESC"
            )
        ).all()
        for row in raw:
            print("DEBUG-ROW:", repr(row)[:200])
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT group_mappings, reason, join_mode, required_groups "
                "FROM oidc_config ORDER BY created_at DESC"
            )
        ).all()
        assert len(rows) == 2, "exactly one row appended"
        mappings, reason, join_mode, required = rows[0]  # the new row is newest
        mappings = json.loads(mappings)
        assert reason == REASON
        # The carry-forward: the join answers ride along, or the append would
        # reset them to the environment on the next poll.
        assert join_mode == "group"
        assert required == '["staff"]'
        assert sorted(map(tuple, mappings)) == [
            ("acme/staff", "staff"),
            ("eng", "eng"),
            ("staff", "staff"),
            ("x", "x"),
        ]


def test_the_same_token_resolves_the_same_groups_after_the_upgrade(
    run_migration,
) -> None:
    engine = run_migration()
    with engine.connect() as conn:
        newest = _newest_config(conn)
        ids = dict(conn.execute(sa.text("SELECT name, id FROM groups")).all())
    policy = OIDCPolicy(
        join_mode=str(newest["join_mode"]),
        required_groups=tuple(json.loads(newest["required_groups"] or "[]")),
        group_mappings=dict(json.loads(newest["group_mappings"] or "[]")),
    )
    assert policy.map_claim_group_names(["staff", "eng"]) == ["staff", "eng"]
    assert policy.map_claim_group_names(["solo"]) == []
    # And the names resolve to the very rows that held the memberships.
    assert ids["staff"] and ids["eng"]


def test_an_empty_eligible_set_appends_nothing(run_migration) -> None:
    engine = run_migration(seed=False)
    with engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT id FROM oidc_config")).all()
        assert rows == [], "no groups, no row"
