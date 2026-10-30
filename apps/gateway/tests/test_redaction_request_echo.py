"""What a follow-up request echoes back is screened like anything else.

Restoring reasoning and tool arguments in the response puts real values into
text the client will send again: the next request of the same turn replays the
assistant's tool calls and reasoning. Request redaction has to cover those
fields, or the real value goes to the provider on the second round.
"""

from __future__ import annotations

from typing import Any

import httpx
import orjson
import pytest
from conftest import Seeded
from helpers import completion_body
from llmp_shared import PlaceholderMap
from test_redaction_restore_surfaces import (
    CF,
    delta_chunk,
    frames,
    install,
    issued,
    pieces,
    post_stream,
)
from test_redaction_tool_arguments import streamed_arguments, tool_chunk


class TestRequestSide:
    async def test_chat_echo_of_tool_calls_and_reasoning(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app)
        fake_upstream.set_json(completion_body("ok"))
        untouched = '{"n": 1,  "q": "x"}'
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": seeded.model.name,
                "messages": [
                    {"role": "user", "content": f"fill with {CF}"},
                    {
                        "role": "assistant",
                        "content": None,
                        "reasoning_content": f"the code is {CF}",
                        "reasoning": f"again {CF}",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "fill", "arguments": f'{{"cf": "{CF}"}}'},
                            },
                            {
                                "id": "c2",
                                "type": "function",
                                "function": {"name": "other", "arguments": untouched},
                            },
                        ],
                    },
                    {"role": "tool", "tool_call_id": "c1", "content": f"filled {CF}"},
                ],
            },
            headers=seeded.auth,
        )
        assert response.status_code == 200, response.text
        sent = fake_upstream.last_body["messages"]
        assert CF not in orjson.dumps(sent).decode()
        calls = sent[2]["tool_calls"]
        # Still valid JSON, with the placeholder where the value was.
        assert orjson.loads(calls[0]["function"]["arguments"])["cf"].startswith("<IT_FISCAL_CODE_")
        # A call with nothing to hide is not re-serialised.
        assert calls[1]["function"]["arguments"] == untouched
        assert sent[2]["reasoning_content"].startswith("the code is <IT_FISCAL_CODE_")

    async def test_the_callers_own_messages_are_not_mutated(self) -> None:
        from test_redaction_http import FakeDetector

        detector = FakeDetector({CF: "IT_FISCAL_CODE"})
        messages = [
            {
                "role": "assistant",
                "tool_calls": [{"function": {"name": "f", "arguments": f'{{"cf": "{CF}"}}'}}],
            }
        ]
        outcome = await detector.redactor().redact_request(messages)
        assert CF in messages[0]["tool_calls"][0]["function"]["arguments"]
        assert CF not in outcome.messages[0]["tool_calls"][0]["function"]["arguments"]

    async def test_arguments_that_are_not_json_are_screened_as_text(self) -> None:
        from test_redaction_http import FakeDetector

        detector = FakeDetector({CF: "IT_FISCAL_CODE"})
        outcome = await detector.redactor().redact_request(
            [
                {
                    "role": "assistant",
                    "tool_calls": [{"function": {"name": "f", "arguments": f'{{"cf": {CF}'}}],
                }
            ]
        )
        assert CF not in orjson.dumps(outcome.messages).decode()

    async def test_responses_echo(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app)
        fake_upstream.set_json({"id": "r", "object": "response", "output": [], "usage": {}})
        response = await client.post(
            "/v1/responses",
            json={
                "model": seeded.model.name,
                "input": [
                    {"role": "user", "content": f"fill with {CF}"},
                    {
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": f"code {CF}"}],
                    },
                    {
                        "type": "function_call",
                        "call_id": "c1",
                        "name": "fill",
                        "arguments": f'{{"cf": "{CF}"}}',
                    },
                    {"type": "function_call_output", "call_id": "c1", "output": f"ok {CF}"},
                ],
            },
            headers=seeded.auth,
        )
        assert response.status_code == 200, response.text
        sent = fake_upstream.last_body["input"]
        assert CF not in orjson.dumps(sent).decode()
        assert orjson.loads(sent[2]["arguments"])["cf"].startswith("<IT_FISCAL_CODE_")

    async def test_messages_echo(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app)
        fake_upstream.set_json(
            {
                "id": "m",
                "type": "message",
                "role": "assistant",
                "content": [],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        )
        response = await client.post(
            "/v1/messages",
            json={
                "model": seeded.model.name,
                "max_tokens": 10,
                "messages": [
                    {"role": "user", "content": f"fill with {CF}"},
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "t1",
                                "name": "fill",
                                "input": {"cf": CF, "n": 1, "nested": [CF]},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": "t1", "content": f"ok {CF}"},
                            {
                                "type": "tool_result",
                                "tool_use_id": "t1",
                                "content": [{"type": "text", "text": f"ok {CF}"}],
                            },
                        ],
                    },
                ],
            },
            headers=seeded.auth,
        )
        assert response.status_code == 200, response.text
        sent = fake_upstream.last_body["messages"]
        assert CF not in orjson.dumps(sent).decode()
        tool_use = sent[1]["content"][0]
        assert tool_use["input"]["n"] == 1
        assert tool_use["input"]["cf"].startswith("<IT_FISCAL_CODE_")


class TestJsonRestoreIsConservative:
    def mapped(self) -> PlaceholderMap:
        mapping = PlaceholderMap()
        mapping.add("X", "VALUE", "<X_ABCDEFGHIJ>")
        return mapping

    def test_an_escape_that_is_not_a_placeholder_is_left_alone(self) -> None:
        text = '{"code": "x\\\\u003cy", "other": "\\u003cz\\u003e"}'
        assert self.mapped().restore_in_json(text) == text

    def test_a_backslash_before_the_escape_is_respected(self) -> None:
        text = '{"a": "\\\\\\u003cX_ABCDEFGHIJ\\u003e"}'
        restored = self.mapped().restore_in_json(text)
        assert orjson.loads(restored) == {"a": "\\VALUE"}
        literal = '{"a": "\\\\u003cX_ABCDEFGHIJ\\u003e"}'
        assert self.mapped().restore_in_json(literal) == literal

    def test_mixed_spellings_are_found(self) -> None:
        for text in (
            '{"a": "<X_ABCDEFGHIJ>"}',
            '{"a": "\\u003cX_ABCDEFGHIJ>"}',
            '{"a": "<X_ABCDEFGHIJ\\u003e"}',
            '{"a": "\\u003CX_ABCDEFGHIJ\\u003E"}',
        ):
            assert orjson.loads(self.mapped().restore_in_json(text)) == {"a": "VALUE"}


class TestStreamedArguments:
    async def test_an_escaped_placeholder_split_anywhere_is_restored(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app)
        placeholder = await issued(app)
        escaped = placeholder.replace("<", "\\u003c").replace(">", "\\u003e")
        arguments = f'{{"cf": "{escaped}"}}'
        request = {
            "model": seeded.model.name,
            "stream": True,
            "messages": [{"role": "user", "content": f"code {CF}"}],
        }
        for cut in range(1, len(arguments)):
            chunks = [tool_chunk(arguments[:cut]), tool_chunk(arguments[cut:])]
            chunks.append(tool_chunk("", finish="tool_calls"))
            fake_upstream.set_stream(frames(chunks))
            events = await post_stream(client, "/v1/chat/completions", request, seeded)
            assert orjson.loads(streamed_arguments(events, 0)) == {"cf": CF}, cut

    async def test_calls_without_an_index_do_not_merge(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app)
        placeholder = await issued(app)
        first = f'{{"a": "{placeholder}"}}'
        second = f'{{"b": "{placeholder}"}}'

        def fragment(text: str, identifier: str | None = None) -> dict[str, Any]:
            call: dict[str, Any] = {"function": {"arguments": text}}
            if identifier:
                call["id"] = identifier
                call["function"]["name"] = "f"
            return {
                "id": "x",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "m",
                "choices": [{"index": 0, "delta": {"tool_calls": [call]}}],
            }

        for size in (4, 9, 20):
            chunks = [
                fragment(p, "a" if i == 0 else None) for i, p in enumerate(pieces(first, size))
            ]
            chunks += [
                fragment(p, "b" if i == 0 else None) for i, p in enumerate(pieces(second, size))
            ]
            chunks.append(delta_chunk({}, "stop"))
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
            # A call starts at the fragment that carries its id; the rest continue it.
            calls: list[str] = []
            for e in events:
                for ch in e.get("choices") or []:
                    for c in ch.get("delta", {}).get("tool_calls") or []:
                        if c.get("id") or not calls:
                            calls.append("")
                        calls[-1] += c["function"].get("arguments") or ""
            assert [orjson.loads(c) for c in calls] == [{"a": CF}, {"b": CF}], size


class TestListContent:
    """A provider may send `content` as a list of parts, streamed or whole."""

    @pytest.mark.parametrize("size", [1, 5, 27])
    async def test_streamed_parts_are_restored(
        self, size: int, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app)
        placeholder = await issued(app)
        text = f"Use `{placeholder}` now"
        chunks = [
            delta_chunk({"content": [{"type": "text", "text": p}]}) for p in pieces(text, size)
        ]
        chunks.append(delta_chunk({}, "stop"))
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
        joined = "".join(
            part.get("text", "")
            for e in events
            for ch in e.get("choices") or []
            for part in (ch.get("delta", {}).get("content") or [])
            if isinstance(part, dict)
        )
        assert joined == text.replace(placeholder, CF)

    async def test_a_whole_list_content_is_restored(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app)
        placeholder = await issued(app)
        body = completion_body("")
        body["choices"][0]["message"]["content"] = [{"type": "text", "text": f"Use {placeholder}"}]
        fake_upstream.set_json(body)
        response = await client.post(
            "/v1/chat/completions",
            json={"model": seeded.model.name, "messages": [{"role": "user", "content": CF}]},
            headers=seeded.auth,
        )
        part = response.json()["choices"][0]["message"]["content"][0]
        assert part["text"] == f"Use {CF}"


class TestResponsesInstructions:
    async def test_an_original_in_instructions_never_reaches_the_provider(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app)
        fake_upstream.set_json({"id": "r", "object": "response", "output": [], "usage": {}})
        for body in ({"input": "hello"}, {"input": [{"role": "user", "content": "hello"}]}):
            response = await client.post(
                "/v1/responses",
                json={"model": seeded.model.name, "instructions": f"The user is {CF}.", **body},
                headers=seeded.auth,
            )
            assert response.status_code == 200, response.text
            sent = fake_upstream.last_body
            assert CF not in orjson.dumps(sent).decode()
            assert sent["instructions"].startswith("The user is <IT_FISCAL_CODE_")
            # The input is still the input, not shifted by the instructions.
            assert sent["input"] in ("hello", [{"role": "user", "content": "hello"}])

    async def test_nothing_to_hide_leaves_instructions_byte_identical_plus_the_note(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        from gateway.redaction.note import system_note

        detector = install(app)
        detector.spans_for.clear()
        fake_upstream.set_json({"id": "r", "object": "response", "output": [], "usage": {}})
        await client.post(
            "/v1/responses",
            json={"model": seeded.model.name, "instructions": "Be  brief.\n", "input": "hi"},
            headers=seeded.auth,
        )
        note = system_note(app.state.redactor._settings.policy)
        assert fake_upstream.last_body["instructions"] == f"Be  brief.\n\n\n{note}"
        assert fake_upstream.last_body["input"] == "hi"

    async def test_the_note_is_not_screened(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        from gateway.redaction.note import system_note

        detector = install(app)
        detector.spans_for["stand in for the user's real data"] = "PERSON"
        fake_upstream.set_json({"id": "r", "object": "response", "output": [], "usage": {}})
        await client.post(
            "/v1/responses",
            json={"model": seeded.model.name, "instructions": "Be brief.", "input": "hi"},
            headers=seeded.auth,
        )
        note = system_note(app.state.redactor._settings.policy)
        assert note and note in fake_upstream.last_body["instructions"]


class TestIndexedFirstFragment:
    async def test_bare_continuations_join_the_numbered_call(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app)
        placeholder = await issued(app)
        arguments = f'{{"who": "{placeholder}", "n": 1234567890}}'

        def fragment(text: str, first: bool) -> dict[str, Any]:
            call: dict[str, Any] = {"function": {"arguments": text}}
            if first:
                call.update({"index": 0, "id": "c1"})
            return {
                "id": "x",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "m",
                "choices": [{"index": 0, "delta": {"tool_calls": [call]}}],
            }

        for size in (3, 8, 20):
            chunks = [fragment(p, i == 0) for i, p in enumerate(pieces(arguments, size))]
            chunks.append(delta_chunk({"content": "tail text"}))
            chunks.append(delta_chunk({}, "tool_calls"))
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
            joined = "".join(
                c["function"].get("arguments") or ""
                for e in events
                for ch in e.get("choices") or []
                for c in ch.get("delta", {}).get("tool_calls") or []
            )
            assert orjson.loads(joined) == {"who": CF, "n": 1234567890}, size


class TestTokenReservation:
    async def test_instructions_are_counted_once(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen: list[str] = []
        estimator = app.state.token_estimator
        original = estimator.count_text

        def spy(text: str, *args: Any, **kwargs: Any) -> int:
            seen.append(text)
            return int(original(text, *args, **kwargs))

        monkeypatch.setattr(estimator, "count_text", spy)
        fake_upstream.set_json({"id": "r", "object": "response", "output": [], "usage": {}})
        await client.post(
            "/v1/responses",
            json={"model": seeded.model.name, "instructions": "Be brief.", "input": "hi"},
            headers=seeded.auth,
        )
        # Once, inside the system message the redactor returned; not again on top.
        assert seen.count("Be brief.") == 1


class TestKnownValues:
    async def test_a_value_found_once_is_replaced_everywhere_in_the_request(self) -> None:
        from test_redaction_http import FakeDetector

        class OnlyInProse(FakeDetector):
            """Misses `Rossi` when it stands alone, as NER does on a bare JSON leaf."""

            def _handle(self, request: httpx.Request) -> httpx.Response:
                response = super()._handle(request)
                body = orjson.loads(response.content)
                texts = orjson.loads(request.content)["texts"]
                for finding in body["findings"]:
                    if texts[finding["index"]] == "Rossi":
                        finding["spans"] = []
                return httpx.Response(200, json=body)

        detector = OnlyInProse({"Rossi": "PERSON"})
        outcome = await detector.redactor().redact_request(
            [
                {"role": "user", "content": "my surname is Rossi"},
                {
                    "role": "assistant",
                    "reasoning_content": "Rossi, said the user.",
                    "tool_calls": [
                        {"function": {"name": "f", "arguments": '{"surname": "Rossi"}'}}
                    ],
                },
                {"role": "tool", "content": "Rossi"},
            ]
        )
        sent = orjson.dumps(outcome.messages).decode()
        assert "Rossi" not in sent
        placeholder = outcome.messages[0]["content"].removeprefix("my surname is ")
        args = orjson.loads(outcome.messages[1]["tool_calls"][0]["function"]["arguments"])
        assert args == {"surname": placeholder}
        assert outcome.messages[2]["content"] == placeholder
        # Restoring still maps back.
        assert outcome.placeholder_map.restore(placeholder) == "Rossi"

    async def test_nothing_substituted_means_nothing_extra(self) -> None:
        from test_redaction_http import FakeDetector

        detector = FakeDetector()
        messages = [{"role": "user", "content": "Rossi"}]
        outcome = await detector.redactor().redact_request(messages)
        assert outcome.messages == messages

    def test_only_whole_words_and_never_inside_a_placeholder(self) -> None:
        from gateway.redaction.http import _known_values

        pattern = _known_values({"Rossi": "<PERSON_X>", "ab": "<SHORT>"})
        assert pattern is not None
        hits = [
            m.group("known")
            for m in pattern.finditer("Rossini, Rossi; <PERSON_ROSSI> ab")
            if m.group("known")
        ]
        assert hits == ["Rossi"]
