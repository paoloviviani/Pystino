"""The engine that calls an out-of-process detector.

Speaks the contract in :mod:`llmp_shared.redaction` and nothing else, so any
service that serves ``POST /detect`` is a valid detector regardless of the model
behind it ([0026](../../../../docs/adr/0026-pluggable-detection.md)). Presidio in
``services/redaction`` is a reference implementation, not a dependency.

The division of labour matters and is from [0012](../../../../docs/adr/0012-redaction-interface.md):
the detector returns **spans only**. Placeholders are derived here, by keyed HMAC,
so a detector can never see the key, never break placeholder stability across
turns, and can be swapped without re-labelling anything already shown to a user.
"""

from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict
from typing import Any

import httpx
from llmp_shared import (
    DetectionRequest,
    DetectionResponse,
    EntitySpan,
    PlaceholderMap,
    opaque_placeholder,
    placeholder_for,
)

from gateway.config import (
    DEFAULT_REDACTION_POLICY,
    EntityMode,
    RedactionPolicy,
    RedactionSettings,
)
from gateway.errors import GatewayError
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


def apply_spans(
    text: str,
    spans: list[EntitySpan],
    *,
    key: bytes,
    placeholders: PlaceholderMap,
    policy: RedactionPolicy | None = None,
    default_threshold: float = 0.0,
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
    if not spans:
        return text, 0

    policy = policy or DEFAULT_REDACTION_POLICY
    eligible = [
        span
        for span in spans
        if policy.mode_for(span.entity_type) is not EntityMode.OFF
        and span.score >= policy.threshold_for(span.entity_type, default_threshold)
        and not policy.allows(span.slice_of(text))
    ]
    if not eligible:
        return text, 0

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

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- detection ---------------------------------------------------------

    async def detect(self, texts: list[str]) -> list[list[EntitySpan]]:
        """Spans for each text, cached, in one round trip for the misses."""
        settings = self._settings
        # What the policy will actually act on, not what the engine can find.
        # Narrowing here is free detection time when an admin has enumerated the
        # set; when they have not, it is None and everything comes back to be
        # filtered locally — an engine's entity list is its own and cannot be
        # enumerated from here (ADR 0026).
        wanted = settings.policy.detected_types()
        # The lowest bar any enabled type sets. Filtering to each type's own
        # threshold happens in apply_spans: asking the detector for the strictest
        # would drop spans a laxer type still wants.
        floor = min(
            [settings.score_threshold]
            + [
                entry.threshold
                for entry in settings.policy.entities.values()
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

        return {finding.index: finding.spans for finding in parsed.findings}

    # -- Redactor ----------------------------------------------------------

    async def redact_request(self, messages: list[dict[str, Any]]) -> RedactionOutcome:
        locations: list[tuple[int, Any]] = []
        texts: list[str] = []
        for message_index, message in enumerate(messages):
            if message.get("role") not in _TEXT_ROLES:
                continue
            for part_index, text in _message_texts(message):
                if text:
                    locations.append((message_index, part_index))
                    texts.append(text)

        if not texts:
            return RedactionOutcome(messages=messages, engine=self.name)

        found = await self.detect(texts)

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
                policy=self._settings.policy,
                default_threshold=self._settings.score_threshold,
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

    async def redact_response_text(self, text: str, outcome: RedactionOutcome) -> str:
        if not self._settings.restore_in_response:
            return text
        return outcome.placeholder_map.restore(text)


def _longest_placeholder(placeholders: PlaceholderMap) -> int:
    """How much text a stream must hold back to never split a placeholder.

    One less than the longest placeholder: a match needs its final character to
    be complete, and holding the full length back would be one more than needed.
    """
    longest = max((len(text) for text in placeholders.placeholders()), default=0)
    return max(0, longest - 1)


def build(settings: RedactionSettings) -> HttpDetectionRedactor:
    return HttpDetectionRedactor(settings)
