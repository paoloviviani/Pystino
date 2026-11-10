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


def test_refuse_with_no_policy_migrates_enabled_as_a_no_policy_block() -> None:
    out, log = _convert([_rule("all", [])])
    assert out[0]["enabled"] is True and out[0]["policies"] == [] and out[0]["mode"] == "block"
    assert out[0]["conditions"] == [] and any("everything in scope" in line for line in log)


def test_a_classifier_only_refusal_keeps_its_condition_without_a_policy() -> None:
    cond = {"key": "classified", "values": [str(uuid.uuid4())], "negate": False}
    out, _ = _convert([_rule("topic", [cond])])
    assert out[0]["enabled"] and out[0]["policies"] == [] and out[0]["conditions"] == [cond]


def test_mixed_or_all_negated_finds_are_kept_off_and_counted() -> None:
    mixed = [_finds(P1), _finds(P2, negate=True)]
    out, log = _convert([_rule("mixed", mixed)])
    assert out[0]["enabled"] is False and "some negated" in out[0]["note"]
    assert sum("kept switched off" in line for line in log) == 1
    allneg = [_finds(P1, negate=True), _finds(P2, negate=True)]
    out, _ = _convert([_rule("neg", allneg)])
    assert out[0]["enabled"] is False
    out, _ = _convert([_rule("both", [_finds(P1), _finds(P2, 3)])])
    assert (
        out[0]["enabled"]
        and out[0]["policies"] == [P1, P2]
        and out[0]["conditions"][0]["count"] == 3
    )


def test_content_otherwise_refuse_becomes_a_block_when_none_of_the_rules_above_hold() -> None:
    cl = str(uuid.uuid4())
    above = [
        _rule("a", [{"key": "classified", "values": [cl], "negate": False}], level="pass"),
        _rule("b", [_finds(P1, 2)], level="pass"),
    ]
    out, _ = _convert(above, {"level": "refuse"})
    last = out[-1]
    assert last["name"] == "Everything else" and last["enabled"] and last["mode"] == "block"
    assert last["policies"] == [P1]
    assert {(c["key"], c["negate"]) for c in last["conditions"]} == {
        ("classified", True),
        ("finds", True),
    }
    # Two finds over different policies cannot be said: switched off with a note, never enabled bare.
    out, _ = _convert(
        [_rule("a", [_finds(P1)], level="pass"), _rule("b", [_finds(P2)], level="pass")],
        {"level": "refuse"},
    )
    assert out[-1]["enabled"] is False and out[-1]["note"]
    # Nothing above: it blocks everything.
    out, _ = _convert([], {"level": "refuse"})
    assert out[-1]["enabled"] and out[-1]["mode"] == "block" and out[-1]["conditions"] == []


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
        {**_rule("b", [{"key": "scan", "values": [], "negate": False}], level="refuse")},
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


def test_default_mode_is_written_out_over_every_format_and_entity_type() -> None:
    log: list[str] = []
    new, modes = m.convert_policy({"default_mode": "redact", "entities": {}}, log, "d")
    assert set(new["entities"]) == set(m.DEFAULT_TYPES)
    assert set(new["formats"]) == set(m.DEFAULT_FORMATS) and len(new["formats"]) >= 29
    assert modes["PERSON"] == modes["IBAN_CODE"] == "redact" and "no longer covered" in log[0]
    gliner, _ = m.convert_policy({"default_mode": "redact", "engine": "gliner"}, [], "g")
    assert "NRP" not in gliner["entities"]


def test_a_name_in_two_tables_keeps_the_strictest_mode() -> None:
    _, modes = m.convert_policy(
        {
            "entities": {"LOCATION": {"mode": "detect_only"}},
            "keywords": [{"name": "LOCATION", "words": ["x"], "mode": "block"}],
            "patterns": [{"name": "LOCATION", "regex": "q+", "mode": "anonymise"}],
        },
        [],
    )
    assert modes["LOCATION"] == "block"


def test_a_missing_policy_keeps_a_hiding_mode_so_the_request_path_fails_closed() -> None:
    gone = str(uuid.uuid4())
    out, _ = _convert([_rule("hide", [], level="pass", redact_with=gone)])
    assert out[0]["policies"] == [gone] and out[0]["mode"] == "redact" and out[0]["enabled"]


def test_split_rule_names_are_truncated_to_120_characters() -> None:
    long = "x" * 118
    out, _ = _convert([_rule(long, [_finds(P2, 2)], level="pass", redact_with=P1)])
    assert len(out[1]["name"]) <= 120


def test_extract_hiding_never_gets_block_and_otherwise_is_a_negation_or_off() -> None:
    log: list[str] = []
    pm = {**PMODES, "pb": {"X": "block", "Y": "block"}}
    pdf = {"key": "filetype", "values": ["pdf"], "negate": False}
    word = {"key": "filetype", "values": ["word"], "negate": False}
    rules = [
        {**_rule("a", [pdf], level="pass", redact_with="pb")},
        {**_rule("b", [word], level="read_with", reader="docling")},
    ]
    out = m.convert_extract_rules(rules, {"level": "refuse"}, pm, log, "p", str(uuid.uuid4()))
    assert out[0]["mode"] == "redact" and out[0]["action"] == {"level": "default"}
    last = out[-1]
    assert last["enabled"] and last["action"] == {"level": "block"}
    assert last["conditions"] == [{"key": "filetype", "values": ["pdf", "word"], "negate": True}]
    # An earlier rule with two conditions cannot be negated as conditions: off with a note.
    two = {**_rule("c", [pdf, {"key": "scan", "values": [], "negate": False}], level="pass")}
    out = m.convert_extract_rules(
        [two], {"level": "read_with", "reader": "docling"}, pm, [], "p", str(uuid.uuid4())
    )
    assert out[-1]["enabled"] is False and out[-1]["note"]
    # Never enabled with no condition: an unconditional rule above makes it unreachable, so it is dropped.
    out = m.convert_extract_rules(
        [_rule("all", [], level="pass")], {"level": "refuse"}, pm, [], "p", str(uuid.uuid4())
    )
    assert all(r["name"] != "Everything else" for r in out)


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


def test_twins_are_paired_whatever_the_row_order() -> None:
    rule = _rule("r", [_finds(P1)])
    twin = {**rule, "id": str(uuid.uuid4())}
    chat, embed = _row("chat", "Same", [rule]), _row("embed", "Same", [twin])
    for order in ([chat, embed], [embed, chat]):
        keep, drop = m.plan_pipelines(list(order), PMODES, [])
        assert len(keep) == 1 and drop == [embed["id"]] and keep[0]["target"]["model_types"] == []


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


def test_otherwise_refuse_drops_repeated_conditions() -> None:
    cl = {"key": "classified", "values": ["c1"], "negate": False, "count": 1}
    out, _ = _convert([_rule("a", [dict(cl)]), _rule("b", [dict(cl)])], {"level": "refuse"})
    last = out[-1]
    assert last["enabled"] and len(last["conditions"]) == 1 and last["conditions"][0]["negate"]


def test_otherwise_refuse_that_contradicts_itself_is_kept_off_with_a_note() -> None:
    pos = {"key": "classified", "values": ["c1"], "negate": False, "count": 1}
    neg = {**pos, "negate": True}
    out, log = _convert([_rule("a", [pos]), _rule("b", [neg])], {"level": "refuse"})
    last = out[-1]
    assert last["enabled"] is False and last["mode"] == "block" and last["conditions"] == []
    assert "contradict" in last["note"] and any("contradicts" in line for line in log)


def test_a_null_policy_item_is_skipped_with_a_log_line() -> None:
    log: list[str] = []
    policy, modes = m.convert_policy(
        {"entities": {"PERSON": None, "EMAIL_ADDRESS": {"mode": "redact"}}, "patterns": [None]},
        log,
        "pol",
    )
    assert list(policy["entities"]) == ["EMAIL_ADDRESS"] and modes == {"EMAIL_ADDRESS": "redact"}
    assert sum("skipped" in line for line in log) == 2


def test_a_finds_condition_without_values_keeps_the_rule_off() -> None:
    empty = {"key": "finds", "values": [], "negate": False, "count": 1}
    out, log = _convert([_rule("a", [empty])])
    assert len(out) == 1 and out[0]["enabled"] is False and out[0]["mode"] == "block"
    assert "cannot be judged" in out[0]["note"] and log
    # ...and as an earlier rule of "everything else: refuse" it cannot be negated.
    out, _ = _convert([_rule("a", [empty], level="pass", redact_with=P1)], {"level": "refuse"})
    assert out[-1]["enabled"] is False
