"""One reassembled SSE event.

An event is the unit the rest of the gateway works with. Nothing downstream ever
sees a network chunk, because chunk boundaries fall in arbitrary places and a
rewriting stage that saw half a JSON object could not do its job.
"""

from __future__ import annotations

from typing import Any, Final

import orjson

DONE_PAYLOAD: Final = "[DONE]"

_UNSET: Final = object()


class SSEEvent:
    """A parsed event, with lazily parsed and memoised JSON.

    JSON parsing is memoised because several stages inspect the same event
    (usage capture, transcript accumulation, redaction) and re-parsing every
    frame three times at tens of thousands of frames per second is real CPU.
    """

    __slots__ = ("_json", "comments", "data_lines", "event", "id", "retry")

    def __init__(
        self,
        *,
        data_lines: list[str] | None = None,
        event: str | None = None,
        id: str | None = None,
        retry: int | None = None,
        comments: list[str] | None = None,
    ) -> None:
        self.data_lines: list[str] = data_lines if data_lines is not None else []
        self.event = event
        self.id = id
        self.retry = retry
        self.comments: list[str] = comments if comments is not None else []
        self._json: Any = _UNSET

    @property
    def data(self) -> str:
        """The data field, multiple ``data:`` lines joined with newlines per spec."""
        return "\n".join(self.data_lines)

    def is_done(self) -> bool:
        """True for the OpenAI terminator frame ``data: [DONE]``."""
        return self.data.strip() == DONE_PAYLOAD

    def is_comment_only(self) -> bool:
        """True for keepalive frames such as ``: ping``.

        These must be forwarded, not dropped: some clients and intermediaries
        rely on them to keep an idle connection open.
        """
        return bool(self.comments) and not self.data_lines and self.event is None

    def json(self) -> Any | None:
        """Parsed data payload, or None if absent, ``[DONE]`` or not valid JSON.

        A malformed frame is not an error here. Providers occasionally emit
        non-JSON diagnostics mid-stream, and a proxy's job is to pass them
        through rather than to fail the request.
        """
        if self._json is _UNSET:
            self._json = self._parse()
        return self._json

    def _parse(self) -> Any | None:
        payload = self.data
        if not payload or payload.strip() == DONE_PAYLOAD:
            return None
        try:
            return orjson.loads(payload)
        except orjson.JSONDecodeError:
            return None

    def replace_json(self, obj: Any) -> None:
        """Replace the data payload with *obj*, re-serialising it.

        Used by rewriting stages. Keeps the memoised parse consistent with the
        bytes that will actually be emitted.
        """
        self.data_lines = [orjson.dumps(obj).decode()]
        self._json = obj

    def encode(self) -> bytes:
        """Serialise back to wire format, including the terminating blank line."""
        parts: list[str] = []
        for comment in self.comments:
            parts.append(f":{comment}\n")
        if self.event is not None:
            parts.append(f"event: {self.event}\n")
        if self.id is not None:
            parts.append(f"id: {self.id}\n")
        if self.retry is not None:
            parts.append(f"retry: {self.retry}\n")
        # An event with no data lines still needs its blank line to terminate.
        for line in self.data_lines:
            parts.append(f"data: {line}\n")
        parts.append("\n")
        return "".join(parts).encode()

    def __repr__(self) -> str:
        preview = self.data[:60]
        return f"<SSEEvent event={self.event!r} data={preview!r}>"

    @classmethod
    def from_json(cls, obj: Any, *, event: str | None = None) -> SSEEvent:
        instance = cls(data_lines=[orjson.dumps(obj).decode()], event=event)
        instance._json = obj
        return instance

    @classmethod
    def done(cls) -> SSEEvent:
        return cls(data_lines=[DONE_PAYLOAD])
