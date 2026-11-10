"""Judging one request against the rules of ONE pipeline (ADR 0114).

Stateless: the outcome is a function of the facts, the rules and the configuration, and
nothing is remembered between requests (the verdict cache only saves time). **Every rule
is evaluated; there is no first match.** A rule's conditions combine with its ``match``
(all or any), each can be negated, and all are judged on the original text. The trace says,
for every rule, whether it matched and why, in the words the console shows an administrator.
Combining the pipelines of a request, and claiming spans, are ``combine``'s and
``redaction.http``'s jobs.

Two things are deliberately not here: any I/O of its own (the caller supplies facts and a
``Judge`` that counts items and asks classifiers) and any action.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from gateway.privacy.finds import Item, Judge
from gateway.privacy.rules import (
    CONTENT_KEYS,
    Condition,
    Rule,
    glob_matches,
)


class Undecidable(Exception):
    """A negated condition that cannot be decided, on a rule that protects.

    "Does not find any" cannot be assumed true or false: the text it exists to catch might
    be there. The caller refuses (503) naming the rule (ADR 0113, section 4; ADR 0114).
    """

    def __init__(self, rule: Rule, why: str) -> None:
        super().__init__(why)
        self.rule = rule
        self.why = why


class State(enum.StrEnum):
    TRUE = "true"
    FALSE = "false"
    #: Cannot be told yet: needs a fact found while reading (a scan).
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Facts:
    """What Pystino observed in one request. Never logged as a whole."""

    #: ``read`` or ``content``.
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
    #: The model's own type (``chat``, ``embedding``, ``rerank``) for a content request.
    model_type: str | None = None
    provider_id: str | None = None
    model_id: str | None = None
    #: The text the content conditions judge. Held in memory for the call only.
    text: str | None = field(default=None, repr=False)
    #: A batch (embedding): each input is judged separately and in full.
    texts: tuple[str, ...] = field(default=(), repr=False)
    #: Counts items and asks classifiers (one per request). None: no content side.
    judge: Judge | None = field(default=None, repr=False, compare=False)

    @property
    def inputs(self) -> tuple[str, ...]:
        return self.texts or ((self.text,) if self.text else ())


@dataclass(slots=True)
class Step:
    """What happened at one rule, for the log and the test endpoint."""

    rule: Rule
    #: ``matched``, ``not_matched``, ``skipped_off`` or ``pending`` (needs a fact found while
    #: reading).
    result: str
    reasons: list[str] = field(default_factory=list)
    #: A content condition could not be decided and the rule acted fail-closed.
    degraded: list[str] = field(default_factory=list)
    #: Every item the rule's ``finds`` condition counted (a text a rule hides is the same).
    items: list[Item] = field(default_factory=list)


@dataclass(slots=True)
class Evaluation:
    kind: str
    steps: list[Step] = field(default_factory=list)
    #: The rules that matched, in order.
    matched: list[Rule] = field(default_factory=list)
    #: Some rule waits for a fact found while reading (a scan).
    pending: bool = False
    pending_rule: Rule | None = None
    #: What the classifiers said, by name: labels only.
    verdicts: dict[str, Any] = field(default_factory=dict)
    #: Distinct items each rule's "finds" condition counted, by rule name: counts only.
    found: dict[str, int] = field(default_factory=dict)
    degraded: list[str] = field(default_factory=list)


_CHEAP_FIRST = ("filetype", "filename", "scan", "finds", "classified")


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


def _plural(n: int, word: str = "item") -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


class Evaluator:
    """Evaluates rules against facts. Stateless: the judge and the facts are the whole input."""

    def _undecided(
        self, condition: Condition, rule: Rule, degraded: list[str], why: str
    ) -> tuple[State, str]:
        """What an undecidable condition counts as, so that it can only ever add protection.

        A rule that does not protect (``off``, ``detect_only``) simply does not match. A
        rule that protects treats the condition as a match, unless it is negated: then the
        request is refused (503), because "found nothing" cannot be assumed.
        """
        if not rule.protective:
            degraded.append(f"{why}; not matched.")
            return State.FALSE, f"{why}; not matched."
        if condition.negate:
            raise Undecidable(rule, why)
        degraded.append(f"{why}; treated as a match.")
        return State.TRUE, f"{why}; treated as a match."

    async def _content(
        self,
        condition: Condition,
        facts: Facts,
        ev: Evaluation,
        degraded: list[str],
        rule: Rule,
        step_items: list[Item],
    ) -> tuple[State, str]:
        """ "The rule's policies find at least N items" and "classified as <classifier>"."""
        if facts.kind == "extract":
            # Reading decides from the file's facts, never its text. A stored row that still
            # carries a content condition (it cannot be saved any more) is undecidable.
            return self._undecided(
                condition, rule, degraded, "An extract rule cannot judge what the file says"
            )
        judge = facts.judge
        if judge is None:
            return self._undecided(
                condition, rule, degraded, "Nothing is available to judge the text"
            )
        texts = facts.inputs
        if condition.key == "finds":
            names = [
                judge.policy_name(p) or "a policy that no longer exists" for p in rule.policies
            ]
            shown = " + ".join(names) or "the rule's policies"
            result = await judge.find(rule.policies, texts)
            n = len(result.items)
            ev.found[rule.name] = max(ev.found.get(rule.name, 0), n)
            if result.items:
                step_items.extend(i for i in result.items if i not in step_items)
            if result.incomplete and n < condition.count:
                return self._undecided(
                    condition, rule, degraded, "The text was too large to check in full"
                )
            if result.unavailable and n < condition.count:
                # The count may be short, so "fewer than N" cannot be claimed.
                return self._undecided(
                    condition, rule, degraded, f"'{shown}' cannot be checked ({result.unavailable})"
                )
            hit = n >= condition.count
            text = (
                f"'{shown}' finds {_plural(n)}"
                f" ({'at least ' if hit else 'fewer than '}{condition.count} needed)."
            )
        else:
            target = condition.values[0] if condition.values else ""
            name = judge.classifier_name(target) or "a classifier that no longer exists"
            verdict = await judge.classify(target, texts)
            ev.verdicts[name] = verdict.positive
            if verdict.positive:
                hit = True
            elif verdict.unavailable or verdict.incomplete:
                why = (
                    f"'{name}' cannot decide ({verdict.unavailable})"
                    if verdict.unavailable
                    else "The text was too large to classify in full"
                )
                return self._undecided(condition, rule, degraded, why)
            else:
                hit = False
            text = f"The text {'is' if hit else 'is not'} classified as '{name}'."
        if hit != condition.negate:
            return State.TRUE, text
        return State.FALSE, text

    async def evaluate(self, rules: Sequence[Rule], facts: Facts) -> Evaluation:
        ev = Evaluation(kind=facts.kind)
        for rule in sorted(rules, key=lambda r: r.position):
            if not rule.enabled:
                ev.steps.append(Step(rule, "skipped_off", ["The rule is off."]))
                continue
            reasons: list[str] = []
            degraded: list[str] = []
            items: list[Item] = []
            states: list[State] = []
            every = rule.match == "all"
            for condition in _ordered(rule.effective_conditions):
                state, why = await self._condition(condition, facts, ev, degraded, rule, items)
                reasons.append(why)
                states.append(state)
                if (every and state is State.FALSE) or (not every and state is State.TRUE):
                    break
            if every:
                verdict = (
                    State.FALSE
                    if State.FALSE in states
                    else State.UNKNOWN
                    if State.UNKNOWN in states
                    else State.TRUE
                )
            else:
                verdict = (
                    State.TRUE
                    if State.TRUE in states
                    else State.UNKNOWN
                    if State.UNKNOWN in states
                    else State.FALSE
                )
            if verdict is State.FALSE:
                ev.steps.append(Step(rule, "not_matched", reasons[-1:], items=items))
            elif verdict is State.UNKNOWN:
                ev.steps.append(Step(rule, "pending", reasons))
                if ev.pending_rule is None and rule.protective:
                    ev.pending, ev.pending_rule = True, rule
            else:
                ev.matched.append(rule)
                ev.degraded.extend(m for m in degraded if m not in ev.degraded)
                ev.steps.append(Step(rule, "matched", reasons, degraded, items))
        return ev

    async def _condition(
        self,
        condition: Condition,
        facts: Facts,
        ev: Evaluation,
        degraded: list[str],
        rule: Rule,
        items: list[Item],
    ) -> tuple[State, str]:
        # A rule that protects (blocks, reads elsewhere, hides) is protective for a file whose
        # name or type is unknown: it matches rather than miss.
        closed = rule.kind == "extract" and rule.protective
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
            return await self._content(condition, facts, ev, degraded, rule, items)
        return State.FALSE, f"Unknown condition {condition.key}."


def rule_id_str(rule: Rule | None) -> str | None:
    return str(rule.id) if rule is not None else None
