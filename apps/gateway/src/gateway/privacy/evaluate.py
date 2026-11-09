"""Judging one request against the rules of ONE pipeline (ADR 0113).

Stateless: the outcome is a function of the facts, the rules and the
configuration, and nothing is remembered between requests (the verdict cache
only saves time). First match wins within a pipeline; an observe-only rule that
matches is recorded and the search goes on. The trace says, for every rule, why
it did or did not apply, in the words the console shows an administrator.
Combining the pipelines of a request is ``gateway.privacy.combine``'s job.

Two things are deliberately not here: any I/O of its own (the caller supplies facts and
a ``Judge`` that counts items and asks classifiers) and any action (the caller carries
out ``Evaluation.fired``).
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from gateway.privacy.finds import Item, Judge
from gateway.privacy.rules import (
    CONTENT_KEYS,
    LADDER,
    Condition,
    Rule,
    glob_matches,
)


class Undecidable(Exception):
    """A condition that cannot be decided, on a rule where "treat it as a match" would fail open.

    Raised for a negated condition ("no health data -> pass") or a rule that adds
    nothing: assuming the condition holds there would let the very text it exists
    to catch through. The caller refuses (503) naming the rule (ADR 0113, section 4).
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
    #: ``fired``, ``observed``, ``not_matched``, ``skipped_off``, ``not_reached``,
    #: ``pending`` (needs a fact found while reading).
    result: str
    reasons: list[str] = field(default_factory=list)
    #: A content condition could not be decided and the rule acted fail-closed.
    degraded: list[str] = field(default_factory=list)
    #: Every item a "finds" condition of this rule counted, by policy name.
    items: dict[str, list[Item]] = field(default_factory=dict)


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
    #: What the classifiers said, by name: labels only.
    verdicts: dict[str, Any] = field(default_factory=dict)
    #: Distinct items each "finds" condition counted, by policy name: counts only.
    found: dict[str, int] = field(default_factory=dict)
    degraded: list[str] = field(default_factory=list)
    #: How strict the strictest later enabled rule of the pipeline is (set while a rule is
    #: judged): an undecided condition may stand in for a match only on a rule at least as
    #: strict, or the rule would fire in place of a stricter one it was shadowing.
    later_rules: tuple[Rule, ...] = ()


def covers(rule: Rule, later: Sequence[Rule]) -> bool:
    """Whether ``rule`` is at least as strict as every rule in ``later``.

    Equal strictness on a rule that redacts counts only for the same policy: another policy
    may redact more (or block), so the rule would shadow it."""
    mine = strictness(rule)
    for other in later:
        theirs = strictness(other)
        if mine < theirs:
            return False
        if (
            mine == theirs
            and mine[1]
            and rule.action.get("redact_with") != other.action.get("redact_with")
        ):
            return False
    return True


def strictness(rule: Rule) -> tuple[int, bool]:
    """Where an action sits on the ladder, and whether it also redacts."""
    ladder = LADDER[rule.kind]
    level = ladder.index(rule.level) if rule.level in ladder else 0
    return level, bool(rule.action.get("redact_with"))


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
        self, condition: Condition, rule: Rule, degraded: list[str], why: str, ev: Evaluation
    ) -> tuple[State, str]:
        """What an undecidable condition counts as, so that it can only ever add protection.

        A match on a protective rule whose condition is not negated (the rule then
        acts). An observe-only rule takes no action, so it simply does not match.
        Anything else (a negated condition, a rule that adds nothing) could let the
        text through, so it is refused instead.
        """
        if rule.observe_only:
            degraded.append(f"{why}; not matched.")
            return State.FALSE, f"{why}; not matched."
        if rule.protective and not condition.negate and covers(rule, ev.later_rules):
            degraded.append(f"{why}; treated as a match.")
            return State.TRUE, f"{why}; treated as a match."
        raise Undecidable(rule, why)

    async def _content(
        self,
        condition: Condition,
        facts: Facts,
        ev: Evaluation,
        degraded: list[str],
        rule: Rule,
        step_items: dict[str, list[Item]],
    ) -> tuple[State, str]:
        """ "<policy> finds at least N items" and "classified as <classifier>"."""
        if facts.kind == "read":
            # Reading decides from the file's facts, never its text. A stored row that still
            # carries a content condition (it cannot be saved any more) is undecidable, so a
            # protective rule acts and anything else is refused, never read as "found nothing".
            return self._undecided(
                condition, rule, degraded, "A reading rule cannot judge what the file says", ev
            )
        judge = facts.judge
        target = condition.values[0] if condition.values else ""
        if judge is None:
            return self._undecided(
                condition, rule, degraded, "Nothing is available to judge the text", ev
            )
        texts = facts.inputs
        if condition.key == "finds":
            name = judge.policy_name(target) or "a policy that no longer exists"
            result = await judge.find(target, texts)
            n = len(result.items)
            ev.found[name] = max(ev.found.get(name, 0), n)
            if result.items:
                step_items[name] = list(result.items)
            if result.incomplete and n < condition.count:
                return self._undecided(
                    condition, rule, degraded, "The text was too large to check in full", ev
                )
            if result.unavailable and n < condition.count:
                # The count may be short, so "fewer than N" cannot be claimed.
                return self._undecided(
                    condition,
                    rule,
                    degraded,
                    f"'{name}' cannot be checked ({result.unavailable})",
                    ev,
                )
            hit = n >= condition.count
            text = (
                f"'{name}' finds {_plural(n)}"
                f" ({'at least ' if hit else 'fewer than '}{condition.count} needed)."
            )
        else:
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
                return self._undecided(condition, rule, degraded, why, ev)
            else:
                hit = False
            text = f"The text {'is' if hit else 'is not'} classified as '{name}'."
        if hit != condition.negate:
            return State.TRUE, text
        return State.FALSE, text

    async def evaluate(
        self, rules: Sequence[Rule], facts: Facts, otherwise: Rule | None = None
    ) -> Evaluation:
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
            items: dict[str, list[Item]] = {}
            unknown = False
            matched = True
            # The "Everything else" rule is always last and always enabled.
            ev.later_rules = tuple(
                later
                for later in (*rules, *([otherwise] if otherwise is not None else []))
                if later.position > rule.position and later.enabled and not later.observe_only
            )
            for condition in _ordered(rule.conditions):
                state, why = await self._condition(condition, facts, ev, degraded, rule, items)
                reasons.append(why)
                if state is State.FALSE:
                    matched = False
                    break
                if state is State.UNKNOWN:
                    unknown = True
            if not matched:
                ev.steps.append(Step(rule, "not_matched", reasons[-1:], items=items))
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
                    return ev
                continue
            if rule.observe_only:
                ev.observed.append(rule)
                ev.degraded.extend(m for m in degraded if m not in ev.degraded)
                ev.steps.append(Step(rule, "observed", reasons, degraded, items))
                continue
            ev.fired = rule
            ev.degraded.extend(m for m in degraded if m not in ev.degraded)
            ev.steps.append(Step(rule, "fired", reasons, degraded, items))
        return ev

    async def _condition(
        self,
        condition: Condition,
        facts: Facts,
        ev: Evaluation,
        degraded: list[str],
        rule: Rule,
        items: dict[str, list[Item]],
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
            return await self._content(condition, facts, ev, degraded, rule, items)
        return State.FALSE, f"Unknown condition {condition.key}."


def rule_id_str(rule: Rule | None) -> str | None:
    return str(rule.id) if rule is not None else None
