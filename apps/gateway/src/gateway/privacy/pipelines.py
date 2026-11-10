"""Pipelines: when and how a kind of request is handled (ADR 0113, section 0; ADR 0114).

A pipeline has a kind (read or content), a scope (who, which target, and for content an
optional model type: chat, embedding, rerank) and an ordered list of rules. Every rule of
every pipeline in scope is evaluated (``gateway.privacy.combine``); there is no first
match and no "everything else".

This module is data and shape: the immutable snapshot the request path holds,
the scope predicate, validation of what an administrator or a user writes, and
the one-line summaries a client shows beside a model choice. There is no I/O
here and no "local": a provider is a provider, and scope is how an
administrator treats one differently.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from gateway.privacy import rules as rulelib
from gateway.privacy.rules import KINDS, Rule, RuleError

if TYPE_CHECKING:
    from gateway.models import PrivacyPipeline

WHO_MODES = ("all", "users", "groups", "keys")
TARGET_MODES = ("all", "only", "except")
_MAX_IDS = 200
_MAX_NAME = 120

#: The scope names a log row stamps, widest first. A pipeline's rank is the
#: narrowest thing it names, and the policies it applies are combined in this
#: order (the order ADR 0038's fold always used: the broadest comes first).
SCOPE_NAMES = ("all", "provider", "model", "group", "user", "api_key")


@dataclass(frozen=True, slots=True)
class Pipeline:
    """An immutable snapshot of one pipeline, as the request path holds it."""

    id: uuid.UUID
    owner_id: uuid.UUID | None
    kind: str
    name: str
    enabled: bool
    position: int
    who_mode: str
    who_ids: frozenset[str]
    target_mode: str
    providers: frozenset[str]
    models: frozenset[str]
    rules: tuple[Rule, ...]
    #: Content only: the model types the pipeline covers; empty is any.
    model_types: frozenset[str] = frozenset()
    #: Stable order among equals (creation order), so a fold is deterministic.
    order: int = 0

    @property
    def is_user(self) -> bool:
        return self.owner_id is not None

    @property
    def scope_rank(self) -> int:
        who = {"all": 0, "groups": 3, "users": 4, "keys": 5}[self.who_mode]
        target = 0 if self.target_mode != "only" else (2 if self.models else 1)
        return max(who, target)

    @property
    def scope_name(self) -> str:
        return SCOPE_NAMES[self.scope_rank]

    def uses_verdicts(self) -> bool:
        return self.enabled and any(
            r.enabled and (r.policies or any(c.key in rulelib.CONTENT_KEYS for c in r.conditions))
            for r in self.rules
        )

    def all_rules(self) -> tuple[Rule, ...]:
        return self.rules


def pipeline_from_row(row: PrivacyPipeline, order: int = 0) -> Pipeline:
    """A stored pipeline as the request path reads it. Raises for one that does not read."""
    if row.kind not in KINDS:
        raise ValueError("unknown kind")
    who = row.who or {}
    target = row.target or {}
    who_mode = str(who.get("mode") or "all")
    target_mode = str(target.get("mode") or "all")
    if who_mode not in WHO_MODES or target_mode not in TARGET_MODES:
        raise ValueError("unknown scope")
    if row.owner_id is not None:
        # A user's pipeline can only ever be about its owner: whatever is stored.
        who_mode, who_ids = "users", frozenset({str(row.owner_id)})
    else:
        who_ids = frozenset(str(i) for i in who.get("ids") or ())
    rules = tuple(
        rulelib.rule_from_json(row.kind, index, raw) for index, raw in enumerate(row.rules or [])
    )
    model_types = frozenset(str(i) for i in target.get("model_types") or ())
    if not model_types <= set(rulelib.MODEL_TYPES) or (
        row.kind == rulelib.KIND_EXTRACT and model_types
    ):
        raise ValueError("unknown model type")
    pipeline = Pipeline(
        id=row.id,
        owner_id=row.owner_id,
        kind=row.kind,
        name=row.name,
        enabled=row.enabled,
        position=row.position,
        who_mode=who_mode,
        who_ids=who_ids,
        target_mode=target_mode,
        providers=frozenset(str(i) for i in target.get("providers") or ()),
        models=frozenset(str(i) for i in target.get("models") or ()),
        rules=rules,
        model_types=model_types,
        order=order,
    )
    # A user's pipeline can never loosen anything, by construction (ADR 0114): administrator
    # pipelines come first, the first rule to hide a span decides how, a block refuses, and
    # the administrator's reader holds. So there is nothing to police here.
    return pipeline


# -- scope -------------------------------------------------------------------------


def not_in_scope(
    pipeline: Pipeline,
    *,
    user_id: str | None,
    group_id: str | None,
    key_id: str | None,
    provider_id: str | None,
    model_id: str | None,
    model_type: str | None = None,
) -> str:
    """Why *pipeline* does not cover this request, or "" when it does."""
    if not pipeline.enabled:
        return "the pipeline is off"
    if pipeline.model_types and model_type is not None and model_type not in pipeline.model_types:
        return "the model type is not covered"
    if pipeline.is_user and (user_id is None or str(pipeline.owner_id) != user_id):
        return "it belongs to another user"
    if pipeline.who_mode == "users" and user_id not in pipeline.who_ids:
        return "not for this user"
    if pipeline.who_mode == "groups" and group_id not in pipeline.who_ids:
        return "not for this group"
    if pipeline.who_mode == "keys" and key_id not in pipeline.who_ids:
        return "not for this key"
    if pipeline.target_mode != "all":
        hit = (provider_id is not None and provider_id in pipeline.providers) or (
            model_id is not None and model_id in pipeline.models
        )
        if pipeline.target_mode == "only" and not hit:
            return "the target is not covered"
        if pipeline.target_mode == "except" and hit:
            return "the target is excluded"
    return ""


# -- validation --------------------------------------------------------------------


def _ids(label: str, raw: Any) -> list[str]:
    if not isinstance(raw, list):
        raise RuleError(f"{label} must be a list.")
    out: list[str] = []
    for item in raw:
        try:
            value = str(uuid.UUID(str(item)))
        except ValueError as exc:
            raise RuleError(f"{label} must be ids.") from exc
        if value not in out:
            out.append(value)
    if len(out) > _MAX_IDS:
        raise RuleError(f"{label} has more than {_MAX_IDS} entries.")
    return out


def validate_scope(
    who: Mapping[str, Any] | None,
    target: Mapping[str, Any] | None,
    *,
    owner_id: uuid.UUID | None,
    kind: str = rulelib.KIND_CONTENT,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The clean who and target, or :class:`RuleError`.

    A user's pipeline has its who fixed to its owner whatever is sent.
    """
    who = who or {}
    target = target or {}
    if owner_id is not None:
        clean_who: dict[str, Any] = {"mode": "users", "ids": [str(owner_id)]}
    else:
        mode = str(who.get("mode") or "all")
        if mode not in WHO_MODES:
            raise RuleError(f"Who must be one of: {', '.join(WHO_MODES)}.")
        ids = _ids("Who", who.get("ids") or []) if mode != "all" else []
        if mode != "all" and not ids:
            raise RuleError("Choose at least one user, group or key, or choose all.")
        clean_who = {"mode": mode, "ids": ids}
    tmode = str(target.get("mode") or "all")
    if tmode not in TARGET_MODES:
        raise RuleError(f"Target must be one of: {', '.join(TARGET_MODES)}.")
    providers = _ids("Providers", target.get("providers") or []) if tmode != "all" else []
    models = _ids("Models", target.get("models") or []) if tmode != "all" else []
    if tmode != "all" and not (providers or models):
        raise RuleError("Choose at least one provider or model, or choose all.")
    types = target.get("model_types") or []
    if not isinstance(types, list) or any(t not in rulelib.MODEL_TYPES for t in types):
        raise RuleError(f"A model type is one of: {', '.join(rulelib.MODEL_TYPES)}.")
    clean_types = [t for t in rulelib.MODEL_TYPES if t in types]
    if clean_types and kind != rulelib.KIND_CONTENT:
        raise RuleError("A model type narrows a content pipeline; an extract pipeline has none.")
    if len(clean_types) == len(rulelib.MODEL_TYPES):
        clean_types = []
    return clean_who, {
        "mode": tmode,
        "providers": providers,
        "models": models,
        "model_types": clean_types,
    }


def validate_pipeline(
    kind: str,
    name: str,
    who: Mapping[str, Any] | None,
    target: Mapping[str, Any] | None,
    rules: Sequence[Mapping[str, Any]],
    *,
    owner_id: uuid.UUID | None = None,
    classifier_ids: Collection[str] | None = None,
    policy_ids: Collection[str] | None = None,
    readers: Collection[str] | None = None,
) -> dict[str, Any]:
    """Everything a pipeline row stores, clean, or :class:`RuleError`."""
    if kind not in KINDS:
        raise RuleError("Choose what the pipeline is for: extract or content.")
    name = (name or "").strip()
    if not name:
        raise RuleError("Give the pipeline a name.")
    if len(name) > _MAX_NAME:
        raise RuleError(f"The name is longer than {_MAX_NAME} characters.")
    if len(rules) > rulelib.MAX_RULES:
        raise RuleError(f"A pipeline has at most {rulelib.MAX_RULES} rules.")
    clean_who, clean_target = validate_scope(who, target, owner_id=owner_id, kind=kind)
    user = owner_id is not None
    clean_rules = [
        rulelib.validate_rule(
            kind,
            raw,
            classifier_ids=classifier_ids,
            policy_ids=policy_ids,
            readers=readers,
            user=user,
        )
        for raw in rules
    ]
    seen: set[str] = set()
    for item in clean_rules:
        lowered = item["name"].lower()
        if lowered in seen:
            raise RuleError(f"Two rules are named '{item['name']}'.")
        seen.add(lowered)
    return {"name": name, "who": clean_who, "target": clean_target, "rules": clean_rules}


# -- words -------------------------------------------------------------------------


def _condition_words(rule: Rule) -> str:
    parts: list[str] = []
    for c in rule.conditions:
        if c.key == "finds":
            many = "an item" if c.count == 1 else f"{c.count} or more items"
            parts.append("it finds " + many if not c.negate else "it does not find " + many)
        elif c.key == "classified":
            parts.append("the text is classified" if not c.negate else "the text is not classified")
        elif c.key == "scan":
            parts.append("scans" if not c.negate else "files that are not scans")
        elif c.key == "filetype":
            parts.append(("not " if c.negate else "") + "/".join(c.values) + " files")
        elif c.key == "filename":
            parts.append(
                ("names not matching " if c.negate else "names matching ") + ", ".join(c.values)
            )
    return (" or " if rule.match == "any" else " and ").join(parts)


_VERB = {
    "off": "",
    "detect_only": "records",
    "anonymise_restore": "hides and restores",
    "anonymise": "hides",
    "redact": "removes",
    "block": "blocks",
}


def summarize(
    pipeline: Pipeline,
    policy_names: Mapping[str, str],
    reader_names: Mapping[str, str] | None = None,
) -> list[str]:
    """What the pipeline does, in short words ("blocks what Policy finds").

    Labels only: never a pattern, an allow-list or another user's anything. Rules that are
    off, or that only record what they find, are left out: they change nothing for the caller.
    """
    words: list[str] = []
    for rule in pipeline.rules:
        if not rule.enabled or rule.is_noop:
            continue
        named = " + ".join(policy_names.get(p, "a policy") for p in rule.policies)
        when = _condition_words(rule)
        suffix = f" when {when}" if when else ""
        if rule.level == rulelib.LEVEL_BLOCK:
            words.append(f"blocks {named}{suffix}" if named else f"blocks{suffix}")
            continue
        if rule.level == rulelib.LEVEL_EXTRACT_WITH:
            reader = rule.action.get("reader")
            label = (reader_names or {}).get(str(reader), str(reader or rulelib.DEFAULT_READER))
            words.append(f"reads{suffix} with {label}")
        verb = _VERB.get(rule.mode, rule.mode)
        if rule.hides_text and rule.mode != "off":
            words.append(f"{verb} {named}{suffix}")
        elif rule.hides_text:
            words.append(f"hides {named} by type{suffix}")
    return list(dict.fromkeys(words))
