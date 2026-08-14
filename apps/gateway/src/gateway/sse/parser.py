"""Incremental SSE parser.

The contract: feed it arbitrary byte chunks as they arrive from the network, get
back only *complete* events. Network chunk boundaries are meaningless — a single
TCP read can carry two and a half events, and providers routinely split one JSON
payload across reads — so every boundary decision here is made on the byte
stream, never on the chunk.

Line terminators are the fiddly part. The SSE specification allows ``\\n``,
``\\r\\n`` and a bare ``\\r``, which means a buffer ending in ``\\r`` is
genuinely ambiguous: the next chunk may begin with ``\\n``, making it one CRLF,
or with anything else, making it a complete line. Resolving that ambiguity early
would invent a blank line and therefore a spurious event boundary, so a trailing
``\\r`` is held back until more bytes arrive or the stream ends.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

from gateway.sse.events import SSEEvent


class SSEParser:
    """Reassembles events from a byte stream.

    Not thread-safe and not reentrant: one parser belongs to one stream.
    """

    __slots__ = ("_buf", "_comments", "_data", "_eof", "_event", "_id", "_retry", "_seen_field")

    def __init__(self) -> None:
        self._buf = bytearray()
        self._data: list[str] = []
        self._event: str | None = None
        self._id: str | None = None
        self._retry: int | None = None
        self._comments: list[str] = []
        self._seen_field = False
        self._eof = False

    def feed(self, chunk: bytes) -> Iterator[SSEEvent]:
        """Add bytes and yield every event they complete."""
        if chunk:
            self._buf.extend(chunk)
        while (line := self._pop_line()) is not None:
            event = self._consume_line(line)
            if event is not None:
                yield event

    def close(self) -> Iterator[SSEEvent]:
        """Signal end of stream and yield anything still buffered.

        A well-behaved server terminates the last event with a blank line. Not
        all of them do, and dropping the final frame would lose exactly the frame
        that carries token usage, so an unterminated trailing event is emitted
        rather than discarded.
        """
        self._eof = True
        while (line := self._pop_line()) is not None:
            event = self._consume_line(line)
            if event is not None:
                yield event

        # A final line with no terminator at all. _pop_line cannot return it
        # (there is nothing to split on), so it is consumed explicitly here —
        # otherwise a provider that ends its stream without a trailing newline
        # loses its last frame, which is exactly the frame carrying token usage.
        if self._buf:
            remainder = bytes(self._buf)
            self._buf.clear()
            event = self._consume_line(remainder)
            if event is not None:
                yield event

        if self._seen_field:
            event = self._flush()
            if event is not None:
                yield event

    # -- internals ---------------------------------------------------------

    def _pop_line(self) -> bytes | None:
        """Remove and return one complete line, or None if none is available.

        The returned line excludes its terminator.
        """
        buf = self._buf
        newline = buf.find(b"\n")
        carriage = buf.find(b"\r")

        if newline == -1 and carriage == -1:
            return None

        if carriage == -1:
            index, skip = newline, 1
        elif newline == -1:
            # A bare CR at the very end may be the first half of a CRLF that has
            # not arrived yet. Wait, unless the stream is over.
            if carriage == len(buf) - 1 and not self._eof:
                return None
            index, skip = carriage, 1
        elif carriage < newline:
            # CRLF only when the two are adjacent; otherwise a bare CR line.
            skip = 2 if carriage + 1 == newline else 1
            index = carriage
        else:
            index, skip = newline, 1

        line = bytes(buf[:index])
        del buf[: index + skip]
        return line

    def _consume_line(self, raw: bytes) -> SSEEvent | None:
        """Apply one line to the event under construction."""
        if not raw:
            # Blank line: dispatch.
            return self._flush()

        # A complete line is complete UTF-8: the terminators we split on are
        # ASCII and cannot appear inside a multi-byte sequence, so no character
        # can straddle this boundary. Malformed bytes are replaced rather than
        # raising, since a proxy should not fail a stream over one bad byte.
        line = raw.decode("utf-8", errors="replace")

        self._seen_field = True

        if line.startswith(":"):
            self._comments.append(line[1:])
            return None

        field, _, value = line.partition(":")
        # Exactly one leading space after the colon is part of the syntax.
        if value.startswith(" "):
            value = value[1:]

        match field:
            case "data":
                self._data.append(value)
            case "event":
                self._event = value
            case "id":
                # NUL is not permitted in the id field; ignore rather than store.
                if "\x00" not in value:
                    self._id = value
            case "retry":
                if value.isdigit():
                    self._retry = int(value)
            case _:
                # Unknown fields are ignored per spec. Deliberately not
                # forwarded: reproducing them would risk emitting something the
                # client cannot parse.
                pass
        return None

    def _flush(self) -> SSEEvent | None:
        if not self._seen_field:
            # Repeated blank lines between events; nothing to dispatch.
            return None
        event = SSEEvent(
            data_lines=self._data,
            event=self._event,
            id=self._id,
            retry=self._retry,
            comments=self._comments,
        )
        self._data = []
        self._event = None
        self._id = None
        self._retry = None
        self._comments = []
        self._seen_field = False
        return event


async def iter_sse_events(chunks: AsyncIterator[bytes]) -> AsyncIterator[SSEEvent]:
    """Turn a byte stream into an event stream."""
    parser = SSEParser()
    async for chunk in chunks:
        for event in parser.feed(chunk):
            yield event
    for event in parser.close():
        yield event
