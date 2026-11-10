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
#: What the redaction service's own recognisers find without a statistical model
#: (``services/redaction`` ``PATTERN_ONLY_ENTITIES``), and the types its NER engines expose.
#: A policy's old ``default_mode`` ("everything not listed") is written out over these.
DEFAULT_FORMATS = (
    "CREDIT_CARD", "CRYPTO", "DATE_TIME", "EMAIL_ADDRESS", "IBAN_CODE", "IP_ADDRESS",
    "PHONE_NUMBER", "MEDICAL_LICENSE", "URL", "US_BANK_NUMBER", "US_DRIVER_LICENSE", "US_ITIN",
    "US_PASSPORT", "US_SSN", "UK_NHS", "IT_FISCAL_CODE", "IT_DRIVER_LICENSE", "IT_VAT_CODE",
    "IT_PASSPORT", "IT_IDENTITY_CARD", "ES_NIF", "PL_PESEL", "SG_NRIC_FIN", "AU_ABN", "AU_ACN",
    "AU_TFN", "AU_MEDICARE", "IN_PAN", "IN_AADHAAR",
)  # fmt: skip
DEFAULT_TYPES = ("PERSON", "LOCATION", "NRP", "ORGANIZATION")
DEFAULT_TYPES_GLINER = ("PERSON", "LOCATION", "ORGANIZATION")
_NS = uuid.UUID("0a86a086-0000-4000-8000-000000000086")
MAX_NAME = 120


def _stricter(a: str, b: str) -> str:
    return a if MODE_ORDER.index(a) >= MODE_ORDER.index(b) else b


# -- policies ------------------------------------------------------------------------


def convert_policy(
    raw: dict[str, Any], log: list[str], name: str = ""
) -> tuple[dict[str, Any], dict[str, str]]:
    """A stored policy without modes, and the ``{item name: mode}`` of what it acted on.

    Items whose mode was ``off`` are dropped (they found nothing). ``default_mode`` other
    than ``off`` is written out as explicit entries for every format and entity type the
    service knows. A name in two tables keeps the strictest mode.
    """
    policy = json.loads(json.dumps(raw or {}))
    modes: dict[str, str] = {}
    default = str(policy.pop("default_mode", "off") or "off")
    formats = policy.setdefault("formats", {})
    entities = policy.setdefault("entities", {})
    if default != "off":
        ner = DEFAULT_TYPES_GLINER if policy.get("engine") == "gliner" else DEFAULT_TYPES
        have = {k.upper() for k in (*formats, *entities)}
        for t in DEFAULT_FORMATS:
            if t not in have:
                formats[t] = {"mode": default, "placeholder": "tagged", "threshold": None}
        for t in ner:
            if t not in have:
                entities[t] = {"mode": default, "placeholder": "tagged", "threshold": None}
        log.append(
            f"policy '{name}': default mode {default} written out as entries for "
            f"{len(DEFAULT_FORMATS)} formats and {len(ner)} entity types; a type an engine or the "
            "service adds later is no longer covered."
        )

    def note(key: str, mode: str) -> None:
        modes[key] = _stricter(modes[key], mode) if key in modes else mode

    for table in ("formats", "entities"):
        kept: dict[str, Any] = {}
        for key, item in (policy.get(table) or {}).items():
            if not isinstance(item, dict):
                log.append(f"policy '{name}': {table[:-1]} {key} is empty (null); skipped.")
                continue
            mode = str(item.pop("mode", "anonymise_restore"))
            if mode == "off":
                log.append(f"policy '{name}': {table[:-1]} {key} was off and is dropped.")
                continue
            kept[key.upper()] = item
            note(key.upper(), mode)
        policy[table] = kept
    for table in ("custom_entities", "patterns", "keywords"):
        kept_list = []
        for item in policy.get(table) or []:
            if not isinstance(item, dict):
                log.append(f"policy '{name}': an empty (null) entry in {table} was skipped.")
                continue
            mode = str(
                item.pop("mode", "redact" if table != "custom_entities" else "anonymise_restore")
            )
            if mode == "off":
                log.append(f"policy '{name}': {item.get('name')} was off and is dropped.")
                continue
            kept_list.append(item)
            note(str(item.get("name")).upper(), mode)
        policy[table] = kept_list
    return policy, modes


def convert_user_entries(
    entries: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    out, modes = [], {}
    for entry in entries or []:
        entry = dict(entry)
        mode = str(entry.pop("mode", "redact"))
        key = str(entry.get("name")).upper()
        modes[key] = _stricter(modes[key], mode) if key in modes else mode
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
    """How a rule hides with these policies. A policy that is gone keeps a hiding mode, so the
    request path fails closed (503) as it did before, instead of quietly hiding nothing."""
    merged: dict[str, str] = {}
    for pid in pids:
        if pid not in pmodes:
            return "redact", {}
        for name, mode in pmodes[pid].items():
            held = merged.get(name)
            merged[name] = mode if held is None else _stricter(held, mode)
    return mode_and_overrides(merged)


def _uuid(rule_id: str, tag: str) -> str:
    return str(uuid.uuid5(uuid.UUID(str(rule_id)), tag))


def _name(base: str, suffix: str) -> str:
    return f"{base} ({suffix})"[:MAX_NAME]


def negate_earlier(kind: str, earlier: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """"None of the earlier rules matched" as conditions that must all hold, or None.

    Expressible when every earlier rule has exactly one condition: each is negated, and
    conditions of one key are combined (not in A, not in B is one "not in A or B"). A content
    ``finds`` is only expressible over one policy and count, because it counts the rule's own.
    """
    conds: list[dict[str, Any]] = []
    for rule in earlier:
        cs = rule.get("conditions") or []
        if len(cs) != 1:
            return None
        conds.append(cs[0])
    out: list[dict[str, Any]] = []
    by_key: dict[str, list[dict[str, Any]]] = {}
    for c in conds:
        by_key.setdefault(str(c.get("key")), []).append(c)
    for key, group in by_key.items():
        if key == "classified":
            for c in group:
                out.append(
                    {"key": key, "values": list(c["values"]), "negate": not c.get("negate"), "count": 1}
                )
        elif key == "finds":
            if any(not c.get("values") for c in group):
                return None  # a count over no policy cannot be judged
            shapes = {(c["values"][0], int(c.get("count") or 1), bool(c.get("negate"))) for c in group}
            if len(shapes) != 1:
                return None
            pid, count, negate = next(iter(shapes))
            out.append(
                {"key": key, "values": [], "negate": not negate, "count": count, "_policy": pid}
            )
        elif key == "scan":
            if len({bool(c.get("negate")) for c in group}) != 1:
                return None
            out.append({"key": key, "values": [], "negate": not group[0].get("negate")})
        else:  # filetype, filename
            if any(c.get("negate") for c in group):
                if len(group) != 1:
                    return None
                out.append({"key": key, "values": list(group[0]["values"]), "negate": False})
            else:
                values = list(dict.fromkeys(v for c in group for v in c["values"]))
                out.append({"key": key, "values": values, "negate": True})
    return out


def dedupe_conditions(conds: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """The conditions without exact repeats, or None when two of them contradict.

    A condition and its exact negation (same key, values, count and policy) can never both
    hold, so a rule built from them could never fire.
    """
    out: list[dict[str, Any]] = []
    seen: dict[tuple[Any, ...], bool] = {}
    for c in conds:
        ident = (c.get("key"), tuple(c.get("values") or ()), c.get("count"), c.get("_policy"))
        negate = bool(c.get("negate"))
        if ident in seen:
            if seen[ident] != negate:
                return None
            continue
        seen[ident] = negate
        out.append(c)
    return out


def _earlier(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The old rules whose match stopped the search (enabled, not observe-only)."""
    return [r for r in rules if r.get("enabled", True) and not r.get("observe_only")]


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
        mixed = len(finds) > 1 and any(c.get("negate") for c in finds)
        fcond: list[dict[str, Any]] = []
        if len(finds) > 1 and not mixed:
            log.append(
                f"{where}: rule '{rule['name']}' had {len(finds)} 'finds' conditions that all had "
                "to hold; now one count over the union of their policies (it fires at least as often)."
            )
        if finds and not mixed:
            counts = {int(c.get("count") or 1) for c in finds}
            if len(counts) > 1:
                log.append(
                    f"{where}: rule '{rule['name']}': counts {sorted(counts)} merged into {max(counts)}."
                )
            fcond = [
                {"key": "finds", "values": [], "negate": bool(finds[0].get("negate")), "count": max(counts)}
            ]
        why_mixed = (
            f"it had {len(finds)} 'finds' conditions, some negated, that all had to hold; one "
            "count over their policies cannot say that. Rebuild it from the conditions as needed."
        )
        if any(not c.get("values") for c in finds):
            why_empty = "a 'finds' condition named no policy, so it cannot be judged. Rebuild it."
            if level == "refuse":
                out.append(
                    disabled(rule, why_empty, conditions=classified, mode="detect_only" if observe else "block")
                )
            elif redact_with:
                mode, over = ("detect_only", {}) if observe else _hide([redact_with], pmodes)
                out.append(
                    disabled(rule, why_empty, policies=[redact_with], conditions=classified, mode=mode, overrides=over)
                )
            else:
                log.append(f"{where}: rule '{rule['name']}' only passed; dropped (no first match now).")
            continue
        if level == "refuse":
            mode = "detect_only" if observe else "block"
            if mixed:
                out.append(disabled(rule, why_mixed, policies=fids, conditions=classified, mode=mode))
                continue
            if mode == "detect_only" and not (fcond or classified):
                log.append(f"{where}: observe-only rule '{rule['name']}' had nothing to look at; dropped.")
                continue
            if not fids:
                log.append(
                    f"{where}: rule '{rule['name']}' blocked without a policy "
                    + ("on a classifier" if classified else "on everything in scope")
                    + ": migrated as an enabled rule with no policy."
                )
            out.append(new(rule, policies=fids, conditions=[*fcond, *classified], mode=mode))
            continue
        # pass
        if not redact_with:
            log.append(f"{where}: rule '{rule['name']}' only passed; dropped (no first match now).")
            continue
        mode, over = _hide([redact_with], pmodes)
        if observe:
            mode, over = "detect_only", {}
        if mixed:
            out.append(
                disabled(rule, why_mixed, policies=[redact_with], conditions=classified, mode=mode, overrides=over)
            )
            continue
        if not fids or fids == [redact_with]:
            out.append(
                new(rule, policies=[redact_with], conditions=[*fcond, *classified], mode=mode, overrides=over)
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
                name=_name(rule["name"], "hide"),
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
        if rules:
            log.append(f"{where}: 'everything else' hides unconditionally now (it only applied when no rule above did).")
        out.append({**_blank(final), "policies": [str(last_with)], "mode": mode, "overrides": over})
    elif last_level == "refuse":
        earlier = _earlier(rules)
        if any(not (r.get("conditions") or []) for r in earlier):
            log.append(f"{where}: 'everything else: refuse' never applied (a rule above matches everything); dropped.")
        else:
            conds = negate_earlier("content", earlier) if earlier else []
            if conds is not None:
                conds = dedupe_conditions(conds)
                if conds is None:
                    log.append(f"{where}: 'everything else: refuse' contradicts itself; added switched off.")
                    out.append(
                        {**_blank(final), "enabled": False, "mode": "block",
                         "note": "Migration 0086: this pipeline refused whatever no rule above matched, "
                         "but those rules contradict each other, so the block could never fire. "
                         "Write the block you want."}
                    )
                    return out
            if conds is None:
                log.append(f"{where}: 'everything else: refuse' cannot be said as conditions; added switched off.")
                out.append(
                    {**_blank(final), "enabled": False, "mode": "block",
                     "note": "Migration 0086: this pipeline refused whatever no rule above matched, "
                     "which cannot be said as conditions. Write the block you want."}
                )
            else:
                pols = list(dict.fromkeys(c.pop("_policy") for c in conds if "_policy" in c))
                out.append({**_blank(final), "policies": pols, "conditions": conds, "mode": "block"})
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


def _clamp(mode: str, over: dict[str, str]) -> tuple[str, dict[str, str]]:
    """An extract rule blocks with its action: its hiding part never has mode block."""
    return ("redact" if mode == "block" else mode), {
        k: ("redact" if v == "block" else v) for k, v in over.items()
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
        mode, over = _clamp(*_hide(policies, pmodes)) if policies else ("off", {})
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
        mode, over = _clamp(*_hide(lw, pmodes)) if lw else ("off", {})
        final: dict[str, Any] = {
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
        if action_of(last)["level"] != "default":
            # It only applied when no rule above did: say that as "none of them" or keep it off.
            earlier = _earlier(rules)
            if any(not (r.get("conditions") or []) for r in earlier):
                log.append(f"{where}: 'everything else' never applied (a rule above matches everything); dropped.")
                return out
            conds = negate_earlier("extract", earlier) if earlier else []
            if conds is None:
                final["enabled"] = False
                final["note"] = (
                    "Migration 0086: this pipeline's last rule applied only when no rule above did, "
                    "which cannot be said as conditions. Write the conditions you want."
                )
                log.append(f"{where}: 'everything else' kept switched off: it cannot be said as conditions.")
            else:
                final["conditions"] = conds
        out.append(final)
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
        rules = convert_content_rules(list(row.get("rules") or []), row.get("otherwise") or {}, pmodes, log, where)
        target["model_types"] = ["chat"] if kind == "chat" else ["embedding", "rerank"]
        converted.append({**row, "kind": "content", "rules": rules, "target": target, "_old": kind})

    def same(k: dict[str, Any], row: dict[str, Any]) -> bool:
        return (
            k["owner_id"] == row["owner_id"]
            and k["name"].lower() == row["name"].lower()
            and k["who"] == row["who"]
            and k["enabled"] == row["enabled"]
            and {**k["target"], "model_types": []} == {**row["target"], "model_types": []}
            and _signature(k["rules"]) == _signature(row["rules"])
        )

    # Twins are paired explicitly (each chat copy with at most one embed copy), wherever they sit.
    chats = [r for r in converted if r.get("_old") == "chat"]
    paired: set[int] = set()
    drop: list[Any] = []
    for row in converted:
        if row.get("_old") != "embed":
            continue
        twin = next((k for k in chats if id(k) not in paired and same(k, row)), None)
        if twin is not None:
            paired.add(id(twin))
            twin["target"] = {**twin["target"], "model_types": []}
            row["_drop"] = True
            drop.append(row["id"])
            log.append(f"pipeline '{row['name']}': the chat and embed copies were identical and became one.")
    keep = [r for r in converted if not r.get("_drop")]
    # Names stay unique within a kind and owner.
    seen: dict[tuple[Any, str], int] = {}
    for row in keep:
        key = (row["owner_id"], row["kind"] + row["name"].lower())
        seen[key] = seen.get(key, 0) + 1
    for row in keep:
        key = (row["owner_id"], row["kind"] + row["name"].lower())
        if seen[key] > 1 and row["kind"] == "content":
            kinds = row["target"]["model_types"]
            row["name"] = _name(row["name"], " + ".join(kinds) if kinds else "any")
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
