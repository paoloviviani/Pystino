"""Migration 0061: group rules gain ``applies_to`` (aggregate | each_member).

Before it, every group rule capped the group's aggregate usage; after it, a
rule may cap each member instead. The upgrade must not change what is in
force: every existing rule becomes ``aggregate``. The test builds the
pre-0061 table raw, runs the migration through Alembic's Operations, and
checks the backfill, the widened uniqueness (one rule of each kind per
scope, metric and window), and the guardrails — ``each_member`` outside a
group scope, and any other spelling, are refused.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

#: Raw DDL takes strings, not datetimes: the sqlite3 default datetime adapter
#: is deprecated, and a string is all a timestamp column needs here.
T0 = "2026-10-15 12:00:00.000000"

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "0061_limit_rule_applies_to.py"
)

#: limit_rules as 0060 left it: no applies_to.
DDL = """
CREATE TABLE limit_rules (
    id CHAR(32) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    scope VARCHAR(32) NOT NULL,
    scope_id CHAR(32),
    metric VARCHAR(32) NOT NULL,
    window_seconds INTEGER,
    period VARCHAR(32),
    limit_value NUMERIC(24, 12) NOT NULL,
    is_active BOOLEAN NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
);
CREATE UNIQUE INDEX uq_limit_rules_identity ON limit_rules (
    scope,
    coalesce(scope_id, '00000000-0000-0000-0000-000000000000'),
    metric,
    coalesce(window_seconds, -1),
    coalesce(period, '')
);
"""


def _load_migration() -> object:
    spec = importlib.util.spec_from_file_location("migration_0061", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def upgraded() -> sa.Engine:
    """A raw SQLite engine at the pre-0061 shape, with the upgrade run."""
    engine = sa.create_engine("sqlite://")
    group_id = uuid.uuid4().hex
    with engine.begin() as conn:
        for statement in DDL.split(";"):
            if statement.strip():
                conn.execute(sa.text(statement))
        conn.execute(
            sa.text(
                "INSERT INTO limit_rules (id, name, scope, scope_id, metric, "
                "window_seconds, period, limit_value, is_active, created_at, "
                "updated_at) VALUES (:id, 'research budget', 'group', :group, "
                "'cost', NULL, 'month', 500, 1, :at, :at)"
            ),
            {"id": uuid.uuid4().hex, "group": group_id, "at": T0},
        )
    with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
        _load_migration().upgrade()  # type: ignore[no-untyped-call]
    yield engine
    engine.dispose()


def _row(engine: sa.Engine) -> dict:
    with engine.connect() as conn:
        return dict(
            conn.execute(sa.text("SELECT scope, scope_id, applies_to FROM limit_rules"))
            .mappings()
            .one()
        )


def test_existing_rules_become_aggregate(upgraded: sa.Engine) -> None:
    """The upgrade changes nothing in force: the old rule caps the total."""
    assert _row(upgraded)["applies_to"] == "aggregate"


def test_one_rule_of_each_kind_per_scope_metric_and_window(upgraded: sa.Engine) -> None:
    """A group may hold both an aggregate and a per-member rule together."""
    row = _row(upgraded)
    with upgraded.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO limit_rules (id, name, scope, scope_id, metric, "
                "window_seconds, period, limit_value, is_active, created_at, "
                "updated_at, applies_to) VALUES (:id, 'member share', 'group', "
                ":group, 'cost', NULL, 'month', 50, 1, :at, :at, 'each_member')"
            ),
            {"id": uuid.uuid4().hex, "group": row["scope_id"], "at": T0},
        )
        with pytest.raises(IntegrityError):
            conn.execute(
                sa.text(
                    "INSERT INTO limit_rules (id, name, scope, scope_id, metric, "
                    "window_seconds, period, limit_value, is_active, created_at, "
                    "updated_at, applies_to) VALUES (:id, 'member share again', "
                    "'group', :group, 'cost', NULL, 'month', 60, 1, :at, :at, "
                    "'each_member')"
                ),
                {"id": uuid.uuid4().hex, "group": row["scope_id"], "at": T0},
            )


def test_each_member_outside_a_group_scope_is_refused(upgraded: sa.Engine) -> None:
    """Per-member only means something for a group: the database says so, not
    just the API — a writer that bypasses the route still cannot store one."""
    with upgraded.begin() as conn:
        with pytest.raises(IntegrityError):
            conn.execute(
                sa.text(
                    "INSERT INTO limit_rules (id, name, scope, scope_id, metric, "
                    "window_seconds, period, limit_value, is_active, created_at, "
                    "updated_at, applies_to) VALUES (:id, 'personal share', 'user', "
                    ":user, 'cost', NULL, 'month', 50, 1, :at, :at, 'each_member')"
                ),
                {"id": uuid.uuid4().hex, "user": uuid.uuid4().hex, "at": T0},
            )
        with pytest.raises(IntegrityError):
            conn.execute(
                sa.text(
                    "INSERT INTO limit_rules (id, name, scope, scope_id, metric, "
                    "window_seconds, period, limit_value, is_active, created_at, "
                    "updated_at, applies_to) VALUES (:id, 'weird mode', 'group', "
                    ":group, 'cost', NULL, 'month', 50, 1, :at, :at, 'per_person')"
                ),
                {
                    "id": uuid.uuid4().hex,
                    "group": _row(upgraded)["scope_id"],
                    "at": T0,
                },
            )
