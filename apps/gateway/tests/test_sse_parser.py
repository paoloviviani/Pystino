"""SSE reassembly.

The property under test throughout: **the output must not depend on how the byte
stream is chopped up.** Every test that matters here feeds the same bytes in
different chunkings and asserts the same events come out, because in production
the chunking is chosen by the network and is never the same twice.
"""

from __future__ import annotations

import orjson
import pytest
from gateway.sse.events import SSEEvent
from gateway.sse.parser import SSEParser, iter_sse_events


def collect(parser: SSEParser, chunks: list[bytes]) -> list[SSEEvent]:
    events: list[SSEEvent] = []
    for piece in chunks:
        events.extend(parser.feed(piece))
    events.extend(parser.close())
    return events


def test_single_event() -> None:
    events = collect(SSEParser(), [b'data: {"a":1}\n\n'])
    assert len(events) == 1
    assert events[0].json() == {"a": 1}


def test_two_events_in_one_chunk() -> None:
    events = collect(SSEParser(), [b"data: one\n\ndata: two\n\n"])
    assert [event.data for event in events] == ["one", "two"]


def test_event_split_byte_by_byte() -> None:
    """The pathological chunking: one byte at a time."""
    raw = b'data: {"hello":"world"}\n\ndata: [DONE]\n\n'
    events = collect(SSEParser(), [raw[i : i + 1] for i in range(len(raw))])
    assert len(events) == 2
    assert events[0].json() == {"hello": "world"}
    assert events[1].is_done()


@pytest.mark.parametrize("split", range(1, 39))
def test_split_at_every_possible_offset(split: int) -> None:
    """Same bytes, every possible two-way split, identical result."""
    raw = b'data: {"n":1}\n\ndata: {"n":2}\n\ndata: [DONE]\n\n'
    events = collect(SSEParser(), [raw[:split], raw[split:]])
    assert [event.data for event in events] == [
        '{"n":1}',
        '{"n":2}',
        "[DONE]",
    ]


def test_boundary_split_inside_the_blank_line() -> None:
    """The nastiest case: the chunk ends between the two newlines."""
    events = collect(SSEParser(), [b"data: one\n", b"\ndata: two\n\n"])
    assert [event.data for event in events] == ["one", "two"]


def test_crlf_line_endings() -> None:
    events = collect(SSEParser(), [b"data: one\r\n\r\ndata: two\r\n\r\n"])
    assert [event.data for event in events] == ["one", "two"]


def test_crlf_split_between_cr_and_lf() -> None:
    """A trailing CR is ambiguous and must not be resolved early.

    If the parser treated the CR as a complete line terminator, the following LF
    would look like a blank line and would end the event one frame too soon.
    """
    events = collect(SSEParser(), [b"data: one\r", b"\n\r\n", b"data: two\r\n\r\n"])
    assert [event.data for event in events] == ["one", "two"]


def test_bare_cr_line_endings() -> None:
    events = collect(SSEParser(), [b"data: one\r\rdata: two\r\r"])
    assert [event.data for event in events] == ["one", "two"]


def test_multi_line_data_is_joined_with_newlines() -> None:
    events = collect(SSEParser(), [b"data: line one\ndata: line two\n\n"])
    assert events[0].data == "line one\nline two"


def test_data_without_space_after_colon() -> None:
    """Exactly one optional space is syntax; a second space is content."""
    events = collect(SSEParser(), [b"data:no-space\n\n", b"data:  two-spaces\n\n"])
    assert events[0].data == "no-space"
    assert events[1].data == " two-spaces"


def test_event_id_and_retry_fields() -> None:
    events = collect(SSEParser(), [b"event: ping\nid: 42\nretry: 3000\ndata: payload\n\n"])
    assert events[0].event == "ping"
    assert events[0].id == "42"
    assert events[0].retry == 3000
    assert events[0].data == "payload"


def test_comments_are_preserved() -> None:
    """Keepalive comments must survive: clients and proxies rely on them."""
    events = collect(SSEParser(), [b": keepalive\n\n"])
    assert len(events) == 1
    assert events[0].is_comment_only()
    assert events[0].comments == [" keepalive"]


def test_unknown_fields_are_ignored() -> None:
    events = collect(SSEParser(), [b"unknown: whatever\ndata: kept\n\n"])
    assert events[0].data == "kept"


def test_repeated_blank_lines_do_not_emit_empty_events() -> None:
    events = collect(SSEParser(), [b"\n\n\ndata: one\n\n\n\n"])
    assert [event.data for event in events] == ["one"]


def test_unterminated_trailing_event_is_emitted_on_close() -> None:
    """Not every server sends the final blank line.

    Dropping the last frame would specifically drop the frame carrying token
    usage, so an unterminated tail is emitted rather than discarded.
    """
    events = collect(SSEParser(), [b'data: {"usage":{"total_tokens":7}}'])
    assert len(events) == 1
    assert events[0].json() == {"usage": {"total_tokens": 7}}


def test_multibyte_utf8_split_across_chunks() -> None:
    """A character split across chunks must still decode correctly."""
    text = "naïve — 日本語"
    raw = b"data: " + text.encode() + b"\n\n"
    events = collect(SSEParser(), [raw[i : i + 1] for i in range(len(raw))])
    assert events[0].data == text


def test_malformed_json_does_not_raise() -> None:
    """A proxy passes odd frames through instead of failing the request."""
    events = collect(SSEParser(), [b"data: {not json\n\n"])
    assert events[0].json() is None
    assert events[0].data == "{not json"


def test_encode_round_trip() -> None:
    original = b"event: message\nid: 9\ndata: hello\n\n"
    events = collect(SSEParser(), [original])
    re_encoded = events[0].encode()
    assert collect(SSEParser(), [re_encoded])[0].data == "hello"
    assert b"event: message" in re_encoded
    assert re_encoded.endswith(b"\n\n")


def test_replace_json_updates_both_bytes_and_cache() -> None:
    event = SSEEvent(data_lines=[orjson.dumps({"a": 1}).decode()])
    assert event.json() == {"a": 1}
    event.replace_json({"a": 2})
    assert event.json() == {"a": 2}
    assert b'"a":2' in event.encode()


async def test_iter_sse_events_over_async_stream() -> None:
    async def stream() -> object:
        for piece in [b"data: on", b"e\n\nda", b"ta: two\n\n"]:
            yield piece

    seen = [event.data async for event in iter_sse_events(stream())]  # type: ignore[arg-type]
    assert seen == ["one", "two"]
