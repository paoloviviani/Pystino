"""A user's own redaction rules, folded in after the administrators' (ADR 0112).

The administrators' effective policy is computed exactly as before. A user's
rules are then applied as the narrowest scope there is, and the whole safety
property is that they **can only tighten**:

* a custom pattern is unioned in: one more regex only finds more;
* a stricter mode for an entity type wins over a weaker one, by the order
  ``off < anonymise_restore < anonymise < redact < block``. A mode that is not
  stricter than what the administrators already say is inert, and a tie goes to
  the administrators;
* there is nothing else. No ``off`` (refused when written and by a CHECK), no
  threshold, no exception, no allow-list, no placeholder style: the request
  model has no such fields, so there is no value to validate away.

Not :meth:`RedactionPolicy.combine`, on purpose. That fold lets any scope veto
same-length placeholders and lower a threshold, which are tightenings for an
administrator's rule but would silently change what an administrator chose to
send when a *user* writes a rule about the same type. This one touches only the
modes and the patterns.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from gateway.config import (
    Contributor,
    CustomEntity,
    CustomPattern,
    EntityMode,
    EntityPolicy,
    KeywordList,
    PlaceholderStyle,
    RedactionPolicy,
)

#: The most rules one user may keep. Their rules are held in memory on every
#: worker (the request path stays query-free), so the count is bounded.
MAX_RULES_PER_USER = 25

#: The modes a user may choose: every mode but ``off``.
USER_MODES = tuple(mode for mode in EntityMode if mode is not EntityMode.OFF)

Kind = Literal["pattern", "mode"]

_ORDER = list(EntityMode)


def rank(mode: EntityMode) -> int:
    return _ORDER.index(mode)


@dataclass(frozen=True, slots=True)
class UserRule:
    """One rule, parsed once when it is loaded rather than per request."""

    id: uuid.UUID
    kind: str
    #: A pattern's label, or the entity type a mode applies to; upper case.
    name: str
    regex: str | None
    mode: EntityMode

    def label(self) -> str:
        """How the log names it: what the user would recognise."""
        return self.name

    def as_policy(self) -> RedactionPolicy:
        """This rule on its own, for attribution (``EffectivePolicy.decider_for``)."""
        if self.kind == "pattern" and self.regex is not None:
            # A pattern is evaluated here, never asked of the redaction service: its name
            # is not an entity type, and listing it under `entities` made the service
            # answer "cannot serve" (a warning per request, a 503 in strict detection).
            return RedactionPolicy(
                patterns=[CustomPattern(name=self.name, regex=self.regex, mode=self.mode)],
            )
        return RedactionPolicy(entities={self.name: EntityPolicy(mode=self.mode)})

    def contributor(self) -> Contributor:
        return Contributor(id=self.id, name=self.label(), owner="user", policy=self.as_policy())


def _named_mode(policy: RedactionPolicy, name: str) -> tuple[EntityMode, PlaceholderStyle] | None:
    """What *policy* already says about a pattern's label, if anything.

    An entity entry or a pattern of that name: a pattern with a fresh label
    collides with nothing and keeps the mode it was written with.
    """
    found = policy._named(name)
    return (found[0], found[1]) if found is not None else None


def fold(
    base: RedactionPolicy,
    rules: Sequence[UserRule],
    *,
    keywords: Sequence[KeywordList] = (),
    custom_entities: Sequence[CustomEntity] = (),
) -> RedactionPolicy:
    """*base* with the user's rules applied. Never weaker than *base*.

    ``keywords`` and ``custom_entities`` are what an administrator policy a user's
    pipeline applies adds beyond modes and regexes (a user's own entries have neither):
    more words to find and more types to ask for can only tighten, and a name that is
    already there keeps the stricter mode.
    """
    entities = dict(base.entities)
    patterns = list(base.patterns)
    held_keywords = list(base.keywords)
    for item in keywords:
        if item.mode is EntityMode.OFF:
            continue
        same = next(
            (
                i
                for i, k in enumerate(held_keywords)
                if (k.name, k.words, k.case_sensitive)
                == (item.name, item.words, item.case_sensitive)
            ),
            None,
        )
        if same is None:
            held_keywords.append(item)
        elif rank(item.mode) > rank(held_keywords[same].mode):
            held_keywords[same] = item
    held_custom = list(base.custom_entities)
    for custom in custom_entities:
        if custom.mode is EntityMode.OFF:
            continue
        same = next(
            (
                i
                for i, c in enumerate(held_custom)
                if (c.name, c.label) == (custom.name, custom.label)
            ),
            None,
        )
        if same is None:
            held_custom.append(custom)
        elif rank(custom.mode) > rank(held_custom[same].mode):
            held_custom[same] = custom
    seen = {(pattern.name, pattern.regex) for pattern in patterns}

    def current() -> RedactionPolicy:
        # `model_construct`: the pieces are already validated, and this runs a
        # few times per fold, which is memoised per user.
        return RedactionPolicy.model_construct(
            engine=base.engine,
            default_mode=base.default_mode,
            formats=base.formats,
            entities=entities,
            custom_entities=held_custom,
            patterns=patterns,
            keywords=held_keywords,
            allow_list=base.allow_list,
        )

    for rule in rules:
        if rule.kind == "pattern" and rule.regex is not None:
            collides = _named_mode(current(), rule.name)
            if (rule.name, rule.regex) not in seen:
                seen.add((rule.name, rule.regex))
                patterns.append(CustomPattern(name=rule.name, regex=rule.regex, mode=rule.mode))
            if collides is not None and rank(rule.mode) > rank(collides[0]):
                # The label is already in use (an administrator's pattern, or a
                # type they named): mode_for reads the entity entry first, so
                # the user's stricter mode has to be written there to apply.
                existing = entities.get(rule.name) or base.formats.get(rule.name)
                entities[rule.name] = EntityPolicy(
                    mode=rule.mode,
                    placeholder=collides[1],
                    threshold=(
                        existing.threshold
                        if existing is not None and existing.mode is not EntityMode.OFF
                        else None
                    ),
                )
        else:
            policy = current()
            if rank(rule.mode) > rank(policy.mode_for(rule.name)):
                existing = entities.get(rule.name) or base.formats.get(rule.name)
                entities[rule.name] = EntityPolicy(
                    mode=rule.mode,
                    placeholder=policy.placeholder_style_for(rule.name),
                    threshold=(
                        existing.threshold
                        if existing is not None and existing.mode is not EntityMode.OFF
                        else None
                    ),
                )
    return current()


def tightens(base: RedactionPolicy, rule: UserRule) -> bool:
    """Whether *rule* changes what *base* does, i.e. is stricter than it."""
    if rule.kind == "pattern" and rule.regex is not None:
        if (rule.name, rule.regex) not in {(p.name, p.regex) for p in base.patterns}:
            collides = _named_mode(base, rule.name)
            return collides is None or rank(rule.mode) > rank(collides[0])
        return rank(rule.mode) > rank(base.mode_for(rule.name))
    return rank(rule.mode) > rank(base.mode_for(rule.name))


def validate_pattern(name: str, regex: str, mode: EntityMode) -> CustomPattern:
    """A user's pattern, under the administrators' own validation.

    Raises ``ValueError`` (pydantic's, which carries RE2's own message) for a
    regex RE2 refuses, one that matches the empty string, or a name with no
    letter or digit; and for ``off``, which a user may not choose.
    """
    if mode is EntityMode.OFF:
        raise ValueError("a rule of yours can only add protection, so it cannot be 'off'")
    return CustomPattern(name=name, regex=regex, mode=mode)


def clean_label(value: str) -> str:
    """An entity type or pattern label in the one alphabet placeholders use.

    The same rule as :class:`CustomPattern`'s name validator, so a type typed as
    ``person`` and a pattern named ``my project`` end up as the labels the
    detector and the substitution already use.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_").upper()
    if not cleaned:
        raise ValueError("a name needs at least one letter or digit")
    if len(cleaned) > 64:
        raise ValueError("a name can be at most 64 characters")
    return cleaned
