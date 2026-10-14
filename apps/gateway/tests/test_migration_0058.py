"""Migration 0058: self-mappings for the groups claims were feeding.

Before the mapping-only rule, any claimed group name equal to a local
group's name landed in it. The rule removes that resolution, so the upgrade
must restate it as what it always should have been spelled as: a self-
mapping (name -> name) for every local group claims were feeding. The test
builds the pre-0058 tables raw, seeds the deployment the upgrade story is
about, runs the migration through Alembic's Operations, and checks the row
it appends: one row, carrying the previous row's answers forward, adding
exactly the missing self-mappings — and none for the groups the rules
exclude (already a mapping key, already a mapping target, required and
manual under a group gate) — and skipping the empty case entirely. The
appended row must also be the one `effective_policy` reads: the fold picks
the newest row, so a timestamp that did not sort would ship a migration
that never took.
"""

from __future__ import annotations

import importlib.util
import json
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from gateway.config import OIDCSettings
from gateway.oidc_policy import OIDCPolicy, effective_policy

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
    code_agents_mode VARCHAR(16),
    api_keys_mode VARCHAR(16),
    reason VARCHAR(500) NOT NULL,
    created_by CHAR(32),
    created_at DATETIME NOT NULL
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

# SQLAlchemy's SQLite DATETIME spells stored values this way; the seed uses
# the same shape so the ORM reads both rows back cleanly.
T0 = "2026-10-01 12:00:00.000000"
REASON = "migration 0058: self-mappings for groups claimed by name"

GROUP_MODE_ROW = {
    "join_mode": "group",
    # staff and vouched are required: the gate's spoof case is live here.
    "required_groups": '["staff", "vouched"]',
    "mappings": '[["acme/vouched", "vouched"], ["keyed", "keyed-access"], ["x", "x"]]',
}


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0058", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def run_migration():
    """A factory for raw SQLite engines with the pre-0058 tables, each with
    the migration's upgrade() run against it; every engine it built is
    disposed when the test ends (the suite fails a run on a ResourceWarning)."""
    engines: list[sa.Engine] = []

    def build(*, seed: bool = True, row: dict[str, str] | None = GROUP_MODE_ROW) -> sa.Engine:
        engine = sa.create_engine("sqlite://")
        engines.append(engine)
        with engine.begin() as conn:
            for statement in DDL.split(";"):
                if statement.strip():
                    conn.execute(sa.text(statement))
            if seed:
                _seed(conn, row)
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            # `Operations.context` installs the proxy that the migration
            # file's bare `op` object binds to (the 0055 harness's way).
            _load_migration().upgrade()
        return engine

    yield build

    for engine in engines:
        engine.dispose()


def _seed(conn: sa.Connection, row: dict[str, str] | None) -> None:
    if row is not None:
        conn.execute(
            sa.text(
                "INSERT INTO oidc_config (id, join_mode, required_groups, group_mappings, "
                "reason, created_at) VALUES (:id, :join_mode, :required, :mappings, 'gate', :at)"
            ),
            {
                "id": uuid.uuid4().hex,
                "join_mode": row["join_mode"],
                "required": row["required_groups"],
                "mappings": row["mappings"],
                "at": T0,
            },
        )
    groups = {
        # (a): imported from the IdP, source oidc, and claims fed it by name
        # (its members' tokens carried "eng" itself). No mapping exists: this
        # is the one group the upgrade must self-map.
        "eng": ("oidc", [("u-eng", "oidc")]),
        # (b): manual, but claims fed it by name under 0.5.0 — except that
        # 0.5.0's group gate dropped same-name claims for REQUIRED names, so
        # with join_mode=group this group was never fed by "staff": skip.
        "staff": ("manual", [("u-staff", "oidc")]),
        # (c): required AND the target of another IdP name's mapping — its
        # memberships came through "acme/vouched", not by name: skip.
        "vouched": ("manual", [("u-vouched", "oidc")]),
        # (d): already a mapping KEY, pointing somewhere else ("keyed" ->
        # "keyed-access"). Appending "keyed" -> "keyed" would sit after the
        # operator's pair and the fold's last-entry-wins would reroute it: skip.
        "keyed": ("oidc", [("u-keyed", "oidc")]),
        # (e): already self-mapped: skip.
        "x": ("oidc", []),
        # Neither eligible: manual and only manual members. No claim ever
        # resolved into it, so the upgrade adds nothing.
        "solo": ("manual", [("u-solo", "manual")]),
    }
    for name, (source, memberships) in groups.items():
        group_id = uuid.uuid4().hex
        conn.execute(
            sa.text("INSERT INTO groups (id, name, source) VALUES (:id, :name, :source)"),
            {"id": group_id, "name": name, "source": source},
        )
        for _, member_source in memberships:
            conn.execute(
                sa.text("INSERT INTO memberships (user_id, group_id, source) VALUES (:u, :g, :s)"),
                {"u": uuid.uuid4().hex, "g": group_id, "s": member_source},
            )


def _newest_config(conn: sa.Connection) -> dict[str, object]:
    return dict(
        conn.execute(sa.text("SELECT * FROM oidc_config ORDER BY created_at DESC, id LIMIT 1"))
        .mappings()
        .one()
    )


def test_the_upgrade_appends_one_row_with_the_missing_self_mappings(run_migration) -> None:
    engine = run_migration()
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT group_mappings, reason, join_mode, required_groups "
                "FROM oidc_config ORDER BY created_at DESC"
            )
        ).all()
        assert len(rows) == 2, "exactly one row appended"
        mappings, reason, join_mode, required = rows[0]  # the new row is newest
        assert reason == REASON
        # The carry-forward: the join answers ride along, or the append would
        # reset them to the environment on the next poll.
        assert join_mode == "group"
        assert required == '["staff", "vouched"]'
        assert sorted(map(tuple, json.loads(mappings))) == [
            ("acme/vouched", "vouched"),
            ("eng", "eng"),
            ("keyed", "keyed-access"),
            ("x", "x"),
        ]


def test_the_appended_row_is_the_one_effective_policy_reads(run_migration) -> None:
    """The fold picks the newest row by `created_at`. The migration writes the
    same UTC datetime the model's column default writes — `sa.func.now()`
    would spell CURRENT_TIMESTAMP on SQLite, which does not sort against the
    ORM's ISO stamps, and the fold would then keep reading the seeded row:
    the migration would have run and changed nothing."""
    from gateway.models import OIDCPolicyConfig
    from sqlalchemy.orm import Session

    engine = run_migration()
    with Session(engine) as session:
        row = session.query(OIDCPolicyConfig).order_by(OIDCPolicyConfig.created_at.desc()).first()
        assert row is not None
        policy = effective_policy(OIDCSettings(enabled=False), row)
        assert policy.config_reason == REASON, "the fold reads the appended row"
        assert policy.join_mode == "group"
        assert policy.required_groups == ("staff", "vouched")
        assert policy.group_mappings.get("eng") == "eng"
        # And what it means for a token: the by-name claim resolves, the
        # excluded shapes do not appear.
        assert policy.map_claim_group_names(["eng"]) == ["eng"]


def test_the_same_token_resolves_the_same_groups_after_the_upgrade(run_migration) -> None:
    engine = run_migration()
    with engine.connect() as conn:
        newest = _newest_config(conn)
        ids = dict(conn.execute(sa.text("SELECT name, id FROM groups")).all())
    policy = OIDCPolicy(
        join_mode=str(newest["join_mode"]),
        required_groups=tuple(json.loads(str(newest["required_groups"] or "[]"))),
        group_mappings=dict(json.loads(str(newest["group_mappings"] or "[]"))),
    )
    assert policy.map_claim_group_names(["eng"]) == ["eng"]
    # The excluded shapes: "staff" (required manual in group mode) and
    # "vouched"/"keyed"/"x" (mapping target, mapping key, self-mapped) claim
    # nothing new — "vouched" reaches its group only through "acme/vouched".
    assert policy.map_claim_group_names(["staff"]) == []
    assert policy.map_claim_group_names(["acme/vouched"]) == ["vouched"]
    assert policy.map_claim_group_names(["keyed-access"]) == []
    assert ids["staff"] and ids["eng"]


def test_an_empty_eligible_set_appends_nothing(run_migration) -> None:
    engine = run_migration(seed=False)
    with engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT id FROM oidc_config")).all()
        assert rows == [], "no groups, no row"


def test_the_environment_group_mode_keeps_a_required_manual_group_out(
    run_migration, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Group mode set from the ENVIRONMENT alone (no console row deciding it —
    and the migrate service carries GATEWAY_OIDC__* since this revision):
    the same skip the row-driven case gets, or the self-mapping would reopen
    the spoof against a required manual group."""
    monkeypatch.setenv("GATEWAY_OIDC__JOIN_MODE", "group")
    monkeypatch.setenv("GATEWAY_OIDC__REQUIRED_GROUPS", "staff")  # the CSV spelling
    engine = run_migration(seed=True, row=None)
    with engine.connect() as conn:
        mappings = json.loads(_newest_config(conn)["group_mappings"])  # type: ignore[arg-type]
        # staff is required and manual under the env's group gate: out. Every
        # other claim-fed group is in — with no row there is no history, so the
        # key/target skips have nothing to read.
        assert sorted(map(tuple, mappings)) == [
            ("eng", "eng"),
            ("keyed", "keyed"),
            ("vouched", "vouched"),
            ("x", "x"),
        ]
        newest = _newest_config(conn)
        assert newest["join_mode"] is None and newest["required_groups"] is None


def test_the_environment_open_mode_self_maps_the_by_name_group(
    run_migration, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same env-only deployment with the environment saying `open`: the
    claims really did resolve by name, so the self-mapping is added — and the
    required-manual skip must not leak into it."""
    monkeypatch.setenv("GATEWAY_OIDC__JOIN_MODE", "open")
    monkeypatch.setenv("GATEWAY_OIDC__REQUIRED_GROUPS", "")
    engine = run_migration(seed=True, row=None)
    with engine.connect() as conn:
        mappings = json.loads(_newest_config(conn)["group_mappings"])  # type: ignore[arg-type]
    # The environment stops gating: every claim-fed group is self-mapped,
    # staff included — the skips that need the row's mappings have no row to
    # read them from.
    assert sorted(map(tuple, mappings)) == [
        ("eng", "eng"),
        ("keyed", "keyed"),
        ("staff", "staff"),
        ("vouched", "vouched"),
        ("x", "x"),
    ]


def test_the_row_decides_over_the_environment(
    run_migration, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fold order is row, then environment: a row that says `open` stands
    even when the environment says `group`, so the by-name self-mappings for
    its groups are added despite GATEWAY_OIDC__JOIN_MODE=group."""
    monkeypatch.setenv("GATEWAY_OIDC__JOIN_MODE", "group")
    monkeypatch.setenv("GATEWAY_OIDC__REQUIRED_GROUPS", '["staff"]')
    # The seeded row decides open; the env required_groups must be ignored
    # with the row's own list in force.
    engine = run_migration(
        row={"join_mode": "open", "required_groups": "[]", "mappings": GROUP_MODE_ROW["mappings"]}
    )
    with engine.connect() as conn:
        mappings = json.loads(_newest_config(conn)["group_mappings"])  # type: ignore[arg-type]
    assert ("staff", "staff") in sorted(map(tuple, mappings))


def test_no_required_manual_group_is_self_mapped_outside_group_mode(run_migration) -> None:
    """The required-and-manual skip is scoped to `group` mode: an open
    deployment's claims really did resolve by name into its manual groups, so
    those keep their self-mapping. (A mapping TARGET stays excluded in every
    mode — its memberships came through the mapping, not by name.) Same seed,
    join_mode open."""
    engine = run_migration(
        row={"join_mode": "open", "required_groups": "[]", "mappings": GROUP_MODE_ROW["mappings"]}
    )
    with engine.connect() as conn:
        mappings = json.loads(_newest_config(conn)["group_mappings"])  # type: ignore[arg-type]
    assert sorted(map(tuple, mappings)) == [
        ("acme/vouched", "vouched"),
        ("eng", "eng"),
        ("keyed", "keyed-access"),
        ("staff", "staff"),
        ("x", "x"),
    ]
