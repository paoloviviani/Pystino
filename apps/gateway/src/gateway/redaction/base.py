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
from gateway.protocols import Slot, is_answer, is_arguments, reader_for
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
    #: Whether the reply's text (the answer and the reasoning) gets its real
    #: values back. Recorded here, at the moment the request was redacted,
    #: rather than read from the engine when the answer arrives: the engine can
    #: be swapped between the two, and a request is restored under the rules it
    #: was redacted under.
    restore_text: bool = True
    #: Whether a tool call's arguments get them back too. Never true unless
    #: :attr:`restore_text` is.
    restore_arguments: bool = True

    #: The system note this request's policy calls for, or None. Fixed by the
    #: policy, not by what matched, so it is the same on every turn.
    note: str | None = None
    #: How many substitutions were written as ``#TOKEN#``, keeping the original's
    #: length, rather than as ``<LABEL_TOKEN>``.
    same_length_substitutions: int = 0

    @property
    def changed(self) -> bool:
        return self.entity_count > 0


def restore_reasoning_and_arguments(
    payload: dict[str, Any], outcome: RedactionOutcome, *, surface: ApiSurface
) -> None:
    """Restore the reasoning and the tool-call arguments of a whole response body.

    The answer text is not handled here: it goes through
    :meth:`Redactor.restore_response`, which also reports where it edited so a
    citation can be moved. These two have no offsets to keep, and no engine
    hook of their own — the outcome records whether the request asked for them.
    """
    if not outcome.placeholder_map or not outcome.restore_text:
        return
    reader = reader_for(surface)
    reader.rewrite_reasoning(payload, outcome.placeholder_map.restore)
    if outcome.restore_arguments:
        reader.rewrite_arguments(payload, outcome.placeholder_map.restore_in_json)


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

    The answer text only. Kept as a module function because tests and the noop
    redactor use it directly; the streaming stage goes through the surface
    protocol instead, so that the same buffering serves all five surfaces.
    """
    return [
        (slot[0], text)
        for slot, text in reader_for(ApiSurface.CHAT_COMPLETIONS).stream_texts(payload)
        if is_answer(slot)
    ]


class TextRewriteStage(ABC):
    """Buffering rewriter for assistant text in a streamed response.

    Subclasses implement :meth:`transform`. ``tail_size`` is how much text is
    withheld while the stream is live: it must exceed the longest entity the
    engine can match, or a match straddling the boundary will be missed. Zero
    disables buffering, which is only correct for transforms that cannot span a
    boundary.
    """

    def __init__(
        self,
        *,
        tail_size: int = 0,
        surface: ApiSurface = ApiSurface.CHAT_COMPLETIONS,
        arguments: bool = False,
    ) -> None:
        self._tail_size = max(0, tail_size)
        #: Whether tool-call arguments go through :meth:`transform_arguments`.
        #: Off, they are not buffered at all: a subclass that never learned
        #: about them must not delay them for nothing.
        self._arguments = arguments
        # Text held back, per slot: the answer, the reasoning and each tool
        # call's arguments are separate strings, and one must never finish a
        # match another began.
        self._pending: dict[Slot, str] = {}
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
        # The last frame each slot appeared in, which a synthesised frame copies
        # its identity from.
        self._templates: dict[Slot, dict[str, Any]] = {}
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

    def transform_arguments(self, text: str, *, final: bool) -> str:
        """Rewrite a tool call's *JSON text*, and return JSON text.

        Only called when the stage was built with ``arguments=True``. The
        default leaves it alone; a transform that writes values into JSON has
        to escape them, which is why this is its own method and not
        :meth:`transform` with a flag.
        """
        return text

    def _apply(self, slot: Slot, text: str, *, final: bool) -> str:
        if is_arguments(slot):
            return self.transform_arguments(text, final=final)
        return self.transform(text, final=final)

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

            texts = [
                (slot, text)
                for slot, text in self._proto.stream_texts(payload)
                if self._arguments or not is_arguments(slot)
            ]
            if not texts:
                # A frame with no incremental text: a role preamble, a
                # lifecycle marker, or a terminal frame repeating the finished
                # text. Flush before a terminal frame so held-back text cannot
                # arrive after the client believes the message is complete —
                # and rewrite any complete text the frame itself carries.
                if self._proto.is_terminal(payload):
                    for extra in self._flush_all():
                        yield extra
                moved = self._rewrite_complete(payload)
                # Citations usually arrive on exactly this kind of frame: no
                # text of their own, pointing back at text already sent.
                if self._proto.shift_citations(payload, self._shift):
                    moved = True
                if moved:
                    event.replace_json(payload)
                yield event
                continue

            terminal = self._proto.is_terminal(payload)
            mutated = False
            here: set[Slot] = set()
            for slot, text in texts:
                self._templates[slot] = payload
                if is_answer(slot):
                    self._remember(slot[0], text)
                rewritten = self._advance(slot, text)
                if terminal:
                    # The frame that says "finished" must also be the one that
                    # carries the last of the text: a client is entitled to
                    # stop reading at it, and text sent after it is text lost.
                    rewritten += self._drain(slot)
                here.add(slot)
                if rewritten != text:
                    mutated = True
                self._proto.set_stream_text(payload, slot, rewritten)

            if terminal:
                # Anything held for a slot this frame does not speak of goes out
                # ahead of it, for the same reason.
                for extra in self._flush_all(skip=here):
                    yield extra

            if self._rewrite_complete(payload):
                mutated = True
            if self._proto.shift_citations(payload, self._shift):
                mutated = True
            if mutated:
                event.replace_json(payload)
            yield event

        # Stream ended without a terminator; do not lose the tail.
        for extra in self._flush_all():
            yield extra

    def _rewrite_complete(self, payload: dict[str, Any]) -> bool:
        """Rewrite the complete fields a frame carries: answer, reasoning, arguments."""
        changed = self._proto.rewrite_whole(payload, self._rewrite_settled)
        if self._proto.rewrite_reasoning(payload, self._rewrite_settled):
            changed = True
        if self._arguments and self._proto.rewrite_arguments(payload, self._rewrite_arguments):
            changed = True
        return changed

    def _advance(self, slot: Slot, text: str) -> str:
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
        buffered = self._pending.get(slot, "") + text
        if not self._tail_size:
            self._pending[slot] = ""
            return self._apply(slot, buffered, final=False) if buffered else ""

        transformed = self._apply(slot, buffered, final=False) if buffered else ""
        boundary = len(transformed) - self._tail_size
        if boundary <= 0:
            self._pending[slot] = transformed
            return ""

        self._pending[slot] = transformed[boundary:]
        return transformed[:boundary]

    def _drain(self, slot: Slot) -> str:
        """Release what is held for *slot*, now that nothing more will come."""
        held = self._pending.get(slot, "")
        if not held:
            return ""
        self._pending[slot] = ""
        return self._apply(slot, held, final=True)

    def _flush_all(self, skip: set[Slot] | None = None) -> list[SSEEvent]:
        """Emit synthesised chunks carrying whatever is still held back."""
        events: list[SSEEvent] = []
        for slot in sorted(self._pending):
            if skip and slot in skip:
                continue
            text = self._drain(slot)
            if not text:
                continue
            events.append(self._synthesise(slot, text))
        return events

    def _remember(self, index: int, text: str) -> None:
        """Keep the provider's own answer text, and drop any stale offset map.

        Only the answer is passed in: a citation's offsets are into that, not
        into the reasoning or the arguments beside it.
        """
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

    def _rewrite_arguments(self, text: str) -> str:
        """Transform a complete arguments string, which arrived whole."""
        return self.transform_arguments(text, final=True) if text else text

    def _synthesise(self, slot: Slot, text: str) -> SSEEvent:
        """Build a frame shaped like the ones the upstream was sending."""
        payload = self._proto.synthesise(self._templates.get(slot), slot, text)
        event = SSEEvent.from_json(payload)
        # Anthropic names its events, and a client switching on `event:` would
        # ignore a frame that carries only `data:`.
        if isinstance(kind := payload.get("type"), str) and self._named_events:
            event.event = kind
        return event
