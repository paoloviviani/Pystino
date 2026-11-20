# ruff: noqa: E501  (fixed SQL built from test constants)
"""Migration 0088: read rules no longer redact.

A read rule that carried policies, a mode or overrides keeps its name, conditions and
action and loses those three; each is logged and audited (actor "system migration"). A
rule without them, and every content rule, is untouched. The downgrade restores nothing and
refuses once something was dropped, unless PYSTINO_ALLOW_LOSSY_DOWNGRADE=1.
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

T0 = "2026-11-19 12:00:00.000000"
POLICY = str(uuid.uuid4())


def _rule(name: str, **over: Any) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "name": name,
        "enabled": True,
        "policies": [],
        "match": "all",
        "conditions": [{"key": "scan", "values": [], "negate": False}],
        "mode": "off",
        "overrides": {},
        "action": {"level": "extract_with", "reader": "docling"},
        **over,
    }


def _setup(tmp_path: Path) -> tuple[Path, str, dict[str, dict[str, Any]]]:
    path = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{path}"
    assert _alembic(url, "upgrade", "0087").returncode == 0
    rules = {
        "redacting": _rule(
            "Redacting", policies=[POLICY], mode="redact", overrides={"URL": "anonymise"}
        ),
        "plain": _rule("Plain"),
    }
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT INTO redaction_policies (id, name, policy, is_active, reason, created_at, updated_at) VALUES (?, 'Adv', '{}', 1, '', ?, ?)",
            (uuid.UUID(POLICY).hex, T0, T0),
        )
        for kind, name, rs in (
            ("extract", "Files", [rules["redacting"], rules["plain"]]),
            ("content", "Chat", [_rule("Hide", policies=[POLICY], mode="redact", action={})]),
        ):
            conn.execute(
                "INSERT INTO privacy_pipelines (id, owner_id, kind, name, enabled, position, who, target, rules, created_at, updated_at) VALUES (?, NULL, ?, ?, 1, 0, ?, ?, ?, ?, ?)",
                (
                    uuid.uuid4().hex,
                    kind,
                    name,
                    json.dumps({"mode": "all", "ids": []}),
                    json.dumps({"mode": "all", "providers": [], "models": [], "model_types": []}),
                    json.dumps(rs),
                    T0,
                    T0,
                ),
            )
        conn.commit()
    return path, url, rules


def _pipelines(path: Path) -> dict[str, list[dict[str, Any]]]:
    with closing(sqlite3.connect(path)) as conn:
        return {
            n: json.loads(r) for n, r in conn.execute("SELECT name, rules FROM privacy_pipelines")
        }


def test_the_redaction_is_dropped_from_read_rules_only_and_each_is_audited(tmp_path: Path) -> None:
    path, url, rules = _setup(tmp_path)
    up = _alembic(url, "upgrade", "0088")
    assert up.returncode == 0, up.stderr[-2000:]
    # Logged, naming the rule and the policy.
    assert "read rules 0088" in up.stderr and "Redacting" in up.stderr and "Adv" in up.stderr
    stored = _pipelines(path)
    redacting, plain = stored["Files"]
    assert (
        redacting["policies"] == [] and redacting["mode"] == "off" and redacting["overrides"] == {}
    )
    # The rest of the rule is kept.
    assert redacting["name"] == "Redacting" and redacting["id"] == rules["redacting"]["id"]
    assert redacting["conditions"] == rules["redacting"]["conditions"]
    assert redacting["action"] == {"level": "extract_with", "reader": "docling"}
    assert plain == rules["plain"]
    # A content rule keeps its redaction.
    assert stored["Chat"][0]["policies"] == [POLICY] and stored["Chat"][0]["mode"] == "redact"
    with closing(sqlite3.connect(path)) as conn:
        audit = conn.execute(
            "SELECT actor_label, action, target_type, target_label, changes FROM admin_audit_events WHERE action = 'privacy_pipeline.read_redaction_dropped'"
        ).fetchall()
    assert len(audit) == 1
    actor, _, target_type, label, changes = audit[0]
    assert (actor, target_type, label) == ("system migration", "privacy_pipeline", "Files")
    detail = json.loads(changes)
    assert detail["rule_id"] == rules["redacting"]["id"]
    assert detail["dropped"] == {
        "policies": [POLICY],
        "mode": "redact",
        "overrides": {"URL": "anonymise"},
        "policy_names": ["Adv"],
    }


def test_the_downgrade_restores_nothing_and_is_refused_unless_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, url, _ = _setup(tmp_path)
    assert _alembic(url, "upgrade", "0088").returncode == 0
    refused = _alembic(url, "downgrade", "0087")
    assert refused.returncode != 0
    assert "PYSTINO_ALLOW_LOSSY_DOWNGRADE=1" in refused.stderr + refused.stdout
    monkeypatch.setenv("PYSTINO_ALLOW_LOSSY_DOWNGRADE", "1")
    allowed = _alembic(url, "downgrade", "0087")
    assert allowed.returncode == 0, allowed.stderr[-1500:]
    assert _pipelines(path)["Files"][0]["policies"] == []


def test_nothing_to_drop_means_no_audit_and_a_clean_downgrade(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{path}"
    assert _alembic(url, "upgrade", "0088").returncode == 0
    assert _alembic(url, "downgrade", "0087").returncode == 0
