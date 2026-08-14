"""Composable stages over a stream of events.

A stage is an async generator that consumes events and yields events. That shape
was chosen over a simpler ``event -> event`` callback for one specific reason: a
redaction stage cannot decide whether to emit a chunk of text until it knows the
text is not the start of an entity that continues into the next frame. "Person"
arriving as ``Ma``/``rio Ros``/``si`` across three frames must be detectable, so
a stage has to be able to hold bytes back and release them later — and to flush
what it held when the stream ends.

A callback returning one event per event cannot buffer. This can, which is what
makes Phase 2 an insertion rather than a redesign.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

from gateway.sse.events import SSEEvent

# A stage transforms a stream into a stream. It may drop events, emit extra ones,
# reorder within its own buffer, or hold events back until flush.
StreamStage = Callable[[AsyncIterator[SSEEvent]], AsyncIterator[SSEEvent]]


def chain(source: AsyncIterator[SSEEvent], *stages: StreamStage) -> AsyncIterator[SSEEvent]:
    """Compose stages left to right.

    ``chain(src, a, b)`` feeds ``src`` through ``a`` and then through ``b``.
    Nothing is consumed until the result is iterated, so building a pipeline is
    free and cancellation propagates back through every stage.
    """
    stream = source
    for stage in stages:
        stream = stage(stream)
    return stream


async def serialise_events(events: AsyncIterator[SSEEvent]) -> AsyncIterator[bytes]:
    """Final stage: back to bytes for the client."""
    async for event in events:
        yield event.encode()


async def passthrough(events: AsyncIterator[SSEEvent]) -> AsyncIterator[SSEEvent]:
    """Identity stage. Useful as a default and as a test double."""
    async for event in events:
        yield event


def tap(callback: Callable[[SSEEvent], None]) -> StreamStage:
    """Observe events without altering them.

    The callback must not block: it runs inline on the event loop between the
    upstream read and the client write, so anything slow here adds latency to
    every token. Used for usage capture and transcript accumulation, both of
    which are pure in-memory updates.
    """

    async def stage(events: AsyncIterator[SSEEvent]) -> AsyncIterator[SSEEvent]:
        async for event in events:
            callback(event)
            yield event

    return stage


def drop_if(predicate: Callable[[SSEEvent], bool]) -> StreamStage:
    """Remove events matching *predicate* from the client-facing stream."""

    async def stage(events: AsyncIterator[SSEEvent]) -> AsyncIterator[SSEEvent]:
        async for event in events:
            if not predicate(event):
                yield event

    return stage
