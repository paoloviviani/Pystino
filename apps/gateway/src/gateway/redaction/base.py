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

import copy
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from llmp_shared import PlaceholderMap

from gateway.sse.events import SSEEvent
from gateway.sse.pipeline import StreamStage


@dataclass
class RedactionOutcome:
    """Result of redacting a request, and the state the response path needs."""

    messages: list[dict[str, Any]]
    placeholder_map: PlaceholderMap = field(default_factory=PlaceholderMap)
    entity_count: int = 0
    engine: str = "noop"

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

    async def redact_request(self, messages: list[dict[str, Any]]) -> RedactionOutcome:
        """Rewrite outbound messages before they reach the upstream."""
        ...

    def response_stage(self, outcome: RedactionOutcome) -> StreamStage:
        """The stage that rewrites the streamed response."""
        ...

    async def redact_response_text(self, text: str, outcome: RedactionOutcome) -> str:
        """The non-streaming equivalent of :meth:`response_stage`."""
        ...


def iter_choice_text(payload: dict[str, Any]) -> list[tuple[int, str]]:
    """Extract ``(choice_index, text)`` from a streamed chunk."""
    results: list[tuple[int, str]] = []
    for choice in payload.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        index = choice.get("index")
        index = index if isinstance(index, int) else 0
        delta = choice.get("delta")
        if isinstance(delta, dict) and isinstance(content := delta.get("content"), str):
            results.append((index, content))
    return results


def set_choice_text(payload: dict[str, Any], index: int, text: str) -> None:
    """Replace one choice's delta content in place."""
    for choice in payload.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        choice_index = choice.get("index")
        choice_index = choice_index if isinstance(choice_index, int) else 0
        if choice_index != index:
            continue
        delta = choice.get("delta")
        if isinstance(delta, dict):
            delta["content"] = text


def has_finish_reason(payload: dict[str, Any]) -> bool:
    return any(
        isinstance(choice, dict) and choice.get("finish_reason")
        for choice in payload.get("choices") or []
    )


class TextRewriteStage(ABC):
    """Buffering rewriter for assistant text in a streamed response.

    Subclasses implement :meth:`transform`. ``tail_size`` is how much text is
    withheld while the stream is live: it must exceed the longest entity the
    engine can match, or a match straddling the boundary will be missed. Zero
    disables buffering, which is only correct for transforms that cannot span a
    boundary.
    """

    def __init__(self, *, tail_size: int = 0) -> None:
        self._tail_size = max(0, tail_size)
        self._pending: dict[int, str] = {}
        self._template: dict[str, Any] | None = None

    @abstractmethod
    def transform(self, text: str, *, final: bool) -> str:
        """Rewrite *text*.

        Called with the accumulated unreleased text. ``final`` is True on the
        last call for a choice, when nothing more will arrive.
        """

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

            texts = iter_choice_text(payload)
            if not texts:
                # A frame with no text (role preamble, or the finish frame). Flush
                # before a finish_reason so held-back text cannot arrive after the
                # client believes the message is complete.
                if has_finish_reason(payload):
                    for extra in self._flush_all():
                        yield extra
                yield event
                continue

            mutated = False
            for index, text in texts:
                rewritten = self._advance(index, text)
                if rewritten != text:
                    mutated = True
                set_choice_text(payload, index, rewritten)

            if mutated:
                event.replace_json(payload)

            # Flushing after writing this frame keeps ordering: released text
            # precedes the finish marker it shares a frame with.
            if has_finish_reason(payload):
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

    def _synthesise(self, index: int, text: str) -> SSEEvent:
        """Build a chunk shaped like the ones the upstream was sending.

        Copying the last real payload keeps ``id``, ``model`` and ``created``
        consistent, which strict clients check.
        """
        if self._template is not None:
            payload = copy.deepcopy(self._template)
            payload["choices"] = [{"index": index, "delta": {"content": text}}]
            payload.pop("usage", None)
        else:
            payload = {
                "object": "chat.completion.chunk",
                "choices": [{"index": index, "delta": {"content": text}}],
            }
        return SSEEvent.from_json(payload)
