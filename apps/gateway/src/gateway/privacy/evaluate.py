"""Judging one request against the rules of ONE pipeline (ADR 0113).

Stateless: the outcome is a function of the facts, the rules and the
configuration, and nothing is remembered between requests (the verdict cache
only saves time). First match wins within a pipeline; an observe-only rule that
matches is recorded and the search goes on. The trace says, for every rule, why
it did or did not apply, in the words the console shows an administrator.
Combining the pipelines of a request is ``gateway.privacy.combine``'s job.

Two things are deliberately not here: any I/O (the caller supplies facts and the
profile table) and any action (the caller carries out ``Evaluation.fired``).
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from gateway.privacy.classify import MAX_REQUEST_CHARS, VerdictCache
from gateway.privacy.rules import (
    CONTENT_KEYS,
    PATTERNS,
    Condition,
    Rule,
    glob_matches,
)

#: Detector engines whose verdicts are the quick check in this release. "Fast"
#: (presidio) answers verdicts from patterns; a model engine arrives in phase 2.
VERDICT_ENGINES = frozenset({"presidio", "patterns"})
MAX_FALLBACK_DEPTH = 3


class State(enum.StrEnum):
    TRUE = "true"
    FALSE = "false"
    #: Cannot be told yet: needs a fact found while reading (a scan).
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Profile:
    """A detector profile as the evaluator sees it."""

    id: str
    name: str
    engine: str
    fallback: str = "refuse"
    fallback_profile_id: str | None = None
    #: Health, from the caller; False when the engine is down or not installed.
    available: bool = True


@dataclass(frozen=True, slots=True)
class Facts:
    """What Pystino observed in one request. Never logged as a whole."""

    kind: str
    user_id: str | None = None
    billing_group_id: str | None = None
    api_key_id: str | None = None
    filename: str | None = None
    mime: str | None = None
    filetype: str | None = None
    #: None until the document has been read.
    is_scan: bool | None = None
    #: The target, as names (for attribution) and ids (for scope).
    target_alias: str | None = None
    target_model: str | None = None
    target_provider: str | None = None
    provider_id: str | None = None
    model_id: str | None = None
    #: The text verdicts are computed from. Held in memory for the call only.
    text: str | None = field(default=None, repr=False)
    #: A batch (embedding): each input is judged separately and in full.
    texts: tuple[str, ...] = field(default=(), repr=False)

    @property
    def inputs(self) -> tuple[str, ...]:
        return self.texts or ((self.text,) if self.text else ())


@dataclass(frozen=True, slots=True)
class BatchVerdict:
    """What the quick check found over every input of a request.

    ``health`` and ``ids`` hold if they hold for *any* input; ``langs`` is each
    input's language. ``incomplete`` means some input (or the request's total)
    exceeded the size caps and part of it was not checked.
    """

    health: bool
    ids: bool
    langs: tuple[str, ...]
    incomplete: bool


@dataclass(frozen=True, slots=True)
class Decider:
    """How a content condition was (or could not be) decided."""

    method: str  # "patterns" | "unavailable"
    profile: str | None = None
    #: "Medical -> Fast", when a fallback decided in place of the profile chosen.
    fallback_used: str | None = None
    note: str | None = None


def resolve_decider(decide: str | None, profiles: Mapping[str, Profile]) -> Decider:
    """The method that will answer a condition, following profile fallbacks."""
    if decide in (None, PATTERNS):
        return Decider("patterns")
    chain: list[str] = []
    current = profiles.get(decide or "")
    if current is None:
        return Decider("unavailable", note="the chosen profile no longer exists")
    first = current
    for _ in range(MAX_FALLBACK_DEPTH + 1):
        if current is None or current.id in chain:
            break
        chain.append(current.id)
        if current.available and current.engine in VERDICT_ENGINES:
            used = None if current is first else f"{first.name} -> {current.name}"
            return Decider("patterns", profile=current.name, fallback_used=used)
        if current.fallback == "fallback_to" and current.fallback_profile_id:
            current = profiles.get(current.fallback_profile_id)
            continue
        break
    why = (
        f"{first.name} is not available"
        if first.engine in VERDICT_ENGINES or first.available
        else f"{first.name} is not installed here"
    )
    return Decider("unavailable", profile=first.name, note=why)


@dataclass(slots=True)
class Step:
    """What happened at one rule, for the log and the test endpoint."""

    rule: Rule
    #: ``fired``, ``observed``, ``not_matched``, ``skipped_off``, ``not_reached``,
    #: ``pending`` (needs a fact found while reading).
    result: str
    reasons: list[str] = field(default_factory=list)
    #: A content condition could not be decided and the rule acted fail-closed.
    degraded: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Evaluation:
    kind: str
    fired: Rule | None = None
    observed: list[Rule] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    #: Facts the answer waits for ("scan"): the first matching-so-far rule needs them.
    pending: str | None = None
    #: The rule that is waiting for it.
    pending_rule: Rule | None = None
    #: What the classifier found, when one ran: labels only.
    verdicts: dict[str, Any] = field(default_factory=dict)
    degraded: list[str] = field(default_factory=list)
    fallbacks: list[str] = field(default_factory=list)


_CHEAP_FIRST = ("filetype", "filename", "scan", "health", "ids", "lang")


def _ordered(conditions: Sequence[Condition]) -> list[Condition]:
    return sorted(conditions, key=lambda c: _CHEAP_FIRST.index(c.key))


def _filetype(condition: Condition, facts: Facts, closed: bool) -> tuple[State, str]:
    if closed and facts.filetype is None:
        # A file whose type cannot be told (a link with no extension, no MIME
        # type sent): a protective rule must not be skipped for want of a fact.
        return State.TRUE, "The file type is not known; treated as a match."
    hit = facts.filetype in condition.values if facts.filetype else False
    shown = ", ".join(condition.values)
    kind = facts.filetype or "an unknown type"
    if hit != condition.negate:
        return State.TRUE, f"The file ({kind}) {'is not' if condition.negate else 'is'} {shown}."
    return State.FALSE, f"The file ({kind}) {'is' if condition.negate else 'is not'} {shown}."


def _scan(condition: Condition, facts: Facts) -> tuple[State, str]:
    if facts.is_scan is None:
        return State.UNKNOWN, "Whether the file is a scan is only known after reading it."
    hit = facts.is_scan
    if hit != condition.negate:
        return State.TRUE, f"The file {'is not' if condition.negate else 'is'} a scan."
    return State.FALSE, f"The file {'is' if condition.negate else 'is not'} a scan."


def _filename(condition: Condition, facts: Facts, closed: bool) -> tuple[State, str]:
    name = facts.filename or ""
    if closed and not name:
        return State.TRUE, "The request names no file; treated as a match."
    hit = bool(name) and any(glob_matches(g, name) for g in condition.values)
    shown = ", ".join(condition.values)
    label = f"'{name}'" if name else "The request names no file; it"
    if hit != condition.negate:
        return State.TRUE, f"{label} {'does not match' if condition.negate else 'matches'} {shown}."
    return State.FALSE, f"{label} {'matches' if condition.negate else 'does not match'} {shown}."


class Evaluator:
    """Evaluates rules against facts, with the deployment's profiles and cache."""

    def __init__(
        self, profiles: Mapping[str, Profile] | None = None, cache: VerdictCache | None = None
    ) -> None:
        self.profiles = profiles or {}
        self.cache = cache or VerdictCache()

    # Content conditions are decided on the request's own text, each input
    # separately and in full. The verdict is computed once per request however
    # many rules ask.
    def _classification(self, facts: Facts, ev: Evaluation) -> BatchVerdict:
        found = ev.verdicts.get("_classification")
        if isinstance(found, BatchVerdict):
            return found
        health = ids = incomplete = False
        langs: list[str] = []
        budget = MAX_REQUEST_CHARS
        for text in facts.inputs:
            if budget <= 0:
                incomplete = True
                break
            budget -= len(text)
            one = self.cache.classify(text)
            health, ids = health or one.health, ids or one.ids
            incomplete = incomplete or one.incomplete
            langs.append(one.lang)
        result = BatchVerdict(health, ids, tuple(langs), incomplete or budget < 0)
        ev.verdicts["_classification"] = result
        ev.verdicts["health"] = result.health
        ev.verdicts["ids"] = result.ids
        ev.verdicts["lang"] = ",".join(sorted(set(result.langs))) or None
        return result

    def _content(
        self, condition: Condition, facts: Facts, ev: Evaluation, degraded: list[str]
    ) -> tuple[State, str]:
        decider = resolve_decider(condition.decide, self.profiles)
        if decider.fallback_used and decider.fallback_used not in ev.fallbacks:
            ev.fallbacks.append(decider.fallback_used)
        if decider.method == "unavailable":
            # Fail closed: assume the condition holds, so a protective rule acts.
            message = f"{decider.note or 'The detector is unavailable'}; treated as a match."
            degraded.append(message)
            return State.TRUE, message
        result = self._classification(facts, ev)
        if result.incomplete:
            # Part of the text was not checked, so "clean" cannot be claimed:
            # fail closed, as for an unavailable detector.
            message = "The text was too large to check in full; treated as a match."
            degraded.append(message)
            return State.TRUE, message
        if condition.key in ("health", "ids"):
            hit = bool(getattr(result, condition.key))
            shown = {"health": "health data", "ids": "identifiers"}[condition.key]
            text = f"The text {'contains' if hit else 'does not contain'} {shown}."
        else:
            # A batch is in a language if any input is.
            hit = any(lang in condition.values for lang in result.langs)
            seen = ", ".join(sorted(set(result.langs))) or "none"
            text = f"The text is in {seen}, rule says {', '.join(condition.values)}."
        if hit != condition.negate:
            return State.TRUE, text
        return State.FALSE, text

    async def evaluate(self, rules: Sequence[Rule], facts: Facts) -> Evaluation:
        ev = Evaluation(kind=facts.kind)
        for rule in sorted(rules, key=lambda r: r.position):
            if ev.fired is not None:
                ev.steps.append(Step(rule, "not_reached", ["An earlier rule already applied."]))
                continue
            if not rule.enabled:
                ev.steps.append(Step(rule, "skipped_off", ["The rule is off."]))
                continue
            reasons: list[str] = []
            degraded: list[str] = []
            unknown = False
            matched = True
            for condition in _ordered(rule.conditions):
                state, why = self._condition(condition, facts, ev, degraded, rule)
                reasons.append(why)
                if state is State.FALSE:
                    matched = False
                    break
                if state is State.UNKNOWN:
                    unknown = True
            if not matched:
                ev.steps.append(Step(rule, "not_matched", reasons[-1:]))
                continue
            if unknown:
                ev.steps.append(Step(rule, "pending", reasons))
                if not rule.observe_only:
                    # Rules after this one cannot be judged until the fact is known.
                    ev.pending = "scan"
                    ev.pending_rule = rule
                    for later in sorted(rules, key=lambda r: r.position):
                        if later.position > rule.position:
                            ev.steps.append(
                                Step(later, "not_reached", ["Waiting for the file to be read."])
                            )
                    ev.verdicts.pop("_classification", None)
                    return ev
                continue
            if rule.observe_only:
                ev.observed.append(rule)
                ev.degraded.extend(m for m in degraded if m not in ev.degraded)
                ev.steps.append(Step(rule, "observed", reasons, degraded))
                continue
            ev.fired = rule
            ev.degraded.extend(m for m in degraded if m not in ev.degraded)
            ev.steps.append(Step(rule, "fired", reasons, degraded))
        ev.verdicts.pop("_classification", None)
        return ev

    def _condition(
        self,
        condition: Condition,
        facts: Facts,
        ev: Evaluation,
        degraded: list[str],
        rule: Rule,
    ) -> tuple[State, str]:
        # A rule that protects (refuses, reads elsewhere, redacts) is protective
        # for a file whose name or type is unknown: it matches rather than miss.
        closed = rule.kind == "read" and rule.protective
        if condition.key == "scan":
            return _scan(condition, facts)
        if condition.key == "filetype":
            state, why = _filetype(condition, facts, closed)
            if "treated as a match" in why:
                degraded.append(why)
            return state, why
        if condition.key == "filename":
            state, why = _filename(condition, facts, closed)
            if "treated as a match" in why:
                degraded.append(why)
            return state, why
        if condition.key in CONTENT_KEYS:
            return self._content(condition, facts, ev, degraded)
        return State.FALSE, f"Unknown condition {condition.key}."


def rule_id_str(rule: Rule | None) -> str | None:
    return str(rule.id) if rule is not None else None


def profile_snapshot(
    rows: Sequence[Any], available: Mapping[uuid.UUID, bool] | None = None
) -> dict[str, Profile]:
    """Profiles keyed by id string, from ORM rows or anything with their fields."""
    out: dict[str, Profile] = {}
    for row in rows:
        out[str(row.id)] = Profile(
            id=str(row.id),
            name=row.name,
            engine=row.engine,
            fallback=row.fallback,
            fallback_profile_id=str(row.fallback_profile_id) if row.fallback_profile_id else None,
            available=(available or {}).get(row.id, True),
        )
    return out
