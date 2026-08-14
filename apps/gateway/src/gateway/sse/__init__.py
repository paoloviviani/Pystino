"""Server-Sent Events: parsing, re-serialisation and the transform pipeline."""

from gateway.sse.events import DONE_PAYLOAD, SSEEvent
from gateway.sse.parser import SSEParser, iter_sse_events
from gateway.sse.pipeline import StreamStage, chain, serialise_events

__all__ = [
    "DONE_PAYLOAD",
    "SSEEvent",
    "SSEParser",
    "StreamStage",
    "chain",
    "iter_sse_events",
    "serialise_events",
]
