# ruff: noqa: S608, E501  (fixed SQL built from test constants)
"""Migration 0083: one detection vocabulary (ADR 0113, section 0c).

Pins what the conversion writes, on SQLite: policies in the two-group shape with the
deployment-default engine, profiles gone, the removed conditions converted and never dropped
silently (``ids`` to a "finds" condition on a detect-only policy; ``health`` and ``lang`` to a
rule that is kept, switched off, with a note), and the policies users already apply shared.
The byte-for-byte proof for requests is ``test_privacy_migration_contract.py``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from gateway.config import EntityMode, RedactionPolicy
from test_migration_0067 import _alembic

T0 = "2026-11-09 12:00:00.000000"


def _old(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{path}"
    assert _alembic(url, "upgrade", "0082").returncode == 0
    return path, url


def _upgrade(url: str) -> Any:
    up = _alembic(url, "upgrade", "0083")
    assert up.returncode == 0, up.stderr[-2000:]
    return up


def _rows(path: Path, table: str) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]


def _policy(conn: sqlite3.Connection, name: str, policy: dict[str, Any]) -> str:
    ident = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO redaction_policies (id, name, policy, is_active, reason, created_at, updated_at) "
        "VALUES (?, ?, ?, 1, '', ?, ?)",
        (ident, name, json.dumps(policy), T0, T0),
    )
    return ident


def _pipeline(
    conn: sqlite3.Connection,
    kind: str,
    name: str,
    rules: list[dict[str, Any]],
    otherwise: dict[str, Any] | None = None,
    owner: uuid.UUID | None = None,
) -> None:
    if owner is not None:
        conn.execute(
            "INSERT INTO users (id, email, display_name, is_admin, is_active, created_at, updated_at) VALUES (?, ?, 'U', 0, 1, ?, ?)",
            (owner.hex, f"{owner.hex[:6]}@x.test", T0, T0),
        ) if False else None
    conn.execute(
        "INSERT INTO privacy_pipelines (id, owner_id, kind, name, enabled, position, who, target, rules, otherwise, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 1, 0, ?, ?, ?, ?, ?, ?)",
        (
            uuid.uuid4().hex,
            owner.hex if owner else None,
            kind,
            name,
            json.dumps({"mode": "all", "ids": []}),
            json.dumps({"mode": "all", "providers": [], "models": []}),
            json.dumps(rules),
            json.dumps(otherwise or {"level": "pass"}),
            T0,
            T0,
        ),
    )


def _rule(name: str, conditions: list[dict[str, Any]], **action: Any) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "name": name,
        "enabled": True,
        "observe_only": False,
        "conditions": conditions,
        "action": action or {"level": "refuse"},
    }


def test_policies_are_split_into_the_two_groups_with_the_default_engine(tmp_path: Path) -> None:
    path, url = _old(tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        ident = _policy(
            conn,
            "Mixed",
            {
                "default_mode": "off",
                "entities": {
                    "PERSON": {"mode": "redact", "threshold": 0.7},
                    "IBAN_CODE": {"mode": "block"},
                    "EMAIL_ADDRESS": {"mode": "anonymise"},
                },
                "patterns": [{"name": "PRJ", "regex": "PRJ-[0-9]+", "mode": "redact"}],
                "allow_list": ["acme.test"],
            },
        )
        conn.commit()
    _upgrade(url)
    stored = json.loads(_rows(path, "redaction_policies")[0]["policy"])
    assert set(stored["entities"]) == {"PERSON"} and set(stored["formats"]) == {
        "IBAN_CODE",
        "EMAIL_ADDRESS",
    }
    assert stored["engine"] is None and stored["keywords"] == [] and stored["custom_entities"] == []
    # Read back, it says what it always said.
    policy = RedactionPolicy.model_validate(stored)
    assert policy.mode_for("IBAN_CODE") is EntityMode.BLOCK
    assert policy.threshold_for("PERSON", 0.5) == 0.7
    assert policy.allow_list == ["acme.test"] and policy.patterns[0].name == "PRJ"
    assert _rows(path, "redaction_policies")[0]["id"] == ident
    assert _rows(path, "redaction_policies")[0]["shared_with_users"] == 0


def test_profiles_and_their_columns_are_gone_and_classifiers_exist_empty(tmp_path: Path) -> None:
    path, url = _old(tmp_path)
    _upgrade(url)
    with closing(sqlite3.connect(path)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "detector_profiles" not in tables and "privacy_classifiers" in tables
        assert list(conn.execute("SELECT * FROM privacy_classifiers")) == []
        for table in ("redaction_policies", "user_policies"):
            assert "profile_id" not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_ids_becomes_a_finds_condition_on_a_detect_only_policy(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path, url = _old(tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        _pipeline(
            conn,
            "embed",
            "Remote",
            [
                _rule(
                    "IDs stay",
                    [{"key": "ids", "values": [], "negate": False, "decide": "patterns"}],
                ),
                _rule(
                    "No IDs, redact", [{"key": "ids", "values": [], "negate": True}], level="pass"
                ),
            ],
        )
        conn.commit()
    _upgrade(url)
    made = [p for p in _rows(path, "redaction_policies") if p["name"] == "Identifiers (migrated)"]
    assert len(made) == 1
    policy = RedactionPolicy.model_validate(json.loads(made[0]["policy"]))
    assert set(policy.formats) == {
        "IBAN_CODE",
        "CREDIT_CARD",
        "IT_FISCAL_CODE",
        "IT_VAT_CODE",
        "UK_NHS",
        "US_SSN",
    }
    assert {e.mode for e in policy.formats.values()} == {EntityMode.DETECT_ONLY}
    assert policy.is_active() is False  # it counts, it never hides
    rules = json.loads(_rows(path, "privacy_pipelines")[0]["rules"])
    assert rules[0]["conditions"] == [
        {"key": "finds", "values": [str(uuid.UUID(made[0]["id"]))], "negate": False, "count": 1}
    ]
    assert rules[1]["conditions"][0]["negate"] is True and rules[1]["enabled"] is True
    assert made[0]["shared_with_users"] == 0


def test_health_and_language_rules_are_kept_switched_off_with_a_note_and_logged(
    tmp_path: Path,
) -> None:
    path, url = _old(tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        _pipeline(
            conn,
            "chat",
            "Remote",
            [
                _rule("Health refused", [{"key": "health", "values": [], "negate": False}]),
                _rule(
                    "Italian health",
                    [
                        {"key": "lang", "values": ["it"], "negate": False},
                        {"key": "health", "values": [], "negate": False},
                    ],
                ),
                _rule(
                    "Not health, pass",
                    [{"key": "health", "values": [], "negate": True}],
                    level="pass",
                ),
                _rule("Fine", []),
            ],
            otherwise={"level": "refuse"},
        )
        conn.commit()
    up = _upgrade(url)
    rules = {r["name"]: r for r in json.loads(_rows(path, "privacy_pipelines")[0]["rules"])}
    # Protective: kept, off, the condition removed (turned on it can only protect more).
    refused = rules["Health refused"]
    assert refused["enabled"] is False and refused["conditions"] == []
    assert refused["action"] == {"level": "refuse"} and refused["observe_only"] is False
    assert "contains health data" in refused["note"] and "switched off" in refused["note"]
    # The other conditions of the rule are kept.
    both = rules["Italian health"]
    assert both["enabled"] is False and both["conditions"] == []
    # A rule that only passed: also observe-only, so turning it on cannot loosen anything.
    passed = rules["Not health, pass"]
    assert passed["enabled"] is False and passed["observe_only"] is True
    assert "does not have: contains health data" in passed["note"]
    # Untouched.
    assert rules["Fine"]["enabled"] is True and "note" not in rules["Fine"]
    # Nothing dropped: every rule is still there, in order.
    assert list(rules) == ["Health refused", "Italian health", "Not health, pass", "Fine"]
    # And the log names each one.
    log = up.stdout + up.stderr
    for name in ("Health refused", "Italian health", "Not health, pass"):
        assert f"rule '{name}'" in log


def test_kept_conditions_of_a_converted_rule_survive_and_decide_is_dropped(tmp_path: Path) -> None:
    path, url = _old(tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        _pipeline(
            conn,
            "read",
            "Files",
            [
                _rule(
                    "Pdf health",
                    [
                        {"key": "filetype", "values": ["pdf"], "negate": False},
                        {"key": "health", "values": [], "negate": False, "decide": "x"},
                    ],
                )
            ],
        )
        conn.commit()
    _upgrade(url)
    (rule,) = json.loads(_rows(path, "privacy_pipelines")[0]["rules"])
    assert rule["conditions"] == [{"key": "filetype", "values": ["pdf"], "negate": False}]
    assert rule["enabled"] is False


def test_policies_users_already_apply_are_shared_so_nothing_stops_working(tmp_path: Path) -> None:
    path, url = _old(tmp_path)
    user = uuid.uuid4()
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        used = _policy(conn, "Used by a user", {"entities": {}})
        unused = _policy(conn, "Not used", {"entities": {}})
        _pipeline(
            conn,
            "chat",
            "Mine",
            [],
            {"level": "pass", "redact_with": str(uuid.UUID(used))},
            owner=user,
        )
        conn.commit()
    _upgrade(url)
    shared = {r["name"]: r["shared_with_users"] for r in _rows(path, "redaction_policies")}
    assert shared == {"Used by a user": 1, "Not used": 0}
    assert unused


def test_a_user_pipeline_with_ids_shares_the_made_policy(tmp_path: Path) -> None:
    path, url = _old(tmp_path)
    user = uuid.uuid4()
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        _pipeline(
            conn,
            "embed",
            "Mine",
            [_rule("IDs", [{"key": "ids", "values": [], "negate": False}])],
            owner=user,
        )
        conn.commit()
    _upgrade(url)
    made = next(
        p for p in _rows(path, "redaction_policies") if p["name"] == "Identifiers (migrated)"
    )
    assert made["shared_with_users"] == 1


def test_a_database_with_nothing_to_convert_is_left_alone(tmp_path: Path) -> None:
    path, url = _old(tmp_path)
    _upgrade(url)
    assert _rows(path, "redaction_policies") == [] and _rows(path, "privacy_pipelines") == []


def test_the_migration_is_quiet_about_what_it_did_not_touch(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    _, url = _old(tmp_path)
    up = _upgrade(url)
    assert "privacy migration 0083" not in up.stdout + up.stderr


def test_a_reading_rule_with_a_content_condition_is_kept_switched_off_with_a_note(
    tmp_path: Path,
) -> None:
    path, url = _old(tmp_path)
    ident = str(uuid.uuid4())
    with closing(sqlite3.connect(path)) as conn:
        _pipeline(
            conn,
            "read",
            "Files",
            [
                _rule("Names", [{"key": "finds", "values": [ident], "negate": False, "count": 2}]),
                _rule(
                    "Topic",
                    [{"key": "classified", "values": [ident], "negate": False}],
                    level="pass",
                ),
                _rule("Scans", [{"key": "scan", "values": [], "negate": False}]),
            ],
        )
        conn.commit()
    up = _upgrade(url)
    rules = {r["name"]: r for r in json.loads(_rows(path, "privacy_pipelines")[0]["rules"])}
    assert list(rules) == ["Names", "Topic", "Scans"]  # nothing dropped
    names = rules["Names"]
    assert names["enabled"] is False and names["conditions"] == []
    assert "a policy finds items" in names["note"] and "switched off" in names["note"]
    topic = rules["Topic"]
    assert topic["enabled"] is False and topic["observe_only"] is True
    assert rules["Scans"]["enabled"] is True and "note" not in rules["Scans"]
    log = up.stdout + up.stderr
    assert "rule 'Names'" in log and "rule 'Topic'" in log


def test_a_rule_reading_with_the_extractors_model_reads_with_the_built_in_extractor(
    tmp_path: Path,
) -> None:
    from gateway.models import ModelDef, ModelKind, Provider
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    path, url = _old(tmp_path)
    engine = create_engine(f"sqlite:///{path}")
    with Session(engine) as db:
        # The migrations seed the extractor's own model ("markitdown"); add another reader.
        other = Provider(name="cloud", base_url="http://ocr.test", plugin=None)
        db.add(other)
        db.flush()
        db.add(
            ModelDef(name="mistral", upstream_model="m", provider_id=other.id, kind=ModelKind.OCR)
        )
        db.commit()
    engine.dispose()
    with closing(sqlite3.connect(path)) as conn:
        _pipeline(
            conn,
            "read",
            "Files",
            [
                {
                    **_rule("Scans", [{"key": "scan", "values": [], "negate": False}]),
                    "action": {"level": "read_with", "reader": "markitdown", "engine": "ocr"},
                },
                {
                    **_rule("Cloud", [{"key": "filetype", "values": ["pdf"], "negate": False}]),
                    "action": {"level": "read_with", "reader": "mistral"},
                },
            ],
            otherwise={"level": "read_with", "reader": "markitdown"},
        )
        conn.commit()
    up = _upgrade(url)
    row = _rows(path, "privacy_pipelines")[0]
    rules = {r["name"]: r["action"] for r in json.loads(row["rules"])}
    assert rules["Scans"] == {"level": "read_with", "reader": "builtin", "engine": "ocr"}
    assert rules["Cloud"]["reader"] == "mistral"  # another OCR model keeps its name
    assert json.loads(row["otherwise"])["reader"] == "builtin"
    assert "reader 'markitdown' is the built-in extractor" in up.stdout + up.stderr
