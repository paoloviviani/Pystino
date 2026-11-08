"""Pipelines: when and how a kind of request is handled (ADR 0113, section 0).

A pipeline has a kind (read, embed, chat), a scope (who, which target), an
ordered list of rules where the first match wins, and a fixed last rule,
"everything else". A request meets only the pipelines whose scope covers it,
and **every** pipeline in scope runs (``gateway.privacy.combine``).

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
    otherwise: Rule
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
            r.enabled and any(c.key in rulelib.CONTENT_KEYS for c in r.conditions)
            for r in self.rules
        )

    def all_rules(self) -> tuple[Rule, ...]:
        return (*self.rules, self.otherwise)


def otherwise_rule(pipeline_id: uuid.UUID, kind: str, action: Mapping[str, Any]) -> Rule:
    return Rule(
        id=uuid.uuid5(pipeline_id, "otherwise"),
        kind=kind,
        name="Everything else",
        enabled=True,
        observe_only=False,
        position=10_000,
        conditions=(),
        action=dict(action) if action else {"level": rulelib.base_level(kind)},
    )


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
    otherwise = otherwise_rule(row.id, row.kind, row.otherwise or {})
    if otherwise.level not in rulelib.LADDER[row.kind]:
        raise ValueError("the last rule has no valid action")
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
        otherwise=otherwise,
        order=order,
    )
    if pipeline.is_user and not tightens_only(pipeline):
        # Enforced when it is read as well as when it is saved: a row edited by
        # hand into something that loosens is not served.
        raise ValueError("a user's pipeline may only tighten")
    return pipeline


def tightens_only(pipeline: Pipeline) -> bool:
    """Whether every action of a (user's) pipeline adds protection or adds nothing.

    The ladder is monotone and the combination takes the highest level, so a
    user's pipeline cannot lower anything. The only thing left to refuse is a
    user's *rule* whose whole point is to add nothing: harmless, but refused on
    save so a list does not fill with sentences that do nothing.
    """
    return all(rule.level in rulelib.LADDER[pipeline.kind] for rule in pipeline.all_rules())


# -- scope -------------------------------------------------------------------------


def not_in_scope(
    pipeline: Pipeline,
    *,
    user_id: str | None,
    group_id: str | None,
    key_id: str | None,
    provider_id: str | None,
    model_id: str | None,
) -> str:
    """Why *pipeline* does not cover this request, or "" when it does."""
    if not pipeline.enabled:
        return "the pipeline is off"
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
    return clean_who, {"mode": tmode, "providers": providers, "models": models}


def validate_pipeline(
    kind: str,
    name: str,
    who: Mapping[str, Any] | None,
    target: Mapping[str, Any] | None,
    rules: Sequence[Mapping[str, Any]],
    otherwise: Mapping[str, Any] | None,
    *,
    owner_id: uuid.UUID | None = None,
    profile_ids: Collection[str] = (),
    policy_ids: Collection[str] | None = None,
    readers: Collection[str] | None = None,
) -> dict[str, Any]:
    """Everything a pipeline row stores, clean, or :class:`RuleError`."""
    if kind not in KINDS:
        raise RuleError("Choose what the pipeline is for: reading, embedding or chat.")
    name = (name or "").strip()
    if not name:
        raise RuleError("Give the pipeline a name.")
    if len(name) > _MAX_NAME:
        raise RuleError(f"The name is longer than {_MAX_NAME} characters.")
    if len(rules) > rulelib.MAX_RULES:
        raise RuleError(f"A pipeline has at most {rulelib.MAX_RULES} rules.")
    clean_who, clean_target = validate_scope(who, target, owner_id=owner_id)
    user = owner_id is not None
    clean_rules = [
        rulelib.validate_rule(
            kind,
            raw,
            profile_ids=profile_ids,
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
    last = rulelib.clean_action(
        kind,
        otherwise or {"level": rulelib.base_level(kind)},
        policy_ids=policy_ids,
        readers=readers,
        user=user,
        otherwise=True,
    )
    return {
        "name": name,
        "who": clean_who,
        "target": clean_target,
        "rules": clean_rules,
        "otherwise": last,
    }


# -- words -------------------------------------------------------------------------

_LANG_NAMES = {"it": "Italian", "en": "English", "de": "German", "fr": "French", "es": "Spanish"}


def _condition_words(rule: Rule) -> str:
    parts: list[str] = []
    for c in rule.conditions:
        if c.key == "health":
            parts.append("health data" if not c.negate else "text with no health data")
        elif c.key == "ids":
            parts.append("IDs" if not c.negate else "text with no IDs")
        elif c.key == "lang":
            langs = " or ".join(_LANG_NAMES.get(v, v) for v in c.values)
            parts.append(f"{langs} text" if not c.negate else f"text not in {langs}")
        elif c.key == "scan":
            parts.append("scans" if not c.negate else "files that are not scans")
        elif c.key == "filetype":
            parts.append(("not " if c.negate else "") + "/".join(c.values) + " files")
        elif c.key == "filename":
            parts.append(
                ("names not matching " if c.negate else "names matching ") + ", ".join(c.values)
            )
    return " and ".join(parts)


def summarize(
    pipeline: Pipeline,
    policy_names: Mapping[str, str],
    reader_names: Mapping[str, str] | None = None,
) -> list[str]:
    """What the pipeline does, in short words ("refuses health data").

    Labels only: never a pattern, an allow-list or another user's anything.
    Observe-only rules and rules that are off are left out, because they change
    nothing for the caller.
    """
    words: list[str] = []
    for rule in pipeline.all_rules():
        if not rule.enabled or rule.observe_only or rule.is_noop:
            continue
        when = _condition_words(rule)
        if rule.level == rulelib.LEVEL_REFUSE:
            words.append(f"refuses {when}" if when else "refuses everything")
            continue
        if rule.level == rulelib.LEVEL_READ_WITH:
            reader = rule.action.get("reader")
            label = (reader_names or {}).get(
                str(reader),
                "the built-in extractor" if reader == rulelib.BUILTIN_READER else str(reader),
            )
            words.append(f"reads {when} with {label}" if when else f"reads with {label}")
        policy = rule.action.get("redact_with")
        if policy:
            name = policy_names.get(str(policy), "a policy")
            words.append(f"redacts with {name}" + (f" for {when}" if when else ""))
    return list(dict.fromkeys(words))
