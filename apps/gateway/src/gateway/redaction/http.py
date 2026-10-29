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
from collections import Counter, OrderedDict
from functools import lru_cache
from typing import Any

import httpx
from llmp_shared import (
    DetectionRequest,
    DetectionResponse,
    EntitySpan,
    PlaceholderMap,
    Restored,
    opaque_placeholder,
    placeholder_for,
    same_length_placeholder,
)

from gateway.config import (
    DEFAULT_REDACTION_POLICY,
    EffectivePolicy,
    EntityMode,
    PlaceholderStyle,
    RedactionPolicy,
    RedactionSettings,
)
from gateway.errors import ContentBlockedError, GatewayError
from gateway.models import ApiSurface
from gateway.redaction.base import RedactionOutcome, TextRewriteStage
from gateway.redaction.note import system_note
from gateway.sse.pipeline import StreamStage, passthrough

logger = logging.getLogger(__name__)

# Roles whose content is sent for detection. Everything the *user* side of the
# conversation contributed, plus prior assistant turns — a name the model repeated
# in turn 3 is still in the transcript the upstream receives in turn 4.
_TEXT_ROLES = frozenset({"system", "user", "assistant", "tool", "developer"})


class RedactionUnavailableError(GatewayError):
    """The detector could not be reached and fail_open is off."""

    status_code = 502
    error_type = "server_error"
    code = "redaction_unavailable"


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
    ) -> str:
        parts = (
            f"{language}\x1f{threshold}\x1f{','.join(types or [])}\x1f"
            f"{pattern_matching}\x1f{ner}\x1f{text}"
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

    __slots__ = ("document", "holder", "key", "text")

    def __init__(self, holder: Any, key: Any, document: _JsonText | None = None) -> None:
        self.holder = holder
        self.key = key
        self.text: str = holder[key]
        self.document = document

    def write(self, text: str) -> None:
        self.holder[self.key] = text
        if self.document is not None:
            self.document.changed = True


def _text_targets(holder: dict[str, Any], key: str, out: list[_Target]) -> None:
    if isinstance(holder.get(key), str) and holder[key]:
        out.append(_Target(holder, key))


def _json_leaves(node: Any, document: _JsonText | None, out: list[_Target]) -> None:
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
                out.append(_Target(node, key, document))
        else:
            _json_leaves(value, document, out)


def _json_text_targets(holder: dict[str, Any], key: str, out: list[_Target]) -> None:
    """Targets for a field that holds JSON as text, or plain text if it is not."""
    raw = holder.get(key)
    if not isinstance(raw, str) or not raw:
        return
    try:
        root = json.loads(raw)
    except ValueError:
        root = None
    if isinstance(root, dict | list):
        _json_leaves(root, _JsonText(holder, key, root), out)
        return
    # Not JSON (a model can emit a broken call): the text itself is screened.
    out.append(_Target(holder, key))


def _content_targets(holder: dict[str, Any], key: str, out: list[_Target]) -> None:
    """A ``content``-like field: a string, or a list of blocks.

    Blocks may be text, a Messages ``tool_result`` (whose own ``content`` is a
    string or more blocks) or a ``tool_use`` (whose ``input`` is an object).
    """
    value = holder.get(key)
    if isinstance(value, str):
        _text_targets(holder, key, out)
        return
    if not isinstance(value, list):
        return
    for part in value:
        if not isinstance(part, dict):
            continue
        _text_targets(part, "text", out)
        if part.get("type") == "tool_result":
            _content_targets(part, "content", out)
        if part.get("type") == "tool_use" and isinstance(part.get("input"), dict | list):
            _json_leaves(part["input"], None, out)


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
    _content_targets(message, "content", out)
    for key in _REASONING_KEYS:
        _text_targets(message, key, out)
    for call in message.get("tool_calls") or []:
        function = call.get("function") if isinstance(call, dict) else None
        if isinstance(function, dict):
            _json_text_targets(function, "arguments", out)
    if kind == "function_call":
        _json_text_targets(message, "arguments", out)
    elif kind == "function_call_output":
        _content_targets(message, "output", out)
    elif kind == "reasoning":
        for field in ("summary", "content"):
            for block in message.get(field) or []:
                if isinstance(block, dict):
                    _text_targets(block, "text", out)
    return out


@lru_cache(maxsize=256)
def _compiled(pattern: str) -> Any:
    """One compile per distinct pattern per process.

    Compiling is cheap and doing it per request is still waste on the hottest
    path there is. Bounded rather than unbounded because the key is operator
    input: a deployment editing rules in a loop must not grow this without end.
    """
    import re2

    return re2.compile(pattern)


def pattern_spans(text: str, policy: RedactionPolicy) -> list[EntitySpan]:
    """Spans for the operator's own regexes.

    Produced in the gateway rather than asked of the detection service: no
    contract change, it works for whatever engine is installed, and one
    deployment's regexes have no business being installed into a service every
    deployment shares. They also stay *outside* the detection cache, since a
    regex is deterministic and cheap — caching them would fragment a key whose
    hit rate is worth 10x on a long conversation (docs/performance.md).

    Score 1.0: a pattern somebody wrote by hand is not a guess, so it outranks
    a model's opinion when the two overlap.
    """
    spans: list[EntitySpan] = []
    for pattern in policy.patterns:
        if pattern.mode is EntityMode.OFF:
            continue
        for match in _compiled(pattern.regex).finditer(text):
            start, end = match.span()
            if end > start:
                spans.append(EntitySpan(start=start, end=end, entity_type=pattern.name, score=1.0))
    return spans


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
) -> tuple[str, int]:
    """Substitute placeholders for *spans* in *text*, according to *policy*.

    Right to left, so each replacement leaves earlier offsets valid — rewriting
    left to right invalidates every span after the first.

    Overlapping spans are resolved by keeping the highest-scoring, then the
    longest. Two detectors disagreeing about the same characters is normal (an
    IBAN is also a long number), and replacing one inside another produces
    corrupted text rather than a redaction.

    **The policy is applied before overlap resolution, not after**, and the order
    is load-bearing: a span the policy discards must not have suppressed the one
    it overlapped. A `URL` set to off, covering the same characters as a
    `PERSON`, would otherwise take the person with it.

    What each mode writes, and what it records:

    ``off``                 nothing; the text is left as the caller wrote it.
    ``anonymise_restore``   a derived placeholder, remembered, so the answer can
                            have the real value put back.
    ``anonymise``           the same placeholder, *not* remembered — so the
                            reader sees the placeholder too.
    ``redact``              ``<PERSON>``: no token, nothing to remember, and two
                            people become the same label.
    """
    policy = policy or DEFAULT_REDACTION_POLICY
    # Merged before eligibility and before overlap resolution, so an operator's
    # pattern competes with the detector's spans on the same terms — and a
    # pattern the policy has switched off drops out with everything else.
    #
    # Before the empty check too, and that is not a detail: a deployment whose
    # detector finds nothing still has its own patterns, and returning early on
    # `not spans` would have silently skipped every one of them.
    spans = [*spans, *pattern_spans(text, policy)]
    if not spans:
        return text, 0
    eligible = [
        span
        for span in spans
        if policy.mode_for(span.entity_type) is not EntityMode.OFF
        and span.score >= policy.threshold_for(span.entity_type, default_threshold)
        and not policy.allows(span.slice_of(text))
    ]
    if not eligible:
        return text, 0

    # Before any rewriting: a blocked request produces no redacted text at all,
    # and deciding this after substitution would mean building a placeholder map
    # for a request that is about to be refused.
    blocked = [span for span in eligible if policy.mode_for(span.entity_type) is EntityMode.BLOCK]
    if blocked:
        kinds = sorted({span.entity_type for span in blocked})
        where = f" by the {scope} policy" if scope else ""
        raise ContentBlockedError(
            f"This request was refused{where}: it contains "
            f"{', '.join(kinds)}, which this deployment does not send to a provider."
        )

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

    # What the caller wrote, before any of our own substitutions: a stand-in is
    # compared against that, not against text this loop has already rewritten,
    # or the second occurrence of one value would collide with the first.
    written = text
    for span in reversed(chosen):
        original = span.slice_of(text)
        mode = policy.mode_for(span.entity_type)
        if mode is EntityMode.REDACT:
            placeholder = opaque_placeholder(span.entity_type)
        else:
            placeholder = _derived_placeholder(
                span.entity_type,
                original,
                key=key,
                style=policy.placeholder_style_for(span.entity_type),
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
        self, texts: list[str], *, policy: RedactionPolicy | None = None
    ) -> list[list[EntitySpan]]:
        """Spans for each text, cached, in one round trip for the misses."""
        settings = self._settings
        active = policy or settings.policy
        # What the policy will actually act on, not what the engine can find.
        # Narrowing here is free detection time when an admin has enumerated the
        # set; when they have not, it is None and everything comes back to be
        # filtered locally — an engine's entity list is its own and cannot be
        # enumerated from here (ADR 0026).
        wanted = active.detected_types()
        # The lowest bar any enabled type sets. Filtering to each type's own
        # threshold happens in apply_spans: asking the detector for the strictest
        # would drop spans a laxer type still wants.
        floor = min(
            [settings.score_threshold]
            + [
                entry.threshold
                for entry in active.entities.values()
                if entry.threshold is not None and entry.mode is not EntityMode.OFF
            ]
        )
        keys = [
            _Cache.key(
                text,
                settings.language,
                floor,
                wanted,
                settings.presidio_pattern_matching,
                settings.presidio_ner,
            )
            for text in texts
        ]
        results: list[list[EntitySpan] | None] = [self._cache.get(key) for key in keys]

        pending = [index for index, spans in enumerate(results) if spans is None]
        if not pending:
            return [spans or [] for spans in results]

        request = DetectionRequest(
            texts=[texts[index] for index in pending],
            language=settings.language,
            score_threshold=floor,
            entity_types=wanted,
            presidio_pattern_matching=settings.presidio_pattern_matching,
            presidio_ner=settings.presidio_ner,
        )
        found = await self._post(request)

        for position, index in enumerate(pending):
            spans = [] if found is None else found.get(position, [])
            results[index] = spans
            # A fail-open outage yields no findings, which must not be cached:
            # one outage would otherwise leave every text seen during it marked
            # "clean" for the life of the process.
            if found is not None:
                self._cache.put(keys[index], spans)

        return [spans or [] for spans in results]

    async def _post(self, request: DetectionRequest) -> dict[int, list[EntitySpan]] | None:
        """Findings by position, or None when failing open on an outage."""
        try:
            response = await self._client.post(self._endpoint, json=request.model_dump(mode="json"))
            response.raise_for_status()
            parsed = DetectionResponse.model_validate(response.json())
        except (TimeoutError, httpx.HTTPError, ValueError) as exc:
            if self._settings.fail_open:
                # Loud, because this is the state in which the gateway is
                # forwarding unredacted prompts while still reporting an engine.
                logger.error(
                    "detection service unavailable (%s); FAILING OPEN and forwarding "
                    "the prompt unredacted",
                    exc,
                    exc_info=True,
                )
                return None
            logger.error("detection service unavailable (%s); refusing the request", exc)
            raise RedactionUnavailableError(
                "The redaction service could not be reached, so this request cannot be "
                "screened. It is refused rather than forwarded unredacted."
            ) from exc

        found = {finding.index: finding.spans for finding in parsed.findings}
        self._report_unsupported(parsed.unsupported_types)
        return found

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
        note = system_note(
            effective,
            restore_text=self._settings.restore_in_response,
            restore_arguments=self._settings.restore_in_response
            and self._settings.restore_in_tool_arguments,
        )
        if not texts:
            return RedactionOutcome(
                messages=messages, engine=self.name, scope=scope, rule_id=rule_id, note=note
            )

        found = await self.detect(texts, policy=effective)

        placeholders = PlaceholderMap()
        total = 0
        tally: Counter[str] = Counter()

        documents: dict[int, _JsonText] = {}
        for target, spans in zip(targets, found, strict=True):
            redacted, count = apply_spans(
                target.text,
                spans,
                key=self._key,
                placeholders=placeholders,
                policy=effective,
                default_threshold=self._settings.score_threshold,
                scope=scope,
                tally=tally,
            )
            total += count
            if count:
                target.write(redacted)
                if target.document is not None:
                    documents[id(target.document)] = target.document
        for document in documents.values():
            document.commit()

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


def _longest_placeholder(placeholders: PlaceholderMap) -> int:
    """How much text a stream must hold back to never split a placeholder.

    One less than the longest placeholder: a match needs its final character to
    be complete, and holding the full length back would be one more than needed.
    """
    longest = max((len(text) for text in placeholders.placeholders()), default=0)
    return max(0, longest - 1)


def build(settings: RedactionSettings) -> HttpDetectionRedactor:
    return HttpDetectionRedactor(settings)
