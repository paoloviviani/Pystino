"""The redaction interface, and the buffering that makes Phase 2 possible.

The no-op engine ships this session, so there is little behaviour to test there.
What must be proven now is the machinery underneath it: that a rewriter can catch
an entity whose text is split across several SSE frames, and that held-back text is
released in a valid position in the stream.

If that were not proven, "the SSE path is structured so response rewriting can be
added without redesign" would be an assertion rather than a fact.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

import orjson
from gateway.redaction import RedactionOutcome, TextRewriteStage
from gateway.redaction.noop import NoOpRedactor
from gateway.sse.events import SSEEvent
from helpers import chunk


async def stream_of(payloads: list[dict | str]) -> AsyncIterator[SSEEvent]:
    for payload in payloads:
        if isinstance(payload, str):
            yield SSEEvent(data_lines=[payload])
        else:
            yield SSEEvent.from_json(payload)


def texts_from(events: list[SSEEvent]) -> str:
    """Concatenate all assistant content across a list of events."""
    out: list[str] = []
    for event in events:
        payload = event.json()
        if not isinstance(payload, dict):
            continue
        for choice in payload.get("choices") or []:
            delta = choice.get("delta") or {}
            if isinstance(content := delta.get("content"), str):
                out.append(content)
    return "".join(out)


class NameRedactingStage(TextRewriteStage):
    """A stand-in for Phase 2: replaces a two-word name with a placeholder."""

    pattern = re.compile(r"Mario Rossi")

    def __init__(self) -> None:
        # Longer than the entity we need to match, which is the rule a real
        # engine must follow too.
        super().__init__(tail_size=16)
        self.calls: list[tuple[str, bool]] = []

    def transform(self, text: str, *, final: bool) -> str:
        self.calls.append((text, final))
        return self.pattern.sub("<PERSON_ABC123>", text)


class UpperStage(TextRewriteStage):
    def __init__(self, tail_size: int = 0) -> None:
        super().__init__(tail_size=tail_size)

    def transform(self, text: str, *, final: bool) -> str:
        return text.upper()


class TestNoOpRedactor:
    async def test_request_passes_through_unchanged(self) -> None:
        redactor = NoOpRedactor()
        messages = [{"role": "user", "content": "my name is Mario Rossi"}]
        outcome = await redactor.redact_request(messages)
        assert outcome.messages == messages
        assert outcome.entity_count == 0
        assert not outcome.changed

    async def test_engine_is_named_not_absent(self) -> None:
        """Recorded on every usage row, so a row cannot later be mistaken for
        one that was actually screened."""
        outcome = await NoOpRedactor().redact_request([])
        assert outcome.engine == "noop"

    async def test_response_stage_is_identity(self) -> None:
        redactor = NoOpRedactor()
        outcome = await redactor.redact_request([])
        events = [
            event
            async for event in redactor.response_stage(outcome)(
                stream_of([chunk("hello "), chunk("world"), "[DONE]"])
            )
        ]
        assert texts_from(events) == "hello world"

    async def test_response_text_is_unchanged(self) -> None:
        redactor = NoOpRedactor()
        outcome = await redactor.redact_request([])
        assert await redactor.redact_response_text("Mario Rossi", outcome) == "Mario Rossi"


class TestTextRewriteStage:
    async def test_entity_split_across_frames_is_still_caught(self) -> None:
        """The case a per-frame callback can never handle."""
        stage = NameRedactingStage()
        events = [
            event
            async for event in stage(
                stream_of(
                    [
                        chunk("Hello Ma"),
                        chunk("rio Ros"),
                        chunk("si, welcome."),
                        chunk(finish_reason="stop"),
                        "[DONE]",
                    ]
                )
            )
        ]
        combined = texts_from(events)
        assert "Mario Rossi" not in combined
        assert "<PERSON_ABC123>" in combined
        assert combined == "Hello <PERSON_ABC123>, welcome."

    async def test_no_text_is_lost(self) -> None:
        """Buffering must delay text, never drop it."""
        stage = UpperStage(tail_size=8)
        source = ["alpha ", "beta ", "gamma ", "delta"]
        events = [
            event
            async for event in stage(stream_of([chunk(piece) for piece in source] + ["[DONE]"]))
        ]
        assert texts_from(events) == "".join(source).upper()

    async def test_tail_is_flushed_before_the_finish_reason_frame(self) -> None:
        """A client must not see 'finished' before the last of the text."""
        stage = UpperStage(tail_size=100)  # holds everything back
        events = [
            event
            async for event in stage(
                stream_of([chunk("held back"), chunk(finish_reason="stop"), "[DONE]"])
            )
        ]

        finish_index = next(
            index
            for index, event in enumerate(events)
            if isinstance(payload := event.json(), dict)
            and any(c.get("finish_reason") for c in payload.get("choices") or [])
        )
        content_indexes = [index for index, event in enumerate(events) if texts_from([event])]
        assert content_indexes, "the held-back text was never emitted"
        assert max(content_indexes) <= finish_index + 1
        assert texts_from(events) == "HELD BACK"

    async def test_tail_is_flushed_when_the_stream_ends_without_done(self) -> None:
        stage = UpperStage(tail_size=100)
        events = [event async for event in stage(stream_of([chunk("no terminator")]))]
        assert texts_from(events) == "NO TERMINATOR"

    async def test_synthesised_chunk_copies_stream_identity(self) -> None:
        """id/model/created must match, because strict clients check them."""
        stage = UpperStage(tail_size=100)
        events = [
            event async for event in stage(stream_of([chunk("tail text", model="m1"), "[DONE]"]))
        ]
        synthesised = [
            event
            for event in events
            if isinstance(payload := event.json(), dict) and texts_from([event])
        ]
        payload = synthesised[-1].json()
        assert payload["id"] == "chatcmpl-test"
        assert payload["model"] == "m1"
        assert payload["created"] == 1_700_000_000
        # A synthesised frame must never carry usage.
        assert "usage" not in payload

    async def test_zero_tail_releases_immediately(self) -> None:
        stage = UpperStage(tail_size=0)
        events = [event async for event in stage(stream_of([chunk("abc"), "[DONE]"]))]
        assert texts_from(events) == "ABC"

    async def test_done_frame_is_preserved(self) -> None:
        stage = UpperStage(tail_size=4)
        events = [event async for event in stage(stream_of([chunk("hello"), "[DONE]"]))]
        assert events[-1].is_done()

    async def test_keepalive_comments_pass_through(self) -> None:
        stage = UpperStage(tail_size=4)

        async def source() -> AsyncIterator[SSEEvent]:
            yield SSEEvent(comments=[" ping"])
            yield SSEEvent.from_json(chunk("text"))
            yield SSEEvent(data_lines=["[DONE]"])

        events = [event async for event in stage(source())]
        assert any(event.is_comment_only() for event in events)

    async def test_multiple_choices_are_buffered_independently(self) -> None:
        stage = UpperStage(tail_size=100)
        payloads = [
            chunk("first", index=0),
            chunk("second", index=1),
            "[DONE]",
        ]
        events = [event async for event in stage(stream_of(payloads))]
        combined = texts_from(events)
        assert "FIRST" in combined
        assert "SECOND" in combined

    async def test_transform_is_told_when_it_is_the_final_call(self) -> None:
        stage = NameRedactingStage()
        _ = [event async for event in stage(stream_of([chunk("some text here"), "[DONE]"]))]
        assert any(final for _, final in stage.calls)


class TestRedactionOutcome:
    def test_changed_reflects_entity_count(self) -> None:
        assert not RedactionOutcome(messages=[]).changed
        assert RedactionOutcome(messages=[], entity_count=1).changed

    def test_placeholder_map_round_trips(self) -> None:
        outcome = RedactionOutcome(messages=[])
        outcome.placeholder_map.add("PERSON", "Mario Rossi", "<PERSON_ABC123>")
        restored = outcome.placeholder_map.restore("Hello <PERSON_ABC123>, and <PERSON_UNKNOWN>.")
        # Known placeholders are restored; unknown ones are left alone rather
        # than having an "original" invented for them.
        assert "Mario Rossi" in restored
        assert "<PERSON_UNKNOWN>" in restored

    def test_json_serialisable_payloads_survive_rewriting(self) -> None:
        event = SSEEvent.from_json(chunk("text"))
        payload = event.json()
        payload["choices"][0]["delta"]["content"] = "changed"
        event.replace_json(payload)
        assert orjson.loads(event.data)["choices"][0]["delta"]["content"] == "changed"
