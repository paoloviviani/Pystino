"""Placeholders inside tool-call arguments are restored, and stay valid JSON.

The model is told about a value only by its placeholder, so when it calls a
tool it writes the placeholder into the arguments. The tool needs the real
value, and the arguments are JSON text, so the value goes back JSON-escaped.
"""

from __future__ import annotations

from typing import Any, ClassVar

import httpx
import orjson
import pytest
from conftest import Seeded
from helpers import completion_body
from llmp_shared import PlaceholderMap
from test_redaction_restore_surfaces import (
    event_frames,
    frames,
    install,
    issued,
    pieces,
    post_stream,
)

CF = "RSSMRA80A01F205X"
#: What an original with a quote and a backslash looks like once escaped.
NASTY = 'say "hi" \\ there\nnext\ttab'


def tool_chunk(arguments: str, *, call: int = 0, finish: str | None = None) -> dict[str, Any]:
    choice: dict[str, Any] = {
        "index": 0,
        "delta": {"tool_calls": [{"index": call, "function": {"arguments": arguments}}]},
    }
    if finish:
        choice["finish_reason"] = finish
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "upstream/test-model",
        "choices": [choice],
    }


def streamed_arguments(events: list[dict[str, Any]], call: int) -> str:
    return "".join(
        str(c["function"].get("arguments") or "")
        for event in events
        for choice in event.get("choices") or []
        for c in choice.get("delta", {}).get("tool_calls") or []
        if c.get("index") == call
    )


class TestTheMap:
    def mapped(self, original: str) -> PlaceholderMap:
        placeholders = PlaceholderMap()
        placeholders.add("X", original, "<X_ABCDEFGHIJ>")
        return placeholders

    def test_the_original_is_json_escaped(self) -> None:
        text = '{"a": "<X_ABCDEFGHIJ>"}'
        restored = self.mapped(NASTY).restore_in_json(text)
        assert orjson.loads(restored) == {"a": NASTY}

    def test_a_placeholder_escaped_as_unicode_is_still_found(self) -> None:
        text = '{"a": "\\u003cX_ABCDEFGHIJ\\u003E"}'
        assert orjson.loads(self.mapped("v").restore_in_json(text)) == {"a": "v"}

    def test_prose_restoration_is_not_escaped(self) -> None:
        assert self.mapped(NASTY).restore("<X_ABCDEFGHIJ>") == NASTY

    def test_an_unknown_placeholder_and_an_empty_map_change_nothing(self) -> None:
        assert PlaceholderMap().restore_in_json('{"a": "<X_ZZZZ>"}') == '{"a": "<X_ZZZZ>"}'
        assert self.mapped("v").restore_in_json('{"a": "<X_ZZZZZZZZZZ>"}') == (
            '{"a": "<X_ZZZZZZZZZZ>"}'
        )


class TestChatCompletions:
    async def test_a_whole_streamed_call_parses_with_the_real_value(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app)
        placeholder = await issued(app)
        arguments = f'{{"cf": "{placeholder}", "keep": 1}}'
        for size in (1, 3, 5, 26, 27, 28, 100):
            chunks = [tool_chunk(p) for p in pieces(arguments, size)]
            chunks.append(tool_chunk("", finish="tool_calls"))
            fake_upstream.set_stream(frames(chunks))
            events = await post_stream(
                client,
                "/v1/chat/completions",
                {
                    "model": seeded.model.name,
                    "stream": True,
                    "messages": [{"role": "user", "content": f"code {CF}"}],
                },
                seeded,
            )
            assert orjson.loads(streamed_arguments(events, 0)) == {"cf": CF, "keep": 1}, size

    async def test_two_interleaved_calls_do_not_share_a_buffer(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app)
        placeholder = await issued(app)
        half = len(placeholder) // 2
        chunks = [
            tool_chunk(f'{{"a": "{placeholder[:half]}', call=0),
            tool_chunk(f'{{"b": "{placeholder[:half]}', call=1),
            tool_chunk(f'{placeholder[half:]}"}}', call=0),
            tool_chunk(f'{placeholder[half:]}"}}', call=1),
            tool_chunk("", call=0, finish="tool_calls"),
        ]
        fake_upstream.set_stream(frames(chunks))
        events = await post_stream(
            client,
            "/v1/chat/completions",
            {
                "model": seeded.model.name,
                "stream": True,
                "messages": [{"role": "user", "content": f"code {CF}"}],
            },
            seeded,
        )
        assert orjson.loads(streamed_arguments(events, 0)) == {"a": CF}
        assert orjson.loads(streamed_arguments(events, 1)) == {"b": CF}

    @pytest.mark.parametrize("stream", [False, True])
    async def test_a_quote_and_a_backslash_in_the_original_keep_it_valid(
        self,
        stream: bool,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: Any,
    ) -> None:
        from test_redaction_http import FakeDetector

        detector = FakeDetector({NASTY: "SECRET"})
        app.state.redactor = detector.redactor()
        app.state.redaction = None
        outcome = await app.state.redactor.redact_request([{"role": "user", "content": NASTY}])
        placeholder = str(outcome.messages[0]["content"])
        arguments = f'{{"value": "{placeholder}"}}'
        request = {
            "model": seeded.model.name,
            "messages": [{"role": "user", "content": f"use {NASTY}"}],
        }
        if stream:
            chunks = [tool_chunk(p) for p in pieces(arguments, 5)]
            chunks.append(tool_chunk("", finish="tool_calls"))
            fake_upstream.set_stream(frames(chunks))
            events = await post_stream(
                client, "/v1/chat/completions", {**request, "stream": True}, seeded
            )
            assert orjson.loads(streamed_arguments(events, 0)) == {"value": NASTY}
        else:
            body = completion_body("")
            body["choices"][0]["message"]["tool_calls"] = [
                {"id": "c", "type": "function", "function": {"name": "f", "arguments": arguments}}
            ]
            fake_upstream.set_json(body)
            response = await client.post("/v1/chat/completions", json=request, headers=seeded.auth)
            call = response.json()["choices"][0]["message"]["tool_calls"][0]
            assert orjson.loads(call["function"]["arguments"]) == {"value": NASTY}

    async def test_a_placeholder_that_was_only_anonymised_stays_a_placeholder(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        from gateway.config import EntityMode, RedactionPolicy

        detector = install(app, policy=RedactionPolicy(default_mode=EntityMode.ANONYMISE))
        assert detector is not None
        placeholder = await issued(app)
        body = completion_body("")
        body["choices"][0]["message"]["tool_calls"] = [
            {
                "id": "c",
                "type": "function",
                "function": {"name": "f", "arguments": f'{{"cf": "{placeholder}"}}'},
            }
        ]
        fake_upstream.set_json(body)
        response = await client.post(
            "/v1/chat/completions",
            json={"model": seeded.model.name, "messages": [{"role": "user", "content": CF}]},
            headers=seeded.auth,
        )
        call = response.json()["choices"][0]["message"]["tool_calls"][0]
        assert placeholder in call["function"]["arguments"]

    async def test_the_setting_can_turn_it_off_and_needs_restore_on(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        for overrides in (
            {"restore_in_tool_arguments": False},
            {"restore_in_response": False},
        ):
            install(app, **overrides)
            placeholder = await issued(app)
            body = completion_body("")
            body["choices"][0]["message"]["tool_calls"] = [
                {
                    "id": "c",
                    "type": "function",
                    "function": {"name": "f", "arguments": f'{{"cf": "{placeholder}"}}'},
                }
            ]
            fake_upstream.set_json(body)
            response = await client.post(
                "/v1/chat/completions",
                json={"model": seeded.model.name, "messages": [{"role": "user", "content": CF}]},
                headers=seeded.auth,
            )
            call = response.json()["choices"][0]["message"]["tool_calls"][0]
            assert placeholder in call["function"]["arguments"], overrides

        # Off for arguments, still on for the answer.
        install(app, restore_in_tool_arguments=False)
        placeholder = await issued(app)
        fake_upstream.set_json(completion_body(f"is {placeholder}"))
        response = await client.post(
            "/v1/chat/completions",
            json={"model": seeded.model.name, "messages": [{"role": "user", "content": CF}]},
            headers=seeded.auth,
        )
        assert response.json()["choices"][0]["message"]["content"] == f"is {CF}"


class TestResponsesSurface:
    BODY: ClassVar[dict[str, Any]] = {"input": f"my code is {CF}", "stream": True}

    async def test_a_function_call_in_a_wire_body_is_restored(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app)
        placeholder = await issued(app)
        fake_upstream.set_json(
            {
                "id": "r1",
                "object": "response",
                "output": [
                    {
                        "type": "function_call",
                        "name": "fill",
                        "arguments": f'{{"cf": "{placeholder}"}}',
                    }
                ],
                "usage": {},
            }
        )
        response = await client.post(
            "/v1/responses",
            json={"model": seeded.model.name, "input": f"code {CF}"},
            headers=seeded.auth,
        )
        assert orjson.loads(response.json()["output"][0]["arguments"]) == {"cf": CF}

    async def test_streamed_function_arguments_stay_valid_json(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app)
        placeholder = await issued(app)
        arguments = f'{{"cf": "{placeholder}", "n": 1}}'
        events: list[dict[str, Any]] = [
            {"type": "response.function_call_arguments.delta", "delta": p, "output_index": 0}
            for p in pieces(arguments, 4)
        ]
        events += [
            {"type": "response.function_call_arguments.done", "arguments": arguments},
            {
                "type": "response.output_item.done",
                "item": {"type": "function_call", "arguments": arguments},
            },
        ]
        fake_upstream.set_stream(event_frames(events))
        got = await post_stream(
            client, "/v1/responses", {"model": seeded.model.name, **self.BODY}, seeded
        )
        streamed = "".join(
            e["delta"] for e in got if e["type"] == "response.function_call_arguments.delta"
        )
        assert orjson.loads(streamed) == {"cf": CF, "n": 1}
        done = next(e for e in got if e["type"] == "response.function_call_arguments.done")
        assert orjson.loads(done["arguments"]) == {"cf": CF, "n": 1}


class TestMessagesSurface:
    BODY: ClassVar[dict[str, Any]] = {
        "max_tokens": 100,
        "stream": True,
        "messages": [{"role": "user", "content": f"my code is {CF}"}],
    }

    async def test_text_and_tool_input_are_restored_when_streamed(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app)
        placeholder = await issued(app)
        answer = f"Use `{placeholder}`"
        arguments = f'{{"cf": "{placeholder}"}}'
        events: list[dict[str, Any]] = [
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}
        ]
        events += [
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": p},
            }
            for p in pieces(answer, 3)
        ]
        events += [
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use"}},
        ]
        events += [
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": p},
            }
            for p in pieces(arguments, 4)
        ]
        events += [
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {}},
            {"type": "message_stop"},
        ]
        fake_upstream.set_stream(event_frames(events, named=True))
        got = await post_stream(
            client, "/v1/messages", {"model": seeded.model.name, **self.BODY}, seeded
        )
        deltas = [e for e in got if e["type"] == "content_block_delta"]
        text = "".join(e["delta"].get("text", "") for e in deltas)
        partial = "".join(e["delta"].get("partial_json", "") for e in deltas)
        assert text == answer.replace(placeholder, CF)
        assert orjson.loads(partial) == {"cf": CF}

    async def test_a_buffered_tool_use_input_is_restored(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app)
        placeholder = await issued(app)
        fake_upstream.set_json(
            {
                "id": "m1",
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t", "name": "fill", "input": {"cf": placeholder}}
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        )
        response = await client.post(
            "/v1/messages",
            json={**self.BODY, "model": seeded.model.name, "stream": False},
            headers=seeded.auth,
        )
        assert response.json()["content"][0]["input"] == {"cf": CF}
