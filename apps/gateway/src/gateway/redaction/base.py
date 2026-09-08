"""The redaction/guardrail interface.

Phase 1 ships the no-op. What must be right *now* is the shape, because the hard
part of response redaction is not detection — it is that a streamed response
arrives in fragments and an entity does not respect fragment boundaries. A name
can arrive as ``Ma`` / ``rio Ros`` / ``si`` across three frames, and a stage that
inspects each frame in isolation will never see it.

:class:`TextRewriteStage` solves that here, once, so Phase 2 only has to supply a
``transform``. It:

* extracts assistant text per choice from streamed chunks,
* holds back a configurable tail so a match spanning frames can still be found,
* emits held-back text as a synthesised chunk when the stream ends,
* and flushes before the ``finish_reason`` frame and before ``[DONE]``, so the
  ordering a client sees stays valid.

The stage is exercised by tests with a deliberately boundary-spanning transform,
so the machinery is proven without shipping an engine.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from llmp_shared import PlaceholderMap, Restored

from gateway.config import EffectivePolicy
from gateway.models import ApiSurface
from gateway.protocols import reader_for
from gateway.sse.events import SSEEvent
from gateway.sse.pipeline import StreamStage


@dataclass
class RedactionOutcome:
    """Result of redacting a request, and the state the response path needs."""

    messages: list[dict[str, Any]]
    placeholder_map: PlaceholderMap = field(default_factory=PlaceholderMap)
    entity_count: int = 0
    engine: str = "noop"
    #: Which scoped rule tightened this request, copied onto the usage row. The
    #: rules table is mutable, so the trail that answers "why was this redacted"
    #: has to live on the request rather than on the rule (ADR 0038).
    scope: str | None = None
    rule_id: uuid.UUID | None = None

    @property
    def changed(self) -> bool:
        return self.entity_count > 0


@runtime_checkable
class Redactor(Protocol):
    """A redaction engine.

    Implementations must be safe to share across concurrent requests: all
    per-request state belongs in the returned :class:`RedactionOutcome`.
    """

    name: str

    async def redact_request(
        self, messages: list[dict[str, Any]], *, policy: EffectivePolicy | None = None
    ) -> RedactionOutcome:
        """Rewrite outbound messages before they reach the upstream.

        ``policy`` is the folded answer for this request's scopes (ADR 0038).
        None means "the deployment policy this engine was built with", which is
        what every caller did before scoping existed and what a test harness
        without a resolver still does.
        """
        ...

    def response_stage(
        self, outcome: RedactionOutcome, *, surface: ApiSurface = ApiSurface.CHAT_COMPLETIONS
    ) -> StreamStage:
        """The stage that rewrites the streamed response.

        ``surface`` selects where the assistant text lives in the frames; the
        buffering that makes the rewrite safe across chunk boundaries is the
        same for all of them.
        """
        ...

    async def restore_response(self, text: str, outcome: RedactionOutcome) -> Restored:
        """The non-streaming equivalent of :meth:`response_stage`.

        Returns the text **and the edits that produced it**, because a caller
        may be holding offsets into the text it passed in — OpenAI's
        ``url_citation`` annotations are character indices — and restoring a
        value of a different length moves everything after it (ADR 0059).
        A caller with no offsets to fix reads ``.text`` and ignores the rest.
        """
        ...


def iter_choice_text(payload: dict[str, Any]) -> list[tuple[int, str]]:
    """Extract ``(choice_index, text)`` from a streamed chat chunk.

    Kept as a module function because tests and the noop redactor use it
    directly; the streaming stage goes through the surface protocol instead, so
    that the same buffering serves all five surfaces.
    """
    return reader_for(ApiSurface.CHAT_COMPLETIONS).stream_texts(payload)


class TextRewriteStage(ABC):
    """Buffering rewriter for assistant text in a streamed response.

    Subclasses implement :meth:`transform`. ``tail_size`` is how much text is
    withheld while the stream is live: it must exceed the longest entity the
    engine can match, or a match straddling the boundary will be missed. Zero
    disables buffering, which is only correct for transforms that cannot span a
    boundary.
    """

    def __init__(
        self, *, tail_size: int = 0, surface: ApiSurface = ApiSurface.CHAT_COMPLETIONS
    ) -> None:
        self._tail_size = max(0, tail_size)
        self._pending: dict[int, str] = {}
        # The provider's text as it arrived, per choice, kept only so that a
        # citation can be moved. A streamed citation's offsets are into the
        # *whole accumulated message*, not into the frame that carries them, so
        # nothing less than the whole answer can place them (ADR 0059).
        #
        # Recomputed lazily and only for a frame that actually carries offsets,
        # which is a handful per response — accumulating is a list append, and
        # remapping never runs on the frames that are just text.
        self._provider: dict[int, list[str]] = {}
        self._maps: dict[int, Restored] = {}
        self._template: dict[str, Any] | None = None
        # Where this surface keeps its assistant text. The buffering below is
        # protocol-independent; only the accessors differ.
        self._proto = reader_for(surface)
        # Anthropic is the one surface that uses named SSE events, so a
        # synthesised frame there needs its `event:` line too.
        self._named_events = surface is ApiSurface.MESSAGES

    @abstractmethod
    def transform(self, text: str, *, final: bool) -> str:
        """Rewrite *text*.

        Called with the accumulated unreleased text. ``final`` is True on the
        last call for a choice, when nothing more will arrive.
        """

    def edits_in(self, text: str) -> Restored | None:
        """The same rewrite as :meth:`transform`, and *where* it wrote.

        ``None`` — the default — means this transform never moves a character,
        so any offset a frame carries is still correct and no work is done.
        Overridden by the restoring rewriter, which is the one that changes
        lengths (ADR 0059).
        """
        return None

    async def __call__(self, events: AsyncIterator[SSEEvent]) -> AsyncIterator[SSEEvent]:
        async for event in events:
            payload = event.json()

            # Terminator and keepalives carry no text, but the terminator is the
            # last chance to release what is still buffered.
            if event.is_done() or not isinstance(payload, dict):
                for extra in self._flush_all():
                    yield extra
                yield event
                continue

            if event.is_comment_only():
                yield event
                continue

            self._template = payload

            texts = self._proto.stream_texts(payload)
            if not texts:
                # A frame with no incremental text: a role preamble, a
                # lifecycle marker, or a terminal frame repeating the finished
                # text. Flush before a terminal frame so held-back text cannot
                # arrive after the client believes the message is complete —
                # and rewrite any complete text the frame itself carries.
                if self._proto.is_terminal(payload):
                    for extra in self._flush_all():
                        yield extra
                moved = self._proto.rewrite_whole(payload, self._rewrite_settled)
                # Citations usually arrive on exactly this kind of frame: no
                # text of their own, pointing back at text already sent.
                if self._proto.shift_citations(payload, self._shift):
                    moved = True
                if moved:
                    event.replace_json(payload)
                yield event
                continue

            mutated = False
            for index, text in texts:
                self._remember(index, text)
                rewritten = self._advance(index, text)
                if rewritten != text:
                    mutated = True
                self._proto.set_stream_text(payload, index, rewritten)

            if self._proto.rewrite_whole(payload, self._rewrite_settled):
                mutated = True
            if self._proto.shift_citations(payload, self._shift):
                mutated = True
            if mutated:
                event.replace_json(payload)

            # Flushing after writing this frame keeps ordering: released text
            # precedes the finish marker it shares a frame with.
            if self._proto.is_terminal(payload):
                yield event
                for extra in self._flush_all():
                    yield extra
                continue

            yield event

        # Stream ended without a terminator; do not lose the tail.
        for extra in self._flush_all():
            yield extra

    def _advance(self, index: int, text: str) -> str:
        """Accumulate *text*, transform, and release everything that is settled.

        The subtlety that makes this the whole point of the class: the transform
        must be applied to the buffer **including** the tail, and only then may a
        prefix be released. Releasing the prefix first and transforming it in
        isolation would cut an entity in half at the release boundary — the exact
        bug this design exists to avoid.

        The unreleased remainder is kept in already-transformed form, so
        ``transform`` sees its own output again on the next call. Implementations
        must therefore be **idempotent**: ``transform(transform(x)) ==
        transform(x)``. Placeholder substitution satisfies this, because a
        placeholder does not itself look like the entity it replaced.
        """
        buffered = self._pending.get(index, "") + text
        if not self._tail_size:
            self._pending[index] = ""
            return self.transform(buffered, final=False) if buffered else ""

        transformed = self.transform(buffered, final=False) if buffered else ""
        boundary = len(transformed) - self._tail_size
        if boundary <= 0:
            self._pending[index] = transformed
            return ""

        self._pending[index] = transformed[boundary:]
        return transformed[:boundary]

    def _flush_all(self) -> list[SSEEvent]:
        """Emit synthesised chunks carrying whatever is still held back."""
        events: list[SSEEvent] = []
        for index in sorted(self._pending):
            held = self._pending[index]
            if not held:
                continue
            self._pending[index] = ""
            text = self.transform(held, final=True)
            if not text:
                continue
            events.append(self._synthesise(index, text))
        return events

    def _remember(self, index: int, text: str) -> None:
        """Keep the provider's own text, and drop any stale offset map."""
        if text:
            self._provider.setdefault(index, []).append(text)
            self._maps.pop(index, None)

    def _shift(self, index: int, offset: int) -> int:
        """Where *offset* in choice *index* ended up after restoration.

        The map is built from every character the provider has sent for this
        choice, which is the coordinate system its citations use. Cached until
        more text arrives, so a frame carrying several annotations pays for one
        pass and a run of text frames pays for none.
        """
        cached = self._maps.get(index)
        if cached is None:
            arrived = "".join(self._provider.get(index, ()))
            if not arrived:
                return offset
            computed = self.edits_in(arrived)
            if computed is None:
                return offset
            cached = computed
            self._maps[index] = cached
        return cached.shift(offset)

    def _rewrite_settled(self, text: str) -> str:
        """Transform a complete text field.

        ``final=True`` because there is nothing more coming for this field —
        it arrived whole, so there is no boundary an entity could straddle.
        """
        return self.transform(text, final=True) if text else text

    def _synthesise(self, index: int, text: str) -> SSEEvent:
        """Build a frame shaped like the ones the upstream was sending."""
        payload = self._proto.synthesise(self._template, index, text)
        event = SSEEvent.from_json(payload)
        # Anthropic names its events, and a client switching on `event:` would
        # ignore a frame that carries only `data:`.
        if isinstance(kind := payload.get("type"), str) and self._named_events:
            event.event = kind
        return event
