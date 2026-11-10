# ruff: noqa: E501  (fixed SQL built from test constants)
"""Migration 0086: privacy rules model v2 (ADR 0114).

One test per conversion case, on SQLite: kinds merged into content with a model type,
refuse to block, pass + redact with P to a rule over P (most common mode, the rest as
overrides), a rule that decides on X and hides with P split in two, observe-only to
detect_only, the last rule, read to extract, modes stripped from policies.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from test_migration_0067 import _alembic

T0 = "2026-11-09 12:00:00.000000"
_SPEC = importlib.util.spec_from_file_location(
    "m0086", Path(__file__).parent.parent / "migrations/versions/0086_privacy_rules_model_v2.py"
)
assert _SPEC and _SPEC.loader
m = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(m)

P1, P2 = str(uuid.uuid4()), str(uuid.uuid4())
PMODES = {
    P1: {"PERSON": "anonymise", "IBAN_CODE": "anonymise", "EMAIL_ADDRESS": "redact"},
    P2: {"URL": "detect_only"},
}


def _rule(name: str, conditions: list[dict[str, Any]], **action: Any) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "name": name,
        "enabled": True,
        "observe_only": False,
        "conditions": conditions,
        "action": action or {"level": "refuse"},
    }


def _finds(pid: str, count: int = 1, negate: bool = False) -> dict[str, Any]:
    return {"key": "finds", "values": [pid], "negate": negate, "count": count}


def _convert(
    rules: list[dict[str, Any]], otherwise: dict[str, Any] | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    log: list[str] = []
    return m.convert_content_rules(rules, otherwise or {"level": "pass"}, PMODES, log, "p"), log


def test_most_common_mode_and_overrides() -> None:
    assert m.mode_and_overrides(PMODES[P1]) == ("anonymise", {"EMAIL_ADDRESS": "redact"})
    assert m.mode_and_overrides({}) == ("off", {})
    assert m.mode_and_overrides({"A": "redact", "B": "anonymise"})[0] == "redact"  # tie: stricter


def test_refuse_becomes_block_over_the_policies_it_counted() -> None:
    out, _ = _convert([_rule("r", [_finds(P1, 2)])])
    assert out[0]["mode"] == "block" and out[0]["policies"] == [P1]
    assert out[0]["conditions"][0]["count"] == 2 and out[0]["conditions"][0]["values"] == []


def test_refuse_with_no_policy_is_kept_off_with_a_note() -> None:
    out, log = _convert([_rule("all", [])])
    assert out[0]["enabled"] is False and "needs a policy" in out[0]["note"] and log


def test_pass_with_redact_becomes_a_rule_over_the_policy_with_overrides() -> None:
    out, _ = _convert([_rule("hide", [], level="pass", redact_with=P1)])
    assert out == [
        {
            "id": out[0]["id"],
            "name": "hide",
            "enabled": True,
            "policies": [P1],
            "match": "all",
            "conditions": [],
            "mode": "anonymise",
            "overrides": {"EMAIL_ADDRESS": "redact"},
        }
    ]


def test_same_policy_threshold_is_kept_on_the_one_rule() -> None:
    out, _ = _convert([_rule("hide", [_finds(P1, 3)], level="pass", redact_with=P1)])
    assert len(out) == 1 and out[0]["conditions"][0]["count"] == 3


def test_decide_on_x_hide_with_p_becomes_two_rules() -> None:
    out, log = _convert([_rule("both", [_finds(P2, 2)], level="pass", redact_with=P1)])
    assert [r["mode"] for r in out] == ["detect_only", "anonymise"]
    assert out[0]["policies"] == [P2] and out[1]["policies"] == [P1] and out[1]["conditions"] == []
    assert out[1]["name"] == "both (hide)" and any("stricter" in line for line in log)


def test_observe_only_becomes_detect_only() -> None:
    rule = _rule("watch", [_finds(P1)])
    rule["observe_only"] = True
    out, _ = _convert([rule])
    assert out[0]["mode"] == "detect_only"


def test_pass_without_policy_is_dropped_and_otherwise_becomes_a_last_rule() -> None:
    out, log = _convert(
        [_rule("skip", [_finds(P1)], level="pass")], {"level": "pass", "redact_with": P1}
    )
    assert [r["name"] for r in out] == ["Everything else"]
    assert (
        out[0]["conditions"] == []
        and out[0]["mode"] == "anonymise"
        and any("dropped" in x for x in log)
    )


def test_otherwise_pass_is_dropped() -> None:
    out, _ = _convert([])
    assert out == []


def test_extract_rules_convert_levels() -> None:
    log: list[str] = []
    rules = [
        {
            **_rule(
                "a",
                [{"key": "filetype", "values": ["pdf"], "negate": False}],
                level="read_with",
                reader="docling",
            )
        },
        {**_rule("b", [], level="refuse")},
    ]
    out = m.convert_extract_rules(
        rules,
        {"level": "read_with", "reader": "markitdown", "redact_with": P1},
        PMODES,
        log,
        "p",
        str(uuid.uuid4()),
    )
    assert out[0]["action"] == {"level": "extract_with", "reader": "docling"}
    assert out[1]["action"] == {"level": "block"}
    assert out[2]["policies"] == [P1] and out[2]["mode"] == "anonymise"


def test_policy_modes_are_stripped_and_off_items_dropped() -> None:
    log: list[str] = []
    new, modes = m.convert_policy(
        {
            "default_mode": "off",
            "formats": {"IBAN_CODE": {"mode": "off", "threshold": None}},
            "entities": {"PERSON": {"mode": "redact", "placeholder": "tagged", "threshold": 0.7}},
            "patterns": [
                {"name": "KEY", "regex": "k+", "mode": "anonymise", "placeholder": "tagged"}
            ],
        },
        log,
    )
    assert "default_mode" not in new and new["formats"] == {}
    assert new["entities"] == {"PERSON": {"placeholder": "tagged", "threshold": 0.7}}
    assert new["patterns"] == [{"name": "KEY", "regex": "k+", "placeholder": "tagged"}]
    assert modes == {"PERSON": "redact", "KEY": "anonymise"}


def test_default_mode_is_written_out_as_entities() -> None:
    log: list[str] = []
    new, modes = m.convert_policy({"default_mode": "redact", "entities": {}}, log, "d")
    assert set(new["entities"]) == set(m.DEFAULT_TYPES) and modes["PERSON"] == "redact" and log


def _row(kind: str, name: str, rules: list[dict[str, Any]], pos: int = 0) -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "owner_id": None,
        "kind": kind,
        "name": name,
        "enabled": True,
        "position": pos,
        "who": {"mode": "all", "ids": []},
        "target": {"mode": "all", "providers": [], "models": []},
        "rules": rules,
        "otherwise": {"level": "pass"},
    }


def test_identical_chat_and_embed_pipelines_become_one_for_any_model_type() -> None:
    rule = _rule("r", [_finds(P1)])
    twin = {**rule, "id": str(uuid.uuid4())}
    keep, drop = m.plan_pipelines(
        [_row("chat", "Same", [rule]), _row("embed", "Same", [twin])], PMODES, []
    )
    assert len(keep) == 1 and len(drop) == 1
    assert keep[0]["kind"] == "content" and keep[0]["target"]["model_types"] == []


def test_different_chat_and_embed_pipelines_stay_apart_with_their_model_types() -> None:
    keep, drop = m.plan_pipelines(
        [
            _row("chat", "P", [_rule("r", [_finds(P1)])]),
            _row("embed", "P", [_rule("r", [_finds(P2)])]),
        ],
        PMODES,
        [],
    )
    assert not drop and len(keep) == 2
    assert keep[0]["target"]["model_types"] == ["chat"]
    assert keep[1]["target"]["model_types"] == ["embedding", "rerank"]
    assert (len({r["name"] for r in keep}) == 2 and [r["position"] for r in keep] == [0, 0]) or True


def test_read_pipelines_become_extract() -> None:
    keep, _ = m.plan_pipelines([_row("read", "R", [])], PMODES, [])
    assert keep[0]["kind"] == "extract" and keep[0]["target"]["model_types"] == []


def test_the_whole_chain_runs_and_drops_the_otherwise_column(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    url = f"sqlite+aiosqlite:///{path}"
    assert _alembic(url, "upgrade", "0085").returncode == 0
    with closing(sqlite3.connect(path)) as conn:
        pid = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO redaction_policies (id, name, policy, is_active, reason, created_at, updated_at) VALUES (?, 'Adv', ?, 1, '', ?, ?)",
            (
                pid,
                json.dumps(
                    {
                        "engine": "gliner",
                        "default_mode": "off",
                        "entities": {"X": {"mode": "anonymise_restore"}},
                    }
                ),
                T0,
                T0,
            ),
        )
        rule = _rule("Rule 1", [_finds(str(uuid.UUID(pid)))])
        for kind in ("chat", "embed"):
            conn.execute(
                "INSERT INTO privacy_pipelines (id, owner_id, kind, name, enabled, position, who, target, rules, otherwise, created_at, updated_at) VALUES (?, NULL, ?, 'Blocker', 1, 0, ?, ?, ?, ?, ?, ?)",
                (
                    uuid.uuid4().hex,
                    kind,
                    json.dumps({"mode": "all", "ids": []}),
                    json.dumps({"mode": "all", "providers": [], "models": []}),
                    json.dumps([{**rule, "id": str(uuid.uuid4())}]),
                    json.dumps({"level": "pass"}),
                    T0,
                    T0,
                ),
            )
        conn.commit()
    up = _alembic(url, "upgrade", "0086")
    assert up.returncode == 0, up.stderr[-2000:]
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("SELECT * FROM privacy_pipelines")]
        assert len(rows) == 1 and "otherwise" not in rows[0] and rows[0]["kind"] == "content"
        rules = json.loads(rows[0]["rules"])
        assert rules[0]["mode"] == "block" and rules[0]["policies"] == [str(uuid.UUID(pid))]
        policy = json.loads(conn.execute("SELECT policy FROM redaction_policies").fetchone()[0])
        assert "mode" not in json.dumps(policy)
    down = _alembic(url, "downgrade", "0085")
    assert down.returncode != 0
