"""The engine that calls an out-of-process detector.

Speaks the contract in :mod:`llmp_shared.redaction` and nothing else, so any
service that serves ``POST /detect`` is a valid detector regardless of the model
behind it (ADR 0026). Presidio in
``services/redaction`` is a reference implementation, not a dependency.

The division of labour matters and is from 0012:
the detector returns **spans only**. Placeholders are derived here, by keyed HMAC,
so a detector can never see the key, never break placeholder stability across
turns, and can be swapped without re-labelling anything already shown to a user.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
from collections import Counter, OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import httpx
from llmp_shared import (
    MAX_CUSTOM_LABELS,
    CustomLabel,
    DetectionRequest,
    DetectionResponse,
    EntitySpan,
    PlaceholderMap,
    Restored,
    opaque_placeholder,
    placeholder_for,
    same_length_placeholder,
)
from pydantic import ValidationError

from gateway.config import (
    DEFAULT_REDACTION_POLICY,
    EffectivePolicy,
    EntityMode,
    HidingRule,
    NerAsk,
    PlaceholderStyle,
    RedactionPolicy,
    RedactionSettings,
    hides,
)
from gateway.errors import ContentBlockedError, GatewayError, ServiceUnavailableError
from gateway.models import ApiSurface
from gateway.redaction.base import RedactionOutcome, TextRewriteStage
from gateway.redaction.keywords import keyword_spans
from gateway.redaction.note import system_note
from gateway.redaction.report import (
    SOURCE_MESSAGE,
    SOURCE_TOOL_RESULT,
    EntityFinding,
    build_detail,
    record,
)
from gateway.sse.pipeline import StreamStage, passthrough

logger = logging.getLogger(__name__)

#: Types the spaCy recogniser's label map once advertised but no stock model ever emits.
_LEGACY_INERT = frozenset({"AGE", "ID", "EMAIL"})

# Roles whose content is sent for detection. Everything the *user* side of the
# conversation contributed, plus prior assistant turns — a name the model repeated
# in turn 3 is still in the transcript the upstream receives in turn 4.
_TEXT_ROLES = frozenset({"system", "user", "assistant", "tool", "developer"})


class RedactionUnavailableError(GatewayError):
    """The detector could not be reached and fail_open is off."""

    status_code = 502
    error_type = "server_error"
    code = "redaction_unavailable"


#: Stable codes for why the service did not answer. Stored on the refused
#: request's row, so the admin console's request list can say which.
REDACTION_TIMEOUT = "redaction_timeout"
REDACTION_UNREACHABLE = "redaction_unreachable"


class EngineUnavailableError(Exception):
    """The service has no such NER engine now (409 ``engine_unavailable``).

    Not a :class:`GatewayError`: the redactor does not know which policy chose
    the engine, so the caller turns it into the 503 that names the policy.
    """

    def __init__(self, engine: str | None, reason: str | None = None) -> None:
        super().__init__(reason or f"NER engine {engine or '(default)'} is not available")
        self.engine = engine
        #: Why, when it is not simply "the service has no such engine".
        self.reason = reason


class _Cache:
    """Bounded LRU over detection results, keyed by a hash of the text.

    The reason this exists: on turn 40 the client resends all 40 messages, so a
    naive implementation re-runs inference over the whole history every turn —
    quadratic in conversation length, against the slowest thing in the request.

    Safe because detection is a pure function of the text and the engine's
    configuration, and because the *placeholder* derivation is stateless and
    happens afterwards. A cache hit therefore cannot change any output; it can
    only skip work. Stale entries are bounded by the process lifetime, which is
    the right trade for a detector whose model changes on redeploys.
    """

    def __init__(self, capacity: int) -> None:
        self._capacity = max(0, capacity)
        self._entries: OrderedDict[str, list[EntitySpan]] = OrderedDict()

    @staticmethod
    def key(
        text: str,
        language: str,
        threshold: float,
        types: list[str] | None,
        pattern_matching: bool | None,
        ner: bool | None,
        engine: str | None = None,
        labels: tuple[tuple[str, str], ...] = (),
    ) -> str:
        # JSON, not a joined string: it keeps None apart from [] (``types=None``
        # is "everything the detector knows", ``[]`` is "nothing enabled", and
        # a text cached as clean under the second must not be served for the
        # first, or it goes upstream unredacted) and it cannot be confused by a
        # separator appearing inside a value.
        # The engine and the labels are part of the question: the same text asked of
        # spaCy and of GLiNER, or with another label, is another answer.
        parts = json.dumps(
            [language, threshold, types, pattern_matching, ner, text, engine, labels]
        )
        return hashlib.sha256(parts.encode()).hexdigest()

    def get(self, key: str) -> list[EntitySpan] | None:
        if key not in self._entries:
            return None
        self._entries.move_to_end(key)
        return self._entries[key]

    def put(self, key: str, spans: list[EntitySpan]) -> None:
        if not self._capacity:
            return
        self._entries[key] = spans
        self._entries.move_to_end(key)
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)

    def __len__(self) -> int:
        return len(self._entries)


#: A placeholder already in a text: left alone by the known-value pass, so an
#: original that happens to appear inside one is not rewritten inside it.
_PLACEHOLDER_SPAN = r"<[A-Z][A-Z0-9_]*>|#[A-Z2-7]{6,}#"

#: The shortest original the known-value pass will look for. Shorter strings
#: are too likely to be ordinary words to replace everywhere.
_KNOWN_MIN = 3


def _known_values(issued: dict[str, str]) -> re.Pattern[str] | None:
    """A pattern for every original substituted in this request, or None.

    Longest first, whole words only, and a placeholder already in the text is
    matched first so it is skipped rather than rewritten inside.
    """
    originals = sorted((o for o in issued if len(o) >= _KNOWN_MIN), key=len, reverse=True)
    if not originals:
        return None
    alternatives = "|".join(re.escape(o) for o in originals)
    return re.compile(rf"(?P<skip>{_PLACEHOLDER_SPAN})|(?<!\w)(?P<known>{alternatives})(?!\w)")


#: Request items that are not messages and carry no role: the Responses API's
#: function calls, their outputs and reasoning items.
_ITEM_TYPES = frozenset({"function_call", "function_call_output", "reasoning"})

#: Where a model's reasoning is replayed in a conversation history.
_REASONING_KEYS = ("reasoning_content", "reasoning", "reasoning_text")


class _JsonText:
    """A JSON document held as text, whose string values are redacted one by one.

    A tool call's arguments are JSON text. Redacting the text as it stands would
    put a placeholder next to a quote or a backslash and could split a value
    the detector sees whole, and restoring puts original values back into
    arguments — so on the way in they are redacted the same way: decode, redact
    every string value, serialise again. Written back only when something changed,
    so an untouched call goes out byte for byte as it came.
    """

    __slots__ = ("changed", "holder", "key", "root")

    def __init__(self, holder: dict[str, Any], key: str, root: Any) -> None:
        self.holder = holder
        self.key = key
        self.root = root
        self.changed = False

    def commit(self) -> None:
        if self.changed:
            self.holder[self.key] = json.dumps(self.root, ensure_ascii=False)


class _Target:
    """One string that is sent for detection, and where it lives."""

    __slots__ = ("document", "holder", "key", "source", "text")

    def __init__(
        self,
        holder: Any,
        key: Any,
        document: _JsonText | None = None,
        source: str = SOURCE_MESSAGE,
    ) -> None:
        self.holder = holder
        self.key = key
        self.text: str = holder[key]
        self.document = document
        #: Where the string came from, for the redaction log only.
        self.source = source

    def write(self, text: str) -> None:
        self.holder[self.key] = text
        if self.document is not None:
            self.document.changed = True


def _text_targets(
    holder: dict[str, Any], key: str, out: list[_Target], source: str = SOURCE_MESSAGE
) -> None:
    if isinstance(holder.get(key), str) and holder[key]:
        out.append(_Target(holder, key, source=source))


def _json_leaves(
    node: Any,
    document: _JsonText | None,
    out: list[_Target],
    source: str = SOURCE_MESSAGE,
) -> None:
    """Every string value in a decoded JSON document (keys are not values)."""
    if isinstance(node, dict):
        children: Any = list(node.keys())
    elif isinstance(node, list):
        children = range(len(node))
    else:
        return
    for key in children:
        value = node[key]
        if isinstance(value, str):
            if value:
                out.append(_Target(node, key, document, source))
        else:
            _json_leaves(value, document, out, source)


def _json_text_targets(
    holder: dict[str, Any], key: str, out: list[_Target], source: str = SOURCE_MESSAGE
) -> None:
    """Targets for a field that holds JSON as text, or plain text if it is not."""
    raw = holder.get(key)
    if not isinstance(raw, str) or not raw:
        return
    try:
        root = json.loads(raw)
    except ValueError:
        root = None
    if isinstance(root, dict | list):
        _json_leaves(root, _JsonText(holder, key, root), out, source)
        return
    # Not JSON (a model can emit a broken call): the text itself is screened.
    out.append(_Target(holder, key, source=source))


def _content_targets(
    holder: dict[str, Any], key: str, out: list[_Target], source: str = SOURCE_MESSAGE
) -> None:
    """A ``content``-like field: a string, or a list of blocks.

    Blocks may be text, a Messages ``tool_result`` (whose own ``content`` is a
    string or more blocks) or a ``tool_use`` (whose ``input`` is an object).
    """
    value = holder.get(key)
    if isinstance(value, str):
        _text_targets(holder, key, out, source)
        return
    if not isinstance(value, list):
        return
    for part in value:
        if not isinstance(part, dict):
            continue
        _text_targets(part, "text", out, source)
        if part.get("type") == "tool_result":
            _content_targets(part, "content", out, SOURCE_TOOL_RESULT)
        if part.get("type") == "tool_use" and isinstance(part.get("input"), dict | list):
            _json_leaves(part["input"], None, out, source)


def _message_targets(message: dict[str, Any]) -> list[_Target]:
    """Every string in *message* that must be screened before it is sent.

    The caller's own text, and also what *we* put there on an earlier turn: a
    follow-up request echoes the assistant's tool calls and reasoning, and since
    those are restored on the way out, the echo carries the real values back in.
    Leaving them unscreened would send them to the provider.
    """
    out: list[_Target] = []
    kind = message.get("type")
    if message.get("role") not in _TEXT_ROLES and kind not in _ITEM_TYPES:
        return out
    _content_targets(
        message,
        "content",
        out,
        SOURCE_TOOL_RESULT if message.get("role") == "tool" else SOURCE_MESSAGE,
    )
    for key in _REASONING_KEYS:
        _text_targets(message, key, out)
    for call in message.get("tool_calls") or []:
        function = call.get("function") if isinstance(call, dict) else None
        if isinstance(function, dict):
            _json_text_targets(function, "arguments", out)
    if kind == "function_call":
        _json_text_targets(message, "arguments", out)
    elif kind == "function_call_output":
        _content_targets(message, "output", out, SOURCE_TOOL_RESULT)
    elif kind == "reasoning":
        for field in ("summary", "content"):
            for block in message.get(field) or []:
                if isinstance(block, dict):
                    _text_targets(block, "text", out)
    return out


@lru_cache(maxsize=4096)
def _compiled(pattern: str) -> Any:
    """One compile per distinct pattern per process.

    Compiling is cheap and doing it per request is still waste on the hottest
    path there is. Bounded rather than unbounded because the key is operator
    input: a deployment editing rules in a loop must not grow this without end.
    The bound is generous because users' own patterns (25 each) share it: a
    small one would thrash across many active users, recompiling on the hot path.
    """
    import re2

    return re2.compile(pattern)


PatternKey = tuple[str, str]


def pattern_spans(
    text: str,
    policy: RedactionPolicy,
    *,
    only: frozenset[PatternKey] | None = None,
    skip: frozenset[PatternKey] = frozenset(),
) -> list[EntitySpan]:
    """Spans for the operator's own regexes.

    Produced in the gateway rather than asked of the detection service: no
    contract change, it works for whatever engine is installed, and one
    deployment's regexes have no business being installed into a service every
    deployment shares. They also stay *outside* the detection cache, since a
    regex is deterministic and cheap — caching them would fragment a key whose
    hit rate is worth 10x on a long conversation (docs/performance.md).

    Score 1.0: a pattern somebody wrote by hand is not a guess, so it outranks
    a model's opinion when the two overlap.

    ``skip`` and ``only`` select patterns by ``(name, regex)``: a user's own
    patterns are matched apart from the rest (:func:`eligible_for`) so that their
    spans can only be added, never displace an administrator's.
    """
    spans: list[EntitySpan] = []
    for pattern in policy.patterns:
        if pattern.mode is EntityMode.OFF:
            continue
        key = (pattern.name, pattern.regex)
        if key in skip or (only is not None and key not in only):
            continue
        for match in _compiled(pattern.regex).finditer(text):
            start, end = match.span()
            if end > start:
                spans.append(
                    EntitySpan(
                        start=start,
                        end=end,
                        entity_type=pattern.name,
                        score=1.0,
                        engine="pattern",
                    )
                )
    for item in policy.keywords:
        if item.mode is EntityMode.OFF:
            continue
        key = (item.name, "\x1f".join(item.words))
        if key in skip or (only is not None and key not in only):
            continue
        spans.extend(keyword_spans(text, item))
    return spans


def eligible_spans(
    text: str,
    spans: list[EntitySpan],
    policy: RedactionPolicy,
    default_threshold: float,
    *,
    acting: bool = True,
) -> list[EntitySpan]:
    """The spans the policy acts on: not off, over its threshold, not allow-listed.

    ``acting`` leaves out ``detect_only`` items, which are found and never hidden
    (and so must not take part in overlap resolution or the substitution). A
    condition that counts items passes ``acting=False``.
    """
    return [
        span
        for span in spans
        if (hides if acting else _found)(policy.mode_for(span.entity_type))
        and span.score >= policy.threshold_for(span.entity_type, default_threshold)
        and not policy.allows(span.slice_of(text))
    ]


def _found(mode: EntityMode) -> bool:
    return mode is not EntityMode.OFF


def _overlaps(a: EntitySpan, b: EntitySpan) -> bool:
    return a.start < b.end and b.start < a.end


def eligible_for(
    text: str,
    detected: list[EntitySpan],
    policy: RedactionPolicy,
    default_threshold: float,
    user_patterns: frozenset[PatternKey] = frozenset(),
    *,
    acting: bool = True,
) -> list[EntitySpan]:
    """Every span the policy acts on, with a user's own patterns held to "add only".

    Overlap resolution prefers the higher score, and a pattern span scores 1.0,
    so a user's pattern (``ar``, or ``\\b\\w``) would beat the detector's
    ``Mario Rossi`` at 0.85 and shrink an administrator's redaction to the
    letters the user chose, leaving the rest of the name to the provider. So a
    user-owned span that overlaps any other eligible span is **dropped**: the
    administrators' span stands whole, in the administrators' mode. A user span
    that overlaps nothing adds protection, which is all a user rule is for.
    Spans from several user patterns still compete with each other as usual.
    """
    base = eligible_spans(
        text,
        [*detected, *pattern_spans(text, policy, skip=user_patterns)],
        policy,
        default_threshold,
        acting=acting,
    )
    if not user_patterns:
        return base
    mine = eligible_spans(
        text,
        pattern_spans(text, policy, only=user_patterns),
        policy,
        default_threshold,
        acting=acting,
    )
    return [*base, *(span for span in mine if not any(_overlaps(span, other) for other in base))]


def resolve_overlaps(text: str, eligible: list[EntitySpan]) -> list[EntitySpan]:
    """Keep the highest-scoring, then the longest, of any overlapping spans."""
    ordered = sorted(eligible, key=lambda span: (span.start, -span.score, -(span.end - span.start)))
    chosen: list[EntitySpan] = []
    for span in ordered:
        if span.start >= span.end or span.end > len(text):
            continue
        if chosen and span.start < chosen[-1].end:
            previous = chosen[-1]
            better = (span.score, span.end - span.start) > (
                previous.score,
                previous.end - previous.start,
            )
            if better:
                chosen[-1] = span
            continue
        chosen.append(span)
    return chosen


@dataclass(slots=True)
class Claim:
    """A span a rule hides, and how: the first rule to hide a span decides (ADR 0114)."""

    span: EntitySpan
    mode: EntityMode
    style: PlaceholderStyle
    rule: int


@dataclass(slots=True)
class Claims:
    #: What is hidden, left to right, no two overlapping.
    chosen: list[Claim] = field(default_factory=list)
    #: Found by a rule that only records (``detect_only``): counted, never hidden.
    observed: list[tuple[EntitySpan, int]] = field(default_factory=list)
    #: Types a rule's override blocks, found in the text: ``(span, rule index)``.
    blocked: list[tuple[EntitySpan, int]] = field(default_factory=list)


def claim_spans(
    text: str,
    detected: list[EntitySpan],
    rules: Sequence[HidingRule],
    default_threshold: float,
    *,
    user_patterns: frozenset[PatternKey] = frozenset(),
) -> Claims:
    """Which rule hides which span of *text*, from one detection pass over the original.

    Rules are taken in order. Each rule's policies find spans (their own thresholds and
    allow-lists); spans that overlap text an earlier rule already claimed are dropped, so
    **the first rule to hide a span decides how**, and the rest of the rule's spans are
    resolved by score and length as ever. ``off`` and ``detect_only`` never claim: the
    latter is only recorded. Nothing is detected again on placeholder text.
    """
    claims = Claims()
    taken: list[EntitySpan] = []
    for index, rule in enumerate(rules):
        modes: dict[int, tuple[EntityMode, PlaceholderStyle]] = {}
        acting: list[EntitySpan] = []
        for part in rule.parts:
            for span in eligible_for(
                text, detected, part, default_threshold, user_patterns, acting=False
            ):
                mode = part.mode_for(span.entity_type)
                if mode is EntityMode.BLOCK:
                    claims.blocked.append((span, index))
                elif mode is EntityMode.DETECT_ONLY:
                    if not any(_overlaps(span, other) for other in taken):
                        claims.observed.append((span, index))
                elif hides(mode):
                    modes[id(span)] = (mode, part.placeholder_style_for(span.entity_type))
                    acting.append(span)
        free = [s for s in acting if not any(_overlaps(s, other) for other in taken)]
        for span in resolve_overlaps(text, free):
            mode, style = modes[id(span)]
            claims.chosen.append(Claim(span, mode, style, index))
            taken.append(span)
    claims.chosen.sort(key=lambda c: c.span.start)
    return claims


def apply_spans(
    text: str,
    spans: list[EntitySpan],
    *,
    key: bytes,
    placeholders: PlaceholderMap,
    policy: RedactionPolicy | None = None,
    default_threshold: float = 0.0,
    scope: str | None = None,
    tally: Counter[str] | None = None,
    issued: dict[str, str] | None = None,
    findings: dict[str, EntityFinding] | None = None,
    source: str = SOURCE_MESSAGE,
    issued_types: dict[str, str] | None = None,
    user_patterns: frozenset[PatternKey] = frozenset(),
    rules: Sequence[HidingRule] | None = None,
    issued_modes: dict[str, str] | None = None,
) -> tuple[str, int]:
    """Substitute placeholders for *spans* in *text*, according to *policy* or *rules*.

    Right to left, so each replacement leaves earlier offsets valid — rewriting
    left to right invalidates every span after the first.

    **Block first, then hide in rule order** (ADR 0114): a type a rule blocks that is found
    refuses the request before any text is rewritten; otherwise each rule hides what its
    policies find, using its mode, and the first rule to hide a span decides how
    (:func:`claim_spans`). With *policy* alone it is one rule, which is what every
    redaction did before rules.

    Overlapping spans are resolved by keeping the highest-scoring, then the longest. Two
    detectors disagreeing about the same characters is normal (an IBAN is also a long
    number), and replacing one inside another produces corrupted text rather than a
    redaction. **A span a rule's policy discards must not have suppressed the one it
    overlapped**: eligibility comes first (a ``URL`` set to off, covering the same
    characters as a ``PERSON``, would otherwise take the person with it).

    What each mode writes, and what it records:

    ``off``                 nothing; the text is left as the caller wrote it.
    ``detect_only``         nothing; the item is recorded in the log.
    ``anonymise_restore``   a derived placeholder, remembered, so the answer can
                            have the real value put back.
    ``anonymise``           the same placeholder, *not* remembered — so the
                            reader sees the placeholder too.
    ``redact``              ``<PERSON>``: no token, nothing to remember, and two
                            people become the same label.
    """
    held = (
        rules
        if rules is not None
        else (HidingRule(None, "", "admin", (policy or DEFAULT_REDACTION_POLICY,)),)
    )
    claims = claim_spans(text, spans, held, default_threshold, user_patterns=user_patterns)
    if findings is not None:
        for span, _ in claims.observed:
            record(findings, span.entity_type, mode=EntityMode.DETECT_ONLY.value, source=source)
    # Before any rewriting: a blocked request produces no redacted text at all,
    # and deciding this after substitution would mean building a placeholder map
    # for a request that is about to be refused.
    if claims.blocked:
        kinds = sorted({span.entity_type for span, _ in claims.blocked})
        if findings is not None:
            # What the refusal was about, counted by type and never quoted: the
            # caller attaches it to the error so the log can say which rule.
            for span, _ in claims.blocked:
                record(findings, span.entity_type, mode=EntityMode.BLOCK.value, source=source)
        first = held[claims.blocked[0][1]]
        where = (
            f" by the rule '{first.name}'"
            if first.name
            else (f" by the {scope} policy" if scope else "")
        )
        raise ContentBlockedError(
            f"This request was refused{where}: it contains "
            f"{', '.join(kinds)}, which this deployment does not send to a provider."
        )
    chosen = claims.chosen
    if not chosen:
        return text, 0

    # What the caller wrote, before any of our own substitutions: a stand-in is
    # compared against that, not against text this loop has already rewritten,
    # or the second occurrence of one value would collide with the first.
    written = text
    for claim in reversed(chosen):
        span, mode = claim.span, claim.mode
        original = span.slice_of(text)
        if mode is EntityMode.REDACT:
            placeholder = opaque_placeholder(span.entity_type)
        else:
            placeholder = _derived_placeholder(
                span.entity_type,
                original,
                key=key,
                style=claim.style,
                placeholders=placeholders,
                text=written,
                tally=tally,
            )
            if mode is EntityMode.ANONYMISE_RESTORE:
                # The map is the only thing that makes a placeholder reversible,
                # so *not* adding is how "anonymise" differs from "anonymise and
                # restore". RestoreStage leaves what it did not issue alone.
                placeholders.add(span.entity_type, original, placeholder)
        text = text[: span.start] + placeholder + text[span.end :]
        if tally is not None:
            tally[mode.value] += 1
        if issued is not None:
            issued[original] = placeholder
        if issued_types is not None:
            issued_types[original] = span.entity_type
        if issued_modes is not None:
            issued_modes[original] = mode.value
        if findings is not None:
            record(findings, span.entity_type, mode=mode.value, source=source)

    return text, len(chosen)


#: The tally key for stand-ins written as ``#TOKEN#``. Not a mode, so it cannot
#: collide with one.
SAME_LENGTH_KEY = "same_length"


def _derived_placeholder(
    entity_type: str,
    original: str,
    *,
    key: bytes,
    style: PlaceholderStyle,
    placeholders: PlaceholderMap,
    text: str,
    tally: Counter[str] | None,
) -> str:
    """The stand-in for *original*: ``#TOKEN#`` when asked and safe, else tagged.

    Safe means the value is long enough, that no other value of this request
    already holds the same ``#TOKEN#``, and that it does not already occur in
    the text around it — in either case restoring it later could not tell which
    was meant. Falling back is silent on purpose: this is best effort, and the
    tagged form always works.
    """
    if style is PlaceholderStyle.SAME_LENGTH:
        candidate = same_length_placeholder(entity_type, original, key=key)
        if (
            candidate is not None
            and candidate not in text
            and placeholders.claim(candidate, original)
        ):
            if tally is not None:
                tally[SAME_LENGTH_KEY] += 1
            return candidate
    return placeholder_for(entity_type, original, key=key)


#: What a placeholder gains when JSON spells both its angle brackets as
#: ``\\u003c`` and ``\\u003e``: five characters each.
ESCAPED_ANGLE_SLACK = 10


class RestoreStage(TextRewriteStage):
    """Puts the real values back into a streamed response.

    ``tail_size`` is **derived, not guessed**: the only thing this transform
    matches is a placeholder we ourselves emitted, and their lengths are known
    exactly. Holding back one character less than the longest of them is
    sufficient and no more than necessary.
    """

    def __init__(
        self,
        placeholders: PlaceholderMap,
        *,
        tail_size: int,
        surface: ApiSurface = ApiSurface.CHAT_COMPLETIONS,
        arguments: bool = False,
    ) -> None:
        super().__init__(
            tail_size=tail_size,
            surface=surface,
            arguments=arguments,
            arguments_slack=ESCAPED_ANGLE_SLACK if arguments else 0,
        )
        self._placeholders = placeholders

    def transform(self, text: str, *, final: bool) -> str:
        return self._placeholders.restore(text)

    def transform_arguments(self, text: str, *, final: bool) -> str:
        """Restore inside JSON text, writing each original JSON-escaped.

        Per fragment, with the same tail rule as prose: the placeholder is
        plain text inside the JSON string, so it can straddle two fragments
        exactly as it can straddle two chunks of an answer.
        """
        return self._placeholders.restore_in_json(text)

    def edits_in(self, text: str) -> Restored:
        return self._placeholders.restore_with_edits(text)


def _engine_unavailable(response: httpx.Response) -> bool:
    """Whether a 409 is the service saying the NER engine asked for is not available."""
    try:
        detail = response.json().get("detail")
    except ValueError:
        return False
    return isinstance(detail, dict) and detail.get("code") == "engine_unavailable"


def _engine_busy(response: httpx.Response) -> bool:
    """Whether a 503 is the service saying the engine's queue is full (``engine_busy``)."""
    try:
        detail = response.json().get("detail")
    except ValueError:
        return False
    return isinstance(detail, dict) and detail.get("code") == "engine_busy"


class HttpDetectionRedactor:
    """Redaction backed by an HTTP detection service."""

    name = "http"

    def __init__(self, settings: RedactionSettings, *, client: httpx.AsyncClient | None = None):
        if not settings.endpoint:
            raise ValueError("GATEWAY_REDACTION__ENDPOINT must be set when the engine is 'http'.")
        key = settings.placeholder_key.get_secret_value()
        if not key:
            raise ValueError(
                "GATEWAY_REDACTION__PLACEHOLDER_KEY must be set when the engine is 'http'. "
                "It is the HMAC key placeholders are derived from, and it must stay stable "
                "for as long as the transcripts it labelled are kept."
            )

        self._settings = settings
        self._key = key.encode()
        self._endpoint = settings.endpoint.rstrip("/") + "/detect"
        self._owns_client = client is None
        # A finite read timeout, unlike the upstream client: a detector that hangs
        # must not hang the request behind it.
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(settings.timeout_seconds))
        self._cache = _Cache(settings.cache_size)
        # The engine-entity mismatch report, deduplicated: the detector names
        # requested types it cannot serve in every response, and logging that
        # per request would bury the first occurrence under the ten-thousandth.
        # Logged when the set changes, which is exactly the event an operator
        # needs to see — the deploy or the rule edit that made a rule inert.
        self._last_unsupported: frozenset[str] | None = None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- detection ---------------------------------------------------------

    async def detect(
        self,
        texts: list[str],
        *,
        policy: RedactionPolicy | None = None,
        asks: Sequence[NerAsk] | None = None,
        strict: bool = False,
    ) -> list[list[EntitySpan]]:
        """Spans for each text from the service, cached, one call per engine asked.

        *asks* is what ``EffectivePolicy.asks`` says the request needs: policies
        that chose different NER engines are asked of the service one engine at a
        time and the spans merged (one span per identical range and type, the
        higher score kept). Without it the policy asks for itself.

        ``strict`` ignores ``fail_open``: a condition that counts items must not
        read an outage as "found nothing" (ADR 0113, section 0). An engine the
        service reports unavailable raises :class:`EngineUnavailableError`.
        """
        settings = self._settings
        active = policy or settings.policy
        # The lowest bar any enabled type sets. Filtering to each type's own
        # threshold happens in apply_spans: asking the detector for the strictest
        # would drop spans a laxer type still wants.
        floor = min(
            [settings.score_threshold]
            + [
                entry.threshold
                for entry in active.service_types().values()
                if entry.threshold is not None and entry.mode is not EntityMode.OFF
            ]
        )
        calls = list(asks) if asks else active.ner_asks()
        merged: list[dict[tuple[int, int, str], EntitySpan]] = [{} for _ in texts]
        for ask in calls:
            for index, spans in enumerate(await self._detect_ask(texts, ask, floor, strict)):
                for span in spans:
                    key = (span.start, span.end, span.entity_type)
                    held = merged[index].get(key)
                    if held is None or span.score > held.score:
                        merged[index][key] = span
        return [sorted(found.values(), key=lambda s: (s.start, s.end)) for found in merged]

    async def _detect_ask(
        self, texts: list[str], ask: NerAsk, floor: float, strict: bool
    ) -> list[list[EntitySpan]]:
        settings = self._settings
        # What the policy will actually act on, not what the engine can find.
        # Narrowing here is free detection time when an admin has enumerated the
        # set; when they have not, it is None and everything comes back to be
        # filtered locally — an engine's entity list is its own and cannot be
        # enumerated from here (ADR 0026).
        wanted = list(ask.types) if ask.types is not None else None
        ner_on = settings.presidio_ner
        engine_off = {
            "spacy": settings.spacy_enabled is False,
            "gliner": settings.gliner_enabled is False,
        }
        if ask.engine is not None and engine_off.get(ask.engine):
            # The policy chose an engine the administrator switched off: fail closed, and the
            # caller turns this into the 503 that names the policy.
            raise EngineUnavailableError(
                ask.engine, f"the NER engine {ask.engine} is switched off on the Detectors tab"
            )
        if ask.engine is None and any(engine_off.values()):
            default = await self._default_engine()
            if default is None or engine_off.get(default, False):
                # The service would run its default engine: tell it not to run NER at all,
                # so what needs it comes back unsupported and is refused below.
                ner_on = False
        keys = [
            _Cache.key(
                text,
                settings.language,
                floor,
                wanted,
                settings.presidio_pattern_matching,
                ner_on,
                ask.engine,
                ask.labels,
            )
            for text in texts
        ]
        results: list[list[EntitySpan] | None] = [self._cache.get(key) for key in keys]

        pending = [index for index, spans in enumerate(results) if spans is None]
        if not pending:
            return [spans or [] for spans in results]

        try:
            request = DetectionRequest(
                texts=[texts[index] for index in pending],
                language=settings.language,
                score_threshold=floor,
                entity_types=wanted,
                presidio_pattern_matching=settings.presidio_pattern_matching,
                presidio_ner=ner_on,
                ner_engine=ask.engine,
                custom_labels=[CustomLabel(entity=e, label=label) for e, label in ask.labels]
                or None,
            )
        except ValidationError as exc:
            # The policies in force together ask for more than the service takes (for
            # example more than 30 custom labels for one engine): a named 503, never a 500.
            raise EngineUnavailableError(
                ask.engine,
                "the policies in force ask the NER engine for more than it accepts "
                f"({len(ask.labels)} custom entity types; the limit is {MAX_CUSTOM_LABELS})",
            ) from exc
        found, partial = await self._post(request, strict=strict)

        for position, index in enumerate(pending):
            spans = [] if found is None else found.get(position, [])
            results[index] = spans
            # A fail-open outage yields no findings, which must not be cached:
            # one outage would otherwise leave every text seen during it marked
            # "clean" for the life of the process.
            # Nor an answer that left types out (the engine could not serve them): a later
            # strict call must see the report again.
            if found is not None and not partial:
                self._cache.put(keys[index], spans)

        return [spans or [] for spans in results]

    async def _healthz(self) -> dict[str, Any] | None:
        """The service's ``/healthz`` body, cached for 30 seconds (None when unreadable)."""
        import time

        held = getattr(self, "_healthz_seen", None)
        if held is not None and time.monotonic() - held[0] < 30:
            return held[1]  # type: ignore[no-any-return]
        try:
            base = self._endpoint.removesuffix("/detect")
            body = (await self._client.get(f"{base}/healthz")).json()
        except Exception:
            logger.info("could not read the redaction service's capabilities", exc_info=True)
            return None
        found = body if isinstance(body, dict) else None
        self._healthz_seen = (time.monotonic(), found)
        return found

    async def _default_engine(self) -> str | None:
        """The service's default NER engine (None when unknown)."""
        body = await self._healthz()
        for engine in (body or {}).get("ner_engines") or []:
            if isinstance(engine, dict) and engine.get("default"):
                return str(engine.get("name"))
        return None

    async def _listed_types(self) -> set[str] | None:
        """Every type the service lists in either family (None when it cannot be asked)."""
        body = await self._healthz()
        if body is None:
            return None
        listed = {str(t).upper() for t in body.get("pattern_entities") or []}
        for engine in body.get("ner_engines") or []:
            if isinstance(engine, dict):
                listed.update(str(t).upper() for t in engine.get("entities") or [])
        return listed

    async def _post(
        self, request: DetectionRequest, *, strict: bool = False
    ) -> tuple[dict[int, list[EntitySpan]] | None, bool]:
        """Findings by position (None when failing open on an outage), and whether the
        engine reported types it could not serve."""
        try:
            response = await self._client.post(self._endpoint, json=request.model_dump(mode="json"))
            if response.status_code == 409 and _engine_unavailable(response):
                raise EngineUnavailableError(request.ner_engine)
            if response.status_code == 503 and _engine_busy(response):
                raise EngineUnavailableError(
                    request.ner_engine, "the engine is busy; try again shortly"
                )
            response.raise_for_status()
            parsed = DetectionResponse.model_validate(response.json())
        except (TimeoutError, httpx.HTTPError, ValueError) as exc:
            if self._settings.fail_open and not strict:
                # Loud, because this is the state in which the gateway is
                # forwarding unredacted prompts while still reporting an engine.
                logger.error(
                    "detection service unavailable (%s; %s characters, engine %s); "
                    "FAILING OPEN and forwarding the prompt unredacted",
                    str(exc) or type(exc).__name__,
                    f"{sum(len(t) for t in request.texts):,}",
                    request.ner_engine or "default",
                    exc_info=True,
                )
                return None, False
            # A connect timeout means the service was never reached, not that it was slow
            timed_out = isinstance(exc, (TimeoutError, httpx.TimeoutException)) and not isinstance(
                exc, (httpx.ConnectTimeout, httpx.PoolTimeout)
            )
            # An httpx timeout has an empty str(): name the type instead. The size and
            # engine are logged, never the text.
            reason = str(exc) or type(exc).__name__
            characters = sum(len(t) for t in request.texts)
            size = f"{characters:,} characters, {len(request.texts):,} items"
            engine = request.ner_engine or "default"
            if timed_out:
                logger.error(
                    "screening timed out after %g s (%s, engine %s); refusing the request",
                    self._settings.timeout_seconds,
                    size,
                    engine,
                )
                raise RedactionUnavailableError(
                    "The redaction service timed out "
                    f"(no answer within {self._settings.timeout_seconds:g} s), so this request "
                    "cannot be screened. It is refused rather than forwarded unredacted.",
                    code=REDACTION_TIMEOUT,
                ) from exc
            logger.error(
                "detection service unavailable (%s; %s, engine %s); refusing the request",
                reason,
                size,
                engine,
            )
            raise RedactionUnavailableError(
                "The redaction service could not be reached, so this request cannot be "
                "screened. It is refused rather than forwarded unredacted.",
                code=REDACTION_UNREACHABLE,
            ) from exc

        found = {finding.index: finding.spans for finding in parsed.findings}
        self._report_unsupported(parsed.unsupported_types)
        family_off = request.presidio_pattern_matching is False or request.presidio_ner is False
        unsupported = list(parsed.unsupported_types)
        if unsupported and family_off and not strict:
            # A type the service lists in neither family (AGE, ID and EMAIL were once advertised
            # by the spaCy recogniser's label map and never detected anything) is not
            # protection lost to a switch: refusing the request for it would be an outage that
            # buys nothing. Said in the log, once per change, instead.
            # Only the legacy set: a custom GLiNER entity, or a model type whose engine slot is
            # unavailable, is also absent from /healthz's lists, and must stay refused.
            listed = await self._listed_types() or set()
            inert = [
                t for t in unsupported if t.upper() in _LEGACY_INERT and t.upper() not in listed
            ]
            if inert:
                logger.warning(
                    "ignoring policy type(s) %s: the redaction service finds them in no family, "
                    "so no switch is what keeps them from being detected",
                    ", ".join(sorted(inert)),
                )
                unsupported = [t for t in unsupported if t not in inert]
        if unsupported and (strict or family_off):
            # A condition that counts items cannot read "the engine cannot serve this
            # type" as "found nothing": that is undecided, and the caller fails closed.
            # So is a type a switched-off detector would have found: the administrator
            # switched it off, but a policy still asks for it.
            raise EngineUnavailableError(
                request.ner_engine,
                (
                    "a detector that is switched off cannot find: "
                    if family_off and not strict
                    else "the engine cannot find: "
                )
                + ", ".join(sorted(unsupported)),
            )
        return found, bool(parsed.unsupported_types)

    def _report_unsupported(self, unsupported: list[str]) -> None:
        """Name the policy types this engine cannot serve, when it changes.

        The detector filters an unservable enumeration rather than raising, so
        this log is the only thing standing between "PERSON is protected" and
        "PERSON never reaches the detector" — the silent kind of under-
        protection, which no 502 and no failed request will ever reveal.
        """
        if not unsupported:
            return
        named = frozenset(unsupported)
        if named == self._last_unsupported:
            return
        self._last_unsupported = named
        logger.warning(
            "the detection engine cannot serve entity type(s) %s — rules protecting "
            "them are inert against this engine until the engine or the policy changes",
            ", ".join(sorted(named)),
        )

    # -- Redactor ----------------------------------------------------------

    async def redact_request(
        self, messages: list[dict[str, Any]], *, policy: EffectivePolicy | None = None
    ) -> RedactionOutcome:
        # One redactor is shared by every request and owns the connection pool
        # and the detection LRU (docs/performance.md); only the *policy* varies
        # per request. Building one redactor per scope would hand every request
        # a cold cache, which is the failure the resolver's own docstring names
        # for a ten-second rebuild — arriving once per request instead.
        effective = policy.policy if policy is not None else self._settings.policy
        # Copied, so a failure part-way cannot leave the caller's list half
        # rewritten — and so the transcript of what the user actually sent is
        # still available to the caller. Deep, because tool calls and content
        # blocks are rewritten in place below.
        rewritten = copy.deepcopy(messages)
        targets: list[_Target] = []
        for message in rewritten:
            targets.extend(_message_targets(message))
        texts = [target.text for target in targets]

        scope = policy.scope if policy is not None else None
        rule_id = policy.rule_id if policy is not None else None
        in_force = effective.is_active()
        user_patterns = policy.user_pattern_keys() if policy is not None else frozenset()
        note = system_note(
            effective,
            restore_text=self._settings.restore_in_response,
            restore_arguments=self._settings.restore_in_response
            and self._settings.restore_in_tool_arguments,
        )
        if not texts:
            return RedactionOutcome(
                messages=messages,
                engine=self.name,
                scope=scope,
                rule_id=rule_id,
                note=note,
                active=in_force,
            )

        try:
            found = await self.detect(
                texts,
                policy=effective,
                asks=(
                    policy.asks(acting=not policy.needs_observed()) if policy is not None else None
                ),
            )
        except EngineUnavailableError as exc:
            raise engine_unavailable_error(exc.engine, policy, exc.reason) from exc

        placeholders = PlaceholderMap()
        total = 0
        tally: Counter[str] = Counter()

        documents: dict[int, _JsonText] = {}
        issued: dict[str, str] = {}
        issued_types: dict[str, str] = {}
        issued_modes: dict[str, str] = {}
        findings: dict[str, EntityFinding] = {}
        hiding = policy.rules if policy is not None else ()
        for target, spans in zip(targets, found, strict=True):
            try:
                redacted, count = apply_spans(
                    target.text,
                    spans,
                    key=self._key,
                    placeholders=placeholders,
                    policy=effective,
                    default_threshold=self._settings.score_threshold,
                    scope=scope,
                    tally=tally,
                    issued=issued,
                    findings=findings,
                    source=target.source,
                    issued_types=issued_types,
                    user_patterns=user_patterns if not hiding else frozenset(),
                    rules=hiding or None,
                    issued_modes=issued_modes,
                )
            except ContentBlockedError as exc:
                exc.findings = findings
                raise
            total += count
            if count:
                target.write(redacted)
                if target.document is not None:
                    documents[id(target.document)] = target.document

        # A value found in one place is a value everywhere in this request. The
        # detector sees an echoed tool-call leaf or a reasoning fragment alone,
        # without the sentence that made it recognisable, and can miss a short
        # name it caught in the user's own turn. So every exact occurrence of an
        # original already substituted is replaced too, with the same stand-in.
        known = _known_values(issued)
        if known is not None:
            for target in targets:
                current = target.holder[target.key]
                hits = 0

                def swap(match: re.Match[str], source: str = target.source) -> str:
                    nonlocal hits
                    original = match.group("known")
                    if original is None:
                        return match.group(0)
                    hits += 1
                    kind = issued_types[original]
                    record(
                        findings,
                        kind,
                        mode=issued_modes.get(original, effective.mode_for(kind).value),
                        source=source,
                    )
                    return issued[original]

                swapped = known.sub(swap, current)
                if hits:
                    total += hits
                    target.write(swapped)
                    if target.document is not None:
                        documents[id(target.document)] = target.document
        for document in documents.values():
            document.commit()

        values = [
            {"type": issued_types[original], "original": original, "placeholder": placeholder}
            for original, placeholder in issued.items()
        ]

        return RedactionOutcome(
            messages=rewritten,
            placeholder_map=placeholders,
            entity_count=total,
            engine=self.name,
            scope=scope,
            rule_id=rule_id,
            note=note,
            restore_text=self._settings.restore_in_response,
            same_length_substitutions=tally[SAME_LENGTH_KEY],
            restore_arguments=self._settings.restore_in_response
            and self._settings.restore_in_tool_arguments,
            active=in_force,
            detail=build_detail(findings, policy),
            values=values,
        )

    def response_stage(
        self, outcome: RedactionOutcome, *, surface: ApiSurface = ApiSurface.CHAT_COMPLETIONS
    ) -> StreamStage:
        if not outcome.restore_text or not outcome.placeholder_map:
            # Nothing was replaced, so nothing can need restoring. Skips the
            # buffering and the per-frame JSON round trip entirely.
            return passthrough
        return RestoreStage(
            outcome.placeholder_map,
            tail_size=_longest_placeholder(outcome.placeholder_map),
            surface=surface,
            arguments=outcome.restore_arguments,
        )

    async def restore_response(self, text: str, outcome: RedactionOutcome) -> Restored:
        if not self._settings.restore_in_response:
            # Callers keep the placeholders, so nothing moved and any offsets
            # they hold are still the provider's own.
            return Restored(text=text)
        return outcome.placeholder_map.restore_with_edits(text)


def engine_unavailable_error(
    engine: str | None, policy: EffectivePolicy | None, reason: str | None = None
) -> ServiceUnavailableError:
    """The 503 for a policy whose NER engine the service no longer has (fail closed).

    Names the policies that chose it and the engine, never the text: a request is
    refused rather than screened by an engine the administrator did not choose.
    """
    names = policy.policies_of_engine(engine) if policy is not None else []
    which = ", ".join(f"'{n}'" for n in names) or "a redaction policy"
    return ServiceUnavailableError(
        f"The NER engine {engine or 'chosen by default'} for {which} cannot run"
        f"{': ' + reason if reason else ' (it is not available)'}, so "
        "this request cannot be screened. It is refused rather than let through.",
        code="redaction_engine_unavailable",
    )


def _longest_placeholder(placeholders: PlaceholderMap) -> int:
    """How much text a stream must hold back to never split a placeholder.

    One less than the longest placeholder: a match needs its final character to
    be complete, and holding the full length back would be one more than needed.
    """
    longest = max((len(text) for text in placeholders.placeholders()), default=0)
    return max(0, longest - 1)


def build(settings: RedactionSettings) -> HttpDetectionRedactor:
    return HttpDetectionRedactor(settings)


def screened_texts(messages: list[dict[str, Any]]) -> list[str]:
    """Every string redaction would screen in *messages*, one per input.

    What the privacy pipelines classify (ADR 0113): the same strings redaction
    reads, so a verdict is about the text that would otherwise leave, and each
    input is judged on its own and in full.
    """
    return [target.text for message in messages for target in _message_targets(message)]
