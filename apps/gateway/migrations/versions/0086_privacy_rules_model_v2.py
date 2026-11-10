"""Privacy rules model v2: two kinds, rules with policies and a mode (ADR 0114).

Revision ID: 0086
Revises: 0085
Create Date: 2026-11-10

* **Kinds.** ``chat`` and ``embed`` become ``content`` (a model type filter says which
  models: chat, or embedding + rerank), ``read`` becomes ``extract``. A chat and an embed
  pipeline with the same owner, name, scope, enabled flag and converted rules become one
  pipeline for any model type; otherwise each is converted on its own (a name clash gets a
  suffix).
* **Rules.** "conditions -> level (+ redact with P)" becomes "When policies + conditions ->
  mode". ``refuse`` is ``block``; ``pass`` + ``redact with P`` is a rule over P whose mode is
  P's most common item mode, the others overrides; ``observe_only`` is ``detect_only``;
  ``otherwise`` with a policy is a final rule with no condition, ``otherwise: pass`` is
  dropped. Every lossy conversion is logged (``privacy rules v2 0086: ...``) and listed in
  ADR 0114; a rule that cannot be said any more is kept **switched off with a note**.
* **Policies and user policies** lose ``mode`` / ``default_mode`` after they are read.
  An item that was ``off`` is dropped (it found nothing, and now an item found is an item
  counted); ``default_mode`` becomes explicit entries for the NER types every engine has.
* The ``otherwise`` column is dropped.

Irreversible: merged pipelines and moved modes cannot be told apart again, so the downgrade
refuses rather than invent an older shape.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import Counter
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0086"
down_revision: str | None = "0085"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

MODE_ORDER = ["off", "detect_only", "anonymise_restore", "anonymise", "redact", "block"]
#: What every engine finds; a policy's old ``default_mode`` becomes these, explicitly.
DEFAULT_TYPES = ("PERSON", "LOCATION", "NRP", "ORGANIZATION")
DEFAULT_TYPES_GLINER = ("PERSON", "LOCATION", "ORGANIZATION")
_NS = uuid.UUID("0a86a086-0000-4000-8000-000000000086")


# -- policies ------------------------------------------------------------------------


def convert_policy(raw: dict[str, Any], log: list[str], name: str = "") -> tuple[dict[str, Any], dict[str, str]]:
    """A stored policy without modes, and the ``{item name: mode}`` of what it acted on.

    Items whose mode was ``off`` are dropped (they found nothing). ``default_mode`` other
    than ``off`` is written out as explicit entity entries.
    """
    policy = json.loads(json.dumps(raw or {}))
    modes: dict[str, str] = {}
    default = str(policy.pop("default_mode", "off") or "off")
    entities = policy.setdefault("entities", {})
    if default != "off":
        engine = policy.get("engine")
        wanted = DEFAULT_TYPES_GLINER if engine == "gliner" else DEFAULT_TYPES
        for t in wanted:
            if t not in {k.upper() for k in entities}:
                entities[t] = {"mode": default, "placeholder": "tagged", "threshold": None}
        log.append(
            f"policy '{name}': default mode {default} written out as entries for "
            f"{', '.join(wanted)}; other types an engine adds later are no longer covered."
        )
    for table in ("formats", "entities"):
        kept: dict[str, Any] = {}
        for key, item in (policy.get(table) or {}).items():
            mode = str(item.pop("mode", "anonymise_restore"))
            if mode == "off":
                log.append(f"policy '{name}': {table[:-1]} {key} was off and is dropped.")
                continue
            kept[key.upper()] = item
            modes[key.upper()] = mode
        policy[table] = kept
    for table in ("custom_entities", "patterns", "keywords"):
        kept_list = []
        for item in policy.get(table) or []:
            mode = str(item.pop("mode", "redact" if table != "custom_entities" else "anonymise_restore"))
            if mode == "off":
                log.append(f"policy '{name}': {item.get('name')} was off and is dropped.")
                continue
            kept_list.append(item)
            modes[str(item.get("name")).upper()] = mode
        policy[table] = kept_list
    return policy, modes


def convert_user_entries(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    out, modes = [], {}
    for entry in entries or []:
        entry = dict(entry)
        mode = str(entry.pop("mode", "redact"))
        modes[str(entry.get("name")).upper()] = mode
        out.append(entry)
    return out, modes


def mode_and_overrides(modes: dict[str, str]) -> tuple[str, dict[str, str]]:
    """The most common mode (the stricter on a tie) and the rest as per-type overrides."""
    if not modes:
        return "off", {}
    counts = Counter(modes.values())
    best = max(counts, key=lambda m: (counts[m], MODE_ORDER.index(m)))
    return best, {k: v for k, v in sorted(modes.items()) if v != best}


# -- rules ---------------------------------------------------------------------------


def _hide(pids: list[str], pmodes: dict[str, dict[str, str]]) -> tuple[str, dict[str, str]]:
    merged: dict[str, str] = {}
    for pid in pids:
        for name, mode in pmodes.get(pid, {}).items():
            held = merged.get(name)
            if held is None or MODE_ORDER.index(mode) > MODE_ORDER.index(held):
                merged[name] = mode
    return mode_and_overrides(merged)


def _uuid(rule_id: str, tag: str) -> str:
    return str(uuid.uuid5(uuid.UUID(str(rule_id)), tag))


def convert_content_rules(
    rules: list[dict[str, Any]],
    otherwise: dict[str, Any],
    pmodes: dict[str, dict[str, str]],
    log: list[str],
    where: str,
) -> list[dict[str, Any]]:
    """The old chat/embed rules (and last rule) as new content rules."""
    out: list[dict[str, Any]] = []

    def new(rule: dict[str, Any], **kw: Any) -> dict[str, Any]:
        base = {
            "id": rule["id"],
            "name": rule["name"],
            "enabled": bool(rule.get("enabled", True)),
            "policies": [],
            "match": "all",
            "conditions": [],
            "mode": "off",
            "overrides": {},
        }
        if rule.get("note"):
            base["note"] = rule["note"]
        base.update(kw)
        return base

    def disabled(rule: dict[str, Any], why: str, **kw: Any) -> dict[str, Any]:
        log.append(f"{where}: rule '{rule['name']}' kept switched off: {why}")
        r = new(rule, **kw)
        r["enabled"] = False
        r["note"] = f"Migration 0086: {why}"
        return r

    for rule in rules:
        conds = rule.get("conditions") or []
        finds = [c for c in conds if c.get("key") == "finds"]
        classified = [dict(c) for c in conds if c.get("key") == "classified"]
        fids = list(dict.fromkeys(str(c["values"][0]) for c in finds if c.get("values")))
        action = rule.get("action") or {}
        level = action.get("level", "pass")
        redact_with = str(action["redact_with"]) if action.get("redact_with") else None
        observe = bool(rule.get("observe_only"))
        if len(finds) > 1:
            log.append(
                f"{where}: rule '{rule['name']}' had {len(finds)} 'finds' conditions that all had "
                "to hold; now one count over the union of their policies (it fires at least as often)."
            )
        fcond: list[dict[str, Any]] = []
        if finds:
            c0 = finds[0]
            counts = {int(c.get("count") or 1) for c in finds}
            if len(counts) > 1:
                log.append(f"{where}: rule '{rule['name']}': counts {sorted(counts)} merged into {max(counts)}.")
            fcond = [
                {
                    "key": "finds",
                    "values": [],
                    "negate": bool(c0.get("negate")),
                    "count": max(counts),
                }
            ]
        if level == "refuse":
            policies = fids
            mode = "detect_only" if observe else "block"
            if not policies:
                out.append(
                    disabled(
                        rule,
                        "it blocked on a classifier or on everything, and a rule now needs a "
                        "policy to look for. Choose one and turn it on.",
                        conditions=classified,
                        mode=mode,
                    )
                )
                continue
            out.append(new(rule, policies=policies, conditions=[*fcond, *classified], mode=mode))
            continue
        # pass
        if not redact_with:
            log.append(f"{where}: rule '{rule['name']}' only passed; dropped (no first match now).")
            continue
        mode, over = _hide([redact_with], pmodes)
        if observe:
            mode, over = "detect_only", {}
        if not fids or fids == [redact_with]:
            out.append(
                new(
                    rule,
                    policies=[redact_with],
                    conditions=[*fcond, *classified],
                    mode=mode,
                    overrides=over,
                )
            )
            continue
        # Decides on X, hides with a different P: two rules in sequence.
        log.append(
            f"{where}: rule '{rule['name']}' decided on other policies than the one it hid with; "
            "now the first only records and the second hides unconditionally"
            + (" on the same classifier" if classified else "")
            + " (stricter than before)."
        )
        if not any(c.get("negate") for c in finds):
            out.append(new(rule, policies=fids, conditions=fcond, mode="detect_only"))
        out.append(
            new(
                rule,
                id=_uuid(rule["id"], "hide"),
                name=f"{rule['name']} (hide)",
                policies=[redact_with],
                conditions=classified,
                mode=mode,
                overrides=over,
            )
        )
    last_level = (otherwise or {}).get("level", "pass")
    last_with = (otherwise or {}).get("redact_with")
    final = {"id": str(uuid.uuid5(_NS, where + "|otherwise")), "name": "Everything else"}
    if last_with:
        mode, over = _hide([str(last_with)], pmodes)
        out.append({**_blank(final), "policies": [str(last_with)], "mode": mode, "overrides": over})
    elif last_level == "refuse":
        log.append(f"{where}: 'everything else: refuse' cannot be said without a policy; added switched off.")
        out.append(
            {
                **_blank(final),
                "enabled": False,
                "mode": "block",
                "note": "Migration 0086: this pipeline refused everything else; choose a policy to block on.",
            }
        )
    return out


def _blank(base: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": True,
        "policies": [],
        "match": "all",
        "conditions": [],
        "mode": "off",
        "overrides": {},
        **base,
    }


def convert_extract_rules(
    rules: list[dict[str, Any]],
    otherwise: dict[str, Any],
    pmodes: dict[str, dict[str, str]],
    log: list[str],
    where: str,
    pipeline_id: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def action_of(a: dict[str, Any]) -> dict[str, Any]:
        level = {"read_with": "extract_with", "refuse": "block"}.get(a.get("level", "default"), "default")
        act: dict[str, Any] = {"level": level}
        if level == "extract_with":
            act["reader"] = a.get("reader") or "markitdown"
        return act

    for rule in rules:
        a = rule.get("action") or {}
        policies = [str(a["redact_with"])] if a.get("redact_with") else []
        mode, over = _hide(policies, pmodes) if policies else ("off", {})
        r: dict[str, Any] = {
            "id": rule["id"],
            "name": rule["name"],
            "enabled": bool(rule.get("enabled", True)),
            "policies": policies,
            "match": "all",
            "conditions": rule.get("conditions") or [],
            "mode": mode,
            "overrides": over,
            "action": action_of(a),
        }
        if rule.get("note"):
            r["note"] = rule["note"]
        if rule.get("observe_only"):
            r["action"] = {"level": "default"}
            r["mode"], r["overrides"] = ("detect_only" if policies else "off"), {}
            r["note"] = "Migration 0086: this rule was observe-only; it now records and does nothing else."
            log.append(f"{where}: observe-only rule '{rule['name']}' kept as a rule that only records.")
        out.append(r)
    last = otherwise or {}
    lw = [str(last["redact_with"])] if last.get("redact_with") else []
    lvl = last.get("level", "default")
    if lw or lvl not in ("default", "pass"):
        mode, over = _hide(lw, pmodes) if lw else ("off", {})
        out.append(
            {
                "id": _uuid(pipeline_id, "otherwise"),
                "name": "Everything else",
                "enabled": True,
                "policies": lw,
                "match": "all",
                "conditions": [],
                "mode": mode,
                "overrides": over,
                "action": action_of(last),
            }
        )
    return out


def _signature(rules: list[dict[str, Any]]) -> str:
    def strip(r: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in r.items() if k not in ("id", "note")}

    return json.dumps([strip(r) for r in rules], sort_keys=True)


def plan_pipelines(
    rows: list[dict[str, Any]], pmodes: dict[str, dict[str, str]], log: list[str]
) -> tuple[list[dict[str, Any]], list[Any]]:
    """The pipelines as they will be stored, and the ids to delete (the merged embed ones).

    Each input row: id, owner_id, kind, name, enabled, position, who, target, rules, otherwise.
    """
    converted: list[dict[str, Any]] = []
    for row in rows:
        kind = row["kind"]
        where = f"pipeline '{row['name']}' ({kind})"
        target = dict(row.get("target") or {})
        if kind == "read":
            rules = convert_extract_rules(
                list(row.get("rules") or []), row.get("otherwise") or {}, pmodes, log, where, str(row["id"])
            )
            target["model_types"] = []
            converted.append({**row, "kind": "extract", "rules": rules, "target": target})
            continue
        rules = convert_content_rules(
            list(row.get("rules") or []), row.get("otherwise") or {}, pmodes, log, where
        )
        # Positions of the old per-kind lists interleave: chat first on a tie.
        target["model_types"] = ["chat"] if kind == "chat" else ["embedding", "rerank"]
        converted.append(
            {**row, "kind": "content", "rules": rules, "target": target, "_old": kind}
        )
    drop: list[Any] = []
    keep: list[dict[str, Any]] = []
    for row in converted:
        if row["kind"] == "extract":
            keep.append(row)
            continue
        twin = next(
            (
                k
                for k in keep
                if k["kind"] == "content"
                and k["owner_id"] == row["owner_id"]
                and k["_old"] == "chat"
                and row["_old"] == "embed"
                and k["name"].lower() == row["name"].lower()
                and k["who"] == row["who"]
                and k["enabled"] == row["enabled"]
                and {**k["target"], "model_types": []} == {**row["target"], "model_types": []}
                and _signature(k["rules"]) == _signature(row["rules"])
            ),
            None,
        )
        if twin is not None:
            twin["target"] = {**twin["target"], "model_types": []}
            drop.append(row["id"])
            log.append(f"pipeline '{row['name']}': the chat and embed copies were identical and became one.")
            continue
        keep.append(row)
    # Names stay unique within a kind and owner.
    seen: dict[tuple[Any, str], int] = {}
    for row in keep:
        key = (row["owner_id"], row["kind"] + row["name"].lower())
        seen[key] = seen.get(key, 0) + 1
    for row in keep:
        key = (row["owner_id"], row["kind"] + row["name"].lower())
        if seen[key] > 1 and row["kind"] == "content":
            kinds = row["target"]["model_types"]
            row["name"] = f"{row['name']} ({' + '.join(kinds) if kinds else 'any'})"[:120]
    # Renumber positions per owner and kind (chat first on a tie).
    groups: dict[tuple[Any, str], list[dict[str, Any]]] = {}
    for row in keep:
        groups.setdefault((row["owner_id"], row["kind"]), []).append(row)
    for members in groups.values():
        members.sort(key=lambda r: (r.get("position") or 0, 0 if r.get("_old") != "embed" else 1))
        for index, row in enumerate(members):
            row["position"] = index
    return keep, drop


# -- the migration -------------------------------------------------------------------


def _tables() -> dict[str, sa.Table]:
    return {
        "privacy_pipelines": sa.table(
            "privacy_pipelines",
            sa.column("id", sa.Uuid()),
            sa.column("owner_id", sa.Uuid()),
            sa.column("kind", sa.String()),
            sa.column("name", sa.String()),
            sa.column("enabled", sa.Boolean()),
            sa.column("position", sa.Integer()),
            sa.column("who", sa.JSON()),
            sa.column("target", sa.JSON()),
            sa.column("rules", sa.JSON()),
            sa.column("otherwise", sa.JSON()),
        ),
        "redaction_policies": sa.table(
            "redaction_policies",
            sa.column("id", sa.Uuid()),
            sa.column("name", sa.String()),
            sa.column("policy", sa.JSON()),
        ),
        "user_policies": sa.table(
            "user_policies",
            sa.column("id", sa.Uuid()),
            sa.column("rules", sa.JSON()),
        ),
    }


def upgrade() -> None:
    bind = op.get_bind()
    t = _tables()
    log: list[str] = []
    pmodes: dict[str, dict[str, str]] = {}
    pols = t["redaction_policies"]
    for row in bind.execute(sa.select(pols.c.id, pols.c.name, pols.c.policy)).all():
        new, modes = convert_policy(dict(row.policy or {}), log, row.name)
        pmodes[str(row.id)] = modes
        bind.execute(pols.update().where(pols.c.id == row.id).values(policy=new))
    ups = t["user_policies"]
    for row in bind.execute(sa.select(ups.c.id, ups.c.rules)).all():
        entries, modes = convert_user_entries(list(row.rules or []))
        pmodes[str(row.id)] = modes
        bind.execute(ups.update().where(ups.c.id == row.id).values(rules=entries))
    pipes = t["privacy_pipelines"]
    rows = [
        {
            "id": r.id,
            "owner_id": r.owner_id,
            "kind": r.kind,
            "name": r.name,
            "enabled": r.enabled,
            "position": r.position,
            "who": r.who,
            "target": r.target,
            "rules": r.rules,
            "otherwise": r.otherwise,
        }
        for r in bind.execute(sa.select(*pipes.c)).all()
    ]
    keep, drop = plan_pipelines(rows, pmodes, log)
    for row in keep:
        bind.execute(
            pipes.update()
            .where(pipes.c.id == row["id"])
            .values(
                kind=row["kind"],
                name=row["name"],
                position=row["position"],
                target=row["target"],
                rules=row["rules"],
            )
        )
    for ident in drop:
        bind.execute(pipes.delete().where(pipes.c.id == ident))
    with op.batch_alter_table("privacy_pipelines") as batch:
        batch.drop_column("otherwise")
    for line in log:
        logger.warning("privacy rules v2 0086: %s", line)


def downgrade() -> None:
    raise NotImplementedError(
        "Migration 0086 cannot be reversed: chat and embed pipelines were merged, per-item "
        "modes moved from policies to rules and first-match order was dropped, so the older "
        "shape cannot be rebuilt. Restore the database backup taken before upgrading."
    )
