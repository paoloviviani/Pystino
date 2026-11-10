"""Counting what a policy finds, and asking a classifier (ADR 0113, section 0c).

One object finds things: the redaction policy. A condition "<policy> finds at
least N items" runs the policy's own items over the request's text and counts the
**distinct** ones, in any mode but ``off`` (``detect_only`` exists for exactly
this: found and counted, never hidden). The same editor defines what is looked
for in both uses, and the Test trace lists each item with its group, and the
score and engine for entities.

* **Patterns** (custom regular expressions and keyword lists) are evaluated here,
  in the gateway, in linear time, and they never fail.
* **Built-in formats and entities** are the redaction service's. If it, or the NER
  engine the policy chose, cannot answer, the result says so (``unavailable``) and
  the evaluator applies the fail-closed rule of section 0: it never reads
  "could not look" as "found nothing". ``fail_open`` does not apply (``strict``).

Nothing here keeps text. Item values are held in memory for one request, for the
administrator's Test tab and Try-it box; they are never logged or stored.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol

from llmp_shared import EntitySpan, normalise_entity

from gateway.config import RedactionPolicy
from gateway.redaction.http import (
    EngineUnavailableError,
    RedactionUnavailableError,
    eligible_for,
)

logger = logging.getLogger(__name__)

#: The most one text may take. Past it the rest is *not checked* and the result
#: says so (``incomplete``); the evaluator then fails closed.
MAX_TEXT_CHARS = 2_000_000
#: The most a whole request may take, summed over its inputs.
MAX_REQUEST_CHARS = 4_000_000

Group = Literal["pattern", "entity"]


@dataclass(frozen=True, slots=True)
class Item:
    """One item a policy found: what it is, which group found it, and how sure."""

    group: Group
    #: The entity type or the name of the pattern or keyword list.
    type: str
    #: ``pattern``, ``spacy`` or ``gliner``.
    engine: str | None
    #: Entities only: the engine's confidence.
    score: float | None
    #: The text found. In memory only; shown to an administrator testing, never logged.
    value: str = field(repr=False, default="")
    #: The name of the policy that found it (set when a rule counts, for the trace).
    policy: str = ""


@dataclass(slots=True)
class FindResult:
    """What one policy found in a request's text."""

    #: Distinct items, in the order found.
    items: tuple[Item, ...] = ()
    #: The part the redaction service answers could not run, and why. The count
    #: may then be short.
    unavailable: str | None = None
    #: Some text was past the size caps and was not checked.
    incomplete: bool = False


@dataclass(slots=True)
class Verdict:
    """What a classifier said about a request's text."""

    positive: bool = False
    unavailable: str | None = None
    incomplete: bool = False


class Judge(Protocol):
    """What the evaluator asks of the content side (one per request)."""

    def policy_name(self, policy_id: str) -> str | None: ...

    def classifier_name(self, classifier_id: str) -> str | None: ...

    async def find(
        self,
        policy_ids: Sequence[str],
        texts: Sequence[str],
        exclude: frozenset[str] = frozenset(),
    ) -> FindResult: ...

    async def classify(self, classifier_id: str, texts: Sequence[str]) -> Verdict: ...


def group_of(span: EntitySpan, policy: RedactionPolicy) -> Group:
    """Which group found a span: by the engine that said so, else by what the type is."""
    if span.engine in ("spacy", "gliner"):
        return "entity"
    if span.engine == "pattern":
        return "pattern"
    name = span.entity_type.upper()
    if (
        name in policy.formats
        or any(p.name == name for p in policy.patterns)
        or any(k.name == name for k in policy.keywords)
    ):
        return "pattern"
    return "entity"


def distinct_items(text: str, spans: Sequence[EntitySpan], policy: RedactionPolicy) -> list[Item]:
    """The spans as distinct items: one per (type, normalised value), the best score kept."""
    found: dict[tuple[str, str], Item] = {}
    for span in spans:
        value = span.slice_of(text)
        key = (span.entity_type, normalise_entity(span.entity_type, value))
        group = group_of(span, policy)
        item = Item(
            group=group,
            type=span.entity_type,
            engine=span.engine or ("pattern" if group == "pattern" else None),
            score=None if group == "pattern" else span.score,
            value=value,
        )
        held = found.get(key)
        if held is None or (item.score or 0.0) > (held.score or 0.0):
            found[key] = item
    return list(found.values())


async def find_in(
    policy: RedactionPolicy,
    texts: Sequence[str],
    *,
    redactor: object | None,
    default_threshold: float,
    user_patterns: frozenset[tuple[str, str]] = frozenset(),
) -> FindResult:
    """Run *policy*'s items over *texts* and count the distinct ones (no side effects).

    The same code answers a "finds" condition, the Test tab and the Try-it box.
    """
    result = FindResult()
    budget = MAX_REQUEST_CHARS
    usable: list[str] = []
    for text in texts:
        if budget <= 0 or len(text) > MAX_TEXT_CHARS:
            result.incomplete = True
            if budget <= 0:
                break
            continue
        budget -= len(text)
        usable.append(text)
    if budget < 0:
        result.incomplete = True

    detected: list[list[EntitySpan]] = [[] for _ in usable]
    needs_service = (
        policy.default_mode.value != "off"
        or any(e.mode.value != "off" for e in policy.service_types().values())
    ) and bool(usable)
    if needs_service:
        detect = getattr(redactor, "detect", None)
        if detect is None or getattr(redactor, "name", "noop") == "noop":
            result.unavailable = "the redaction service is not configured"
        else:
            try:
                detected = await detect(
                    usable, policy=policy, asks=policy.ner_asks(acting=False), strict=True
                )
            except EngineUnavailableError as exc:
                result.unavailable = (
                    f"the NER engine {exc.engine or '(default)'} cannot answer: "
                    f"{exc.reason or 'it is not available'}"
                )
            except RedactionUnavailableError as exc:
                result.unavailable = (
                    "the redaction service timed out"
                    if exc.code == "redaction_timeout"
                    else "the redaction service could not be reached"
                )

    seen: dict[tuple[str, str], Item] = {}
    for text, spans in zip(usable, detected, strict=True):
        eligible = eligible_for(text, spans, policy, default_threshold, user_patterns, acting=False)
        for item in distinct_items(text, eligible, policy):
            seen.setdefault((item.type, normalise_entity(item.type, item.value)), item)
    result.items = tuple(seen.values())
    return result


# -- the classifier ---------------------------------------------------------------

#: A text is judged in windows of this size; any window positive makes the text positive.
CHUNK = 8_000
OVERLAP = 256
#: The most windows one request may take; past it the rest is not checked.
MAX_CHUNKS = 64


class ClassifierUnavailableError(Exception):
    """The decision model could not answer (no model, inactive, unreachable, unreadable reply)."""


class ClassifierBackend(Protocol):
    """Asks a decision model whether one chunk of text is about a topic."""

    async def positive(self, classifier: Any, chunk: str) -> bool: ...


def windows(text: str) -> list[str]:
    if len(text) <= CHUNK:
        return [text]
    out: list[str] = []
    start = 0
    while start < len(text):
        out.append(text[start : start + CHUNK])
        if start + CHUNK >= len(text):
            break
        start += CHUNK - OVERLAP
    return out


class ContentJudge:
    """The content side of one request: policies to count, classifiers to ask.

    Built per request from the state's snapshot (no queries), and it remembers its
    own answers for the request, so several rules naming one policy cost one pass.
    """

    def __init__(
        self,
        *,
        policies: Mapping[str, tuple[str, RedactionPolicy]],
        classifiers: Mapping[str, Any],
        redactor: object | None,
        default_threshold: float,
        backend: ClassifierBackend | None,
    ) -> None:
        self._policies = policies
        self._classifiers = classifiers
        self._redactor = redactor
        self._threshold = default_threshold
        self._backend = backend
        self._found: dict[tuple[str, tuple[str, ...]], FindResult] = {}
        self._combined: dict[tuple[Any, ...], FindResult] = {}
        self._verdicts: dict[tuple[str, tuple[str, ...]], Verdict] = {}

    def policy_name(self, policy_id: str) -> str | None:
        found = self._policies.get(policy_id)
        return found[0] if found is not None else None

    def classifier_name(self, classifier_id: str) -> str | None:
        found = self._classifiers.get(classifier_id)
        return getattr(found, "name", None)

    async def _find_one(self, policy_id: str, texts: Sequence[str]) -> FindResult:
        key = (policy_id, tuple(texts))
        if key in self._found:
            return self._found[key]
        entry = self._policies.get(policy_id)
        if entry is None:
            result = FindResult(unavailable="the policy no longer exists")
        else:
            result = await find_in(
                entry[1], texts, redactor=self._redactor, default_threshold=self._threshold
            )
            result.items = tuple(replace(i, policy=entry[0]) for i in result.items)
        self._found[key] = result
        return result

    async def find(
        self,
        policy_ids: Sequence[str],
        texts: Sequence[str],
        exclude: frozenset[str] = frozenset(),
    ) -> FindResult:
        """The distinct items the rule's own policies find, combined (ADR 0114).

        Each policy is run once per request (its answer is remembered), and the union is
        counted by (type, normalised value): two policies finding one name are one item.
        """
        ids = tuple(policy_ids)
        key = (ids, tuple(texts), exclude)
        held = self._combined.get(key)
        if held is not None:
            return held
        merged = FindResult()
        seen: dict[tuple[str, str], Item] = {}
        problems: list[str] = []
        for pid in ids:
            one = await self._find_one(pid, texts)
            merged.incomplete = merged.incomplete or one.incomplete
            if one.unavailable and one.unavailable not in problems:
                problems.append(one.unavailable)
            for item in one.items:
                if item.type.upper() in exclude:
                    continue  # a type the rule's override sets to off is not counted
                seen.setdefault((item.type, normalise_entity(item.type, item.value)), item)
        merged.items = tuple(seen.values())
        merged.unavailable = "; ".join(problems) or None
        self._combined[key] = merged
        return merged

    async def classify(self, classifier_id: str, texts: Sequence[str]) -> Verdict:
        key = (classifier_id, tuple(texts))
        if key in self._verdicts:
            return self._verdicts[key]
        ref = self._classifiers.get(classifier_id)
        verdict = Verdict()
        if ref is None or self._backend is None or not getattr(ref, "available", False):
            verdict.unavailable = getattr(ref, "why", "") or "the classifier is not available"
        else:
            chunks: list[str] = []
            for text in texts:
                chunks.extend(windows(text))
            if len(chunks) > MAX_CHUNKS:
                chunks, verdict.incomplete = chunks[:MAX_CHUNKS], True
            try:
                for chunk in chunks:
                    if await self._backend.positive(ref, chunk):
                        verdict.positive = True
                        break
            except ClassifierUnavailableError as exc:
                verdict.unavailable = str(exc) or "the decision model could not answer"
            except Exception:
                # Whatever went wrong, "could not ask" is never "no".
                logger.warning("a classifier call failed", exc_info=True)
                verdict.unavailable = "the decision model could not be reached"
        self._verdicts[key] = verdict
        return verdict
