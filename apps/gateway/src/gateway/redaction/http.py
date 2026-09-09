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

import hashlib
import logging
from collections import OrderedDict
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
)

from gateway.config import (
    DEFAULT_REDACTION_POLICY,
    EffectivePolicy,
    EntityMode,
    RedactionPolicy,
    RedactionSettings,
)
from gateway.errors import ContentBlockedError, GatewayError
from gateway.models import ApiSurface
from gateway.redaction.base import RedactionOutcome, TextRewriteStage
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
    def key(text: str, language: str, threshold: float, types: list[str] | None) -> str:
        parts = f"{language}\x1f{threshold}\x1f{','.join(types or [])}\x1f{text}"
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


def _message_texts(message: dict[str, Any]) -> list[tuple[Any, str]]:
    """``(location, text)`` for each redactable string in a message.

    Handles both content shapes: a plain string, and the multimodal list of parts.
    ``location`` is ``None`` for the plain form and the part's index for the list
    form, so the caller can write the redacted text back where it came from.
    """
    content = message.get("content")
    if isinstance(content, str):
        return [(None, content)]
    if isinstance(content, list):
        found: list[tuple[Any, str]] = []
        for index, part in enumerate(content):
            if isinstance(part, dict) and isinstance(text := part.get("text"), str):
                found.append((index, text))
        return found
    return []


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
                spans.append(
                    EntitySpan(start=start, end=end, entity_type=pattern.name, score=1.0)
                )
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
    blocked = [
        span for span in eligible if policy.mode_for(span.entity_type) is EntityMode.BLOCK
    ]
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

    for span in reversed(chosen):
        original = span.slice_of(text)
        mode = policy.mode_for(span.entity_type)
        if mode is EntityMode.REDACT:
            placeholder = opaque_placeholder(span.entity_type)
        else:
            placeholder = placeholder_for(span.entity_type, original, key=key)
            if mode is EntityMode.ANONYMISE_RESTORE:
                # The map is the only thing that makes a placeholder reversible,
                # so *not* adding is how "anonymise" differs from "anonymise and
                # restore". RestoreStage leaves what it did not issue alone.
                placeholders.add(span.entity_type, original, placeholder)
        text = text[: span.start] + placeholder + text[span.end :]

    return text, len(chosen)


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
    ) -> None:
        super().__init__(tail_size=tail_size, surface=surface)
        self._placeholders = placeholders

    def transform(self, text: str, *, final: bool) -> str:
        return self._placeholders.restore(text)

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
        keys = [_Cache.key(text, settings.language, floor, wanted) for text in texts]
        results: list[list[EntitySpan] | None] = [self._cache.get(key) for key in keys]

        pending = [index for index, spans in enumerate(results) if spans is None]
        if not pending:
            return [spans or [] for spans in results]

        request = DetectionRequest(
            texts=[texts[index] for index in pending],
            language=settings.language,
            score_threshold=floor,
            entity_types=wanted,
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
        locations: list[tuple[int, Any]] = []
        texts: list[str] = []
        for message_index, message in enumerate(messages):
            if message.get("role") not in _TEXT_ROLES:
                continue
            for part_index, text in _message_texts(message):
                if text:
                    locations.append((message_index, part_index))
                    texts.append(text)

        scope = policy.scope if policy is not None else None
        rule_id = policy.rule_id if policy is not None else None
        if not texts:
            return RedactionOutcome(
                messages=messages, engine=self.name, scope=scope, rule_id=rule_id
            )

        found = await self.detect(texts, policy=effective)

        placeholders = PlaceholderMap()
        # Copied, so a failure part-way cannot leave the caller's list half
        # rewritten — and so the transcript of what the user actually sent is
        # still available to the caller.
        rewritten = [dict(message) for message in messages]
        total = 0

        for (message_index, part_index), text, spans in zip(locations, texts, found, strict=True):
            redacted, count = apply_spans(
                text,
                spans,
                key=self._key,
                placeholders=placeholders,
                policy=effective,
                default_threshold=self._settings.score_threshold,
                scope=scope,
            )
            total += count
            if not count:
                continue
            message = rewritten[message_index]
            if part_index is None:
                message["content"] = redacted
            else:
                parts = [dict(part) for part in message["content"]]
                parts[part_index]["text"] = redacted
                message["content"] = parts

        return RedactionOutcome(
            messages=rewritten,
            placeholder_map=placeholders,
            entity_count=total,
            engine=self.name,
            scope=scope,
            rule_id=rule_id,
        )

    def response_stage(
        self, outcome: RedactionOutcome, *, surface: ApiSurface = ApiSurface.CHAT_COMPLETIONS
    ) -> StreamStage:
        if not self._settings.restore_in_response or not outcome.placeholder_map:
            # Nothing was replaced, so nothing can need restoring. Skips the
            # buffering and the per-frame JSON round trip entirely.
            return passthrough
        return RestoreStage(
            outcome.placeholder_map,
            tail_size=_longest_placeholder(outcome.placeholder_map),
            surface=surface,
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
