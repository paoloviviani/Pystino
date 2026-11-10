# ruff: noqa: S608, E501  (fixed SQL built from test constants)
"""Migration 0082: scoped redaction rules, users' own rules and routing rules become policies and pipelines (ADR 0113).

The behaviour of requests after the conversion is exercised through the helpers of ``privacy_helpers.py``; this file
pins the shape of what the conversion writes, on SQLite: a policy keeps its id, scopes become
a who or a target in the order the old fold used, users' rules keep their order, routing rules
keep first-match meaning, and the downgrade restores the older tables.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from test_migration_0067 import _alembic

T0 = "2026-11-08 12:00:00.000000"


def _old(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{path}"
    assert _alembic(url, "upgrade", "0081").returncode == 0
    return path, url


def _upgrade(url: str) -> None:
    up = _alembic(url, "upgrade", "0082")
    assert up.returncode == 0, up.stderr[-2000:]


def _rows(path: Path, table: str) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]


def _rule(conn: sqlite3.Connection, scope: str, subject: uuid.UUID | None, name: str = "") -> str:
    ident = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO redaction_rules (id, name, scope, scope_id, policy, is_active, reason, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 1, '', ?, ?)",
        (
            ident,
            name,
            scope,
            subject.hex if subject else None,
            json.dumps({"entities": {}}),
            T0,
            T0,
        ),
    )
    return ident


def test_every_scoped_rule_becomes_a_policy_with_its_id_and_pipelines_per_kind(
    tmp_path: Path,
) -> None:
    path, url = _old(tmp_path)
    provider, group = uuid.uuid4(), uuid.uuid4()
    with closing(sqlite3.connect(path)) as conn:
        everyone = _rule(conn, "all", None)
        by_provider = _rule(conn, "provider", provider, "Cloud")
        by_group = _rule(conn, "group", group, "Clinic")
        conn.commit()
    _upgrade(url)

    policies = {r["id"]: r for r in _rows(path, "redaction_policies")}
    assert set(policies) == {everyone, by_provider, by_group}
    # A rule saved without a name is called what the log always called it.
    assert policies[everyone]["name"] == "All requests"
    assert policies[by_provider]["name"] == "Cloud"

    pipelines = _rows(path, "privacy_pipelines")
    assert len(pipelines) == 9
    assert {p["kind"] for p in pipelines} == {"read", "embed", "chat"}
    chat = [p for p in pipelines if p["kind"] == "chat"]
    # Broadest scope first: the order the old fold used, which decides the allow-list.
    assert [p["name"] for p in sorted(chat, key=lambda p: p["position"])] == [
        "All requests: chat",
        "Cloud: chat",
        "Clinic: chat",
    ]
    by_name = {p["name"]: p for p in chat}
    assert json.loads(by_name["Cloud: chat"]["target"]) == {
        "mode": "only",
        "providers": [str(provider)],
        "models": [],
    }
    assert json.loads(by_name["Clinic: chat"]["who"]) == {"mode": "groups", "ids": [str(group)]}
    assert json.loads(by_name["All requests: chat"]["otherwise"]) == {
        "level": "pass",
        "redact_with": str(uuid.UUID(everyone)),
    }
    read = next(p for p in pipelines if p["kind"] == "read" and p["name"].startswith("All"))
    assert json.loads(read["otherwise"])["level"] == "default"
    assert all(p["owner_id"] is None and json.loads(p["rules"]) == [] for p in pipelines)


def test_users_own_rules_keep_their_order_in_one_policy_with_three_pipelines(
    tmp_path: Path,
) -> None:
    path, url = _old(tmp_path)
    user = uuid.uuid4()
    with closing(sqlite3.connect(path)) as conn:
        for index, (kind, name, regex, mode) in enumerate(
            [("pattern", "PRJ", r"PRJ-\d+", "redact"), ("mode", "PERSON", None, "block")]
        ):
            conn.execute(
                "INSERT INTO user_redaction_rules (id, user_id, kind, name, regex, mode, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    uuid.uuid4().hex,
                    user.hex,
                    kind,
                    name,
                    regex,
                    mode,
                    f"2026-11-08 12:00:0{index}.000000",
                    T0,
                ),
            )
        conn.commit()
    _upgrade(url)

    [policy] = _rows(path, "user_policies")
    assert [e["name"] for e in json.loads(policy["rules"])] == ["PRJ", "PERSON"]
    pipelines = _rows(path, "privacy_pipelines")
    assert len(pipelines) == 3
    for p in pipelines:
        assert p["owner_id"] == user.hex
        assert json.loads(p["who"]) == {"mode": "users", "ids": [str(user)]}
        assert json.loads(p["otherwise"])["redact_with"] == str(uuid.UUID(policy["id"]))


def test_routing_rules_become_rules_in_pipelines_and_keep_first_match(tmp_path: Path) -> None:
    path, url = _old(tmp_path)
    ids = [uuid.uuid4().hex for _ in range(3)]
    with closing(sqlite3.connect(path)) as conn:
        for index, (kind, name, conditions, action) in enumerate(
            [
                (
                    "read",
                    "Scans and pdfs",
                    [{"key": "filetype", "values": ["pdf", "scan"], "negate": False}],
                    {"type": "reader", "reader": "ocr"},
                ),
                (
                    "embed",
                    "Health or IDs",
                    [
                        {
                            "key": "verdict",
                            "values": ["health", "ids"],
                            "negate": False,
                            "decide": "patterns",
                        }
                    ],
                    {"type": "refuse"},
                ),
                ("embed", "Keep local", [], {"type": "local"}),
            ]
        ):
            conn.execute(
                "INSERT INTO privacy_rules (id, kind, name, enabled, observe_only, position, conditions, action, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, 0, ?, ?, ?, ?, ?)",
                (ids[index], kind, name, index, json.dumps(conditions), json.dumps(action), T0, T0),
            )
        conn.commit()
    _upgrade(url)

    pipelines = {p["name"]: p for p in _rows(path, "privacy_pipelines")}
    read = json.loads(pipelines["Routing rules (read)"]["rules"])
    # "a pdf or a scan" is two adjacent rules with the same action: first-match meaning is kept.
    assert [r["name"] for r in read] == ["Scans and pdfs", "Scans and pdfs (2)"]
    assert read[0]["id"] == str(uuid.UUID(ids[0])) and read[0]["conditions"][0]["key"] == "filetype"
    assert read[0]["conditions"][0]["values"] == ["pdf"]
    assert read[1]["conditions"] == [{"key": "scan", "values": [], "negate": False}]
    assert read[0]["action"] == {"level": "read_with", "reader": "builtin", "engine": "ocr"}
    embed = json.loads(pipelines["Routing rules (embed)"]["rules"])
    assert [r["conditions"][0]["key"] for r in embed[:2]] == ["health", "ids"]
    # `local` failed closed without a local model; with no "local" it refuses.
    assert embed[2]["action"] == {"level": "refuse"}
    assert all(r["conditions"] == [] for r in embed[2:])


def test_a_routing_rule_that_named_a_group_gets_a_pipeline_scoped_to_it(tmp_path: Path) -> None:
    path, url = _old(tmp_path)
    group = uuid.uuid4()
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT INTO groups (id, name, description, is_active, source, created_at, updated_at) "
            "VALUES (?, 'Clinic', '', 1, 'manual', ?, ?)",
            (group.hex, T0, T0),
        )
        conn.execute(
            "INSERT INTO privacy_rules (id, kind, name, enabled, observe_only, position, conditions, action, created_at, updated_at) "
            "VALUES (?, 'embed', 'Clinic only', 1, 0, 0, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex,
                json.dumps([{"key": "who", "values": ["group:Clinic"], "negate": False}]),
                json.dumps({"type": "refuse"}),
                T0,
                T0,
            ),
        )
        conn.commit()
    _upgrade(url)

    [pipeline] = _rows(path, "privacy_pipelines")
    assert pipeline["name"] == "Clinic only"
    assert json.loads(pipeline["who"]) == {"mode": "groups", "ids": [str(group)]}
    assert json.loads(pipeline["rules"])[0]["conditions"] == []


def test_the_old_tables_and_the_local_flag_are_gone_and_the_downgrade_restores_them(
    tmp_path: Path,
) -> None:
    path, url = _old(tmp_path)
    _upgrade(url)
    with closing(sqlite3.connect(path)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {
            "redaction_policies",
            "user_policies",
            "privacy_pipelines",
            "privacy_settings",
        } <= tables
        assert not {"redaction_rules", "user_redaction_rules", "privacy_rules"} & tables
        assert "is_local" not in {r[1] for r in conn.execute("PRAGMA table_info(providers)")}
        assert "user_available" in {
            r[1] for r in conn.execute("PRAGMA table_info(detector_profiles)")
        }

    down = _alembic(url, "downgrade", "0081")
    assert down.returncode == 0, down.stderr[-2000:]
    with closing(sqlite3.connect(path)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"redaction_rules", "user_redaction_rules", "privacy_rules"} <= tables
        assert not {"redaction_policies", "user_policies", "privacy_pipelines"} & tables
        assert "is_local" in {r[1] for r in conn.execute("PRAGMA table_info(providers)")}


def test_an_empty_deployment_migrates_to_nothing(tmp_path: Path) -> None:
    path, url = _old(tmp_path)
    _upgrade(url)
    assert _rows(path, "redaction_policies") == []
    assert _rows(path, "privacy_pipelines") == []
    assert _rows(path, "user_policies") == []


# -- the downgrade writes the data back, or refuses ---------------------------------


def _sorted_old(path: Path) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    with closing(sqlite3.connect(path)) as conn:
        rules = sorted(
            (r[0], r[1], json.loads(r[2]), r[3])
            for r in conn.execute("SELECT scope, scope_id, policy, is_active FROM redaction_rules")
        )
        users = [
            tuple(r)
            for r in conn.execute(
                "SELECT kind, name, regex, mode FROM user_redaction_rules ORDER BY created_at, id"
            )
        ]
    return rules, users


def _old_shape(policy: dict[str, Any]) -> dict[str, Any]:
    """What the older release stored: one ``entities`` table (``formats`` merged back in),
    no engine, custom entities or keywords, and ``detect_only`` written as ``off``."""

    def mode(entry: dict[str, Any]) -> dict[str, Any]:
        return {**entry, "mode": "off"} if entry.get("mode") == "detect_only" else entry

    merged = {**policy.get("formats", {}), **policy.get("entities", {})}
    return {
        "default_mode": policy["default_mode"],
        "patterns": policy["patterns"],
        "allow_list": policy["allow_list"],
        "entities": {k: mode(v) for k, v in merged.items()},
    }


def test_a_downgrade_that_would_lose_pipelines_is_refused_unless_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, url = _old(tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT INTO privacy_rules (id, kind, name, enabled, observe_only, position, conditions, action, created_at, updated_at) "
            "VALUES (?, 'embed', 'R', 1, 0, 0, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex,
                json.dumps([{"key": "verdict", "values": ["health"], "negate": False}]),
                json.dumps({"type": "refuse"}),
                T0,
                T0,
            ),
        )
        conn.commit()
    _upgrade(url)

    refused = _alembic(url, "downgrade", "0081")
    assert refused.returncode != 0
    assert "PYSTINO_ALLOW_LOSSY_DOWNGRADE=1" in refused.stderr + refused.stdout
    assert "Routing rules (embed)" in refused.stderr + refused.stdout
    # Nothing was changed by the refusal.
    assert _rows(path, "privacy_pipelines")

    monkeypatch.setenv("PYSTINO_ALLOW_LOSSY_DOWNGRADE", "1")
    allowed = _alembic(url, "downgrade", "0081")
    assert allowed.returncode == 0, allowed.stderr[-1500:]


def test_converted_rules_that_lost_a_condition_get_their_own_pipeline_and_none_vanish(
    tmp_path: Path,
) -> None:
    path, url = _old(tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        for index, (name, conditions) in enumerate(
            [
                # `local` cannot be said any more: the rule is kept, apart, so it can only add.
                ("Cloud only", [{"key": "target", "values": ["cloud"], "negate": False}]),
                # A verdict with no values asked nothing: it always holds, the rule must not vanish.
                ("Empty verdict", [{"key": "verdict", "values": [], "negate": False}]),
                ("Plain", []),
            ]
        ):
            conn.execute(
                "INSERT INTO privacy_rules (id, kind, name, enabled, observe_only, position, conditions, action, created_at, updated_at) "
                "VALUES (?, 'embed', ?, 1, 0, ?, ?, '{\"type\": \"refuse\"}', ?, ?)",
                (uuid.uuid4().hex, name, index, json.dumps(conditions), T0, T0),
            )
        conn.commit()
    _upgrade(url)

    pipelines = {p["name"]: p for p in _rows(path, "privacy_pipelines")}
    assert "Cloud only" in pipelines
    assert [r["name"] for r in json.loads(pipelines["Cloud only"]["rules"])] == ["Cloud only"]
    shared = json.loads(pipelines["Routing rules (embed)"]["rules"])
    assert [r["name"] for r in shared] == ["Empty verdict", "Plain"]
    assert shared[0]["conditions"] == []


def test_a_policy_with_only_some_kinds_turned_off_is_written_back_active(tmp_path: Path) -> None:
    """The older rule has no per-kind switch: off for one kind must not turn off the others."""
    path, url = _old(tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        _rule(conn, "all", None, "Everyone")
        conn.commit()
    _upgrade(url)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("UPDATE privacy_pipelines SET enabled = 0 WHERE kind = 'embed'")
        conn.commit()
    down = _alembic(url, "downgrade", "0081")
    assert down.returncode == 0, down.stderr[-1500:]
    [row] = _rows(path, "redaction_rules")
    assert row["is_active"] == 1
