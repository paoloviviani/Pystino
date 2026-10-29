"""Restoring placeholders in what a provider sends back, on every surface.

Router-level: a fake detector finds a fiscal code, a fake provider answers with
the placeholder the gateway issued, and the caller must read the real value
back, wherever the provider put the text and however it cut the stream.
"""

from __future__ import annotations

from typing import Any, ClassVar

import httpx
import orjson
from conftest import Seeded
from helpers import completion_body, sse
from test_redaction_http import FakeDetector

CF = "RSSMRA80A01F205X"


def install(app: Any, **settings: Any) -> FakeDetector:
    detector = FakeDetector({CF: "IT_FISCAL_CODE"})
    app.state.redactor = detector.redactor(**settings)
    app.state.redaction = None
    return detector


async def issued(app: Any) -> str:
    """The placeholder the gateway derives for the fiscal code."""
    outcome = await app.state.redactor.redact_request([{"role": "user", "content": CF}])
    return str(outcome.messages[0]["content"])


def frames(chunks: list[dict[str, Any]]) -> list[bytes]:
    return [sse(c) for c in chunks] + [sse("[DONE]")]


def delta_chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    choice: dict[str, Any] = {"index": 0, "delta": delta}
    if finish:
        choice["finish_reason"] = finish
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "upstream/test-model",
        "choices": [choice],
    }


def pieces(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


async def read_stream(
    client: httpx.AsyncClient, seeded: Seeded, path: str = "/v1/chat/completions"
) -> list[dict[str, Any]]:
    response = await client.post(
        path,
        json={
            "model": seeded.model.name,
            "stream": True,
            "messages": [{"role": "user", "content": f"my code is {CF}"}],
        },
        headers=seeded.auth,
    )
    assert response.status_code == 200, response.text
    out: list[dict[str, Any]] = []
    for line in response.text.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            out.append(orjson.loads(line[6:]))
    return out


def joined(events: list[dict[str, Any]], key: str) -> str:
    return "".join(
        str(choice["delta"][key])
        for event in events
        for choice in event.get("choices") or []
        if isinstance(choice.get("delta", {}).get(key), str)
    )


class TestChatStream:
    async def test_content_is_restored_at_any_cut(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app)
        placeholder = await issued(app)
        text = f"Non posso usare `{placeholder}`: e' un segnaposto."
        for size in (1, 2, 3, 5, 7, 11, 26, 27, 28):
            chunks = [delta_chunk({"content": p}) for p in pieces(text, size)]
            chunks.append(delta_chunk({}, "stop"))
            fake_upstream.set_stream(frames(chunks))
            events = await read_stream(client, seeded)
            assert joined(events, "content") == text.replace(placeholder, CF), size

    async def test_reasoning_is_restored_in_both_spellings(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app)
        placeholder = await issued(app)
        thought = f"The user wrote {placeholder} and asks me to use it."
        for key in ("reasoning_content", "reasoning"):
            chunks = [delta_chunk({key: p}) for p in pieces(thought, 4)]
            chunks.append(delta_chunk({"content": "ok"}, "stop"))
            fake_upstream.set_stream(frames(chunks))
            events = await read_stream(client, seeded)
            assert joined(events, key) == thought.replace(placeholder, CF), key
            assert joined(events, "content") == "ok"

    async def test_reasoning_and_content_do_not_share_a_buffer(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        """A placeholder begun in the thinking must not complete in the answer."""
        install(app)
        placeholder = await issued(app)
        half = len(placeholder) // 2
        chunks = [
            delta_chunk({"reasoning_content": f"see {placeholder[:half]}"}),
            delta_chunk({"content": placeholder[half:]}),
            delta_chunk({}, "stop"),
        ]
        fake_upstream.set_stream(frames(chunks))
        events = await read_stream(client, seeded)
        assert joined(events, "content") == placeholder[half:]
        assert joined(events, "reasoning_content") == f"see {placeholder[:half]}"

    async def test_text_held_back_arrives_before_the_finish_frame(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        """A client that stops reading at finish_reason must already have it all."""
        install(app)
        placeholder = await issued(app)
        text = f"Code {placeholder}"
        chunks = [delta_chunk({"content": p}) for p in pieces(text[:-3], 6)]
        chunks.append(delta_chunk({"content": text[-3:]}, "stop"))
        fake_upstream.set_stream(frames(chunks))
        events = await read_stream(client, seeded)
        seen = ""
        for event in events:
            for choice in event.get("choices") or []:
                seen += choice["delta"].get("content") or ""
                if choice.get("finish_reason"):
                    assert seen == text.replace(placeholder, CF)
                    return
        raise AssertionError("no finish frame")


class TestChatBuffered:
    async def test_content_and_reasoning_are_restored(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app)
        placeholder = await issued(app)
        body = completion_body(f"Use `{placeholder}`")
        message = body["choices"][0]["message"]
        message["reasoning_content"] = f"thinking about {placeholder}"
        message["reasoning"] = f"thinking about {placeholder}"
        fake_upstream.set_json(body)
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": seeded.model.name,
                "messages": [{"role": "user", "content": f"code {CF}"}],
            },
            headers=seeded.auth,
        )
        got = response.json()["choices"][0]["message"]
        assert got["content"] == f"Use `{CF}`"
        assert got["reasoning_content"] == f"thinking about {CF}"
        assert got["reasoning"] == f"thinking about {CF}"


def event_frames(events: list[dict[str, Any]], *, named: bool = False) -> list[bytes]:
    out = []
    for event in events:
        data = b"data: " + orjson.dumps(event) + b"\n\n"
        out.append(b"event: " + str(event["type"]).encode() + b"\n" + data if named else data)
    return out


async def post_stream(
    client: httpx.AsyncClient, path: str, body: dict[str, Any], seeded: Seeded
) -> list[dict[str, Any]]:
    response = await client.post(path, json=body, headers=seeded.auth)
    assert response.status_code == 200, response.text
    return [
        orjson.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


class TestResponsesSurface:
    BODY: ClassVar[dict[str, Any]] = {"input": f"my code is {CF}", "stream": True}

    async def test_a_wire_body_without_output_text_is_restored(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        """The wire format has no `output_text`; an SDK adds it afterwards."""
        install(app)
        placeholder = await issued(app)
        fake_upstream.set_json(
            {
                "id": "r1",
                "object": "response",
                "output": [
                    {
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": f"saw {placeholder}"}],
                    },
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": f"Use `{placeholder}`"}],
                    },
                ],
                "usage": {},
            }
        )
        response = await client.post(
            "/v1/responses",
            json={"model": seeded.model.name, "input": f"code {CF}"},
            headers=seeded.auth,
        )
        output = response.json()["output"]
        assert output[0]["summary"][0]["text"] == f"saw {CF}"
        assert output[1]["content"][0]["text"] == f"Use `{CF}`"

    async def test_streamed_text_reasoning_and_the_done_events(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: Any
    ) -> None:
        install(app)
        placeholder = await issued(app)
        answer = f"Use `{placeholder}` now"
        thought = f"saw {placeholder}"
        events: list[dict[str, Any]] = [{"type": "response.created", "response": {"output": []}}]
        events += [
            {"type": "response.reasoning_summary_text.delta", "delta": p, "output_index": 0}
            for p in pieces(thought, 3)
        ]
        events.append(
            {"type": "response.reasoning_summary_text.done", "text": thought, "output_index": 0}
        )
        events += [
            {"type": "response.output_text.delta", "delta": p, "output_index": 1}
            for p in pieces(answer, 5)
        ]
        done_part = {"type": "output_text", "text": answer}
        events += [
            {"type": "response.output_text.done", "text": answer, "output_index": 1},
            {"type": "response.content_part.done", "part": done_part},
            {
                "type": "response.output_item.done",
                "item": {"type": "message", "content": [dict(done_part)]},
            },
            {
                "type": "response.completed",
                "response": {"output": [{"type": "message", "content": [dict(done_part)]}]},
            },
        ]
        fake_upstream.set_stream(event_frames(events))
        got = await post_stream(
            client, "/v1/responses", {"model": seeded.model.name, **self.BODY}, seeded
        )
        text = "".join(e["delta"] for e in got if e["type"] == "response.output_text.delta")
        reasoning = "".join(
            e["delta"] for e in got if e["type"] == "response.reasoning_summary_text.delta"
        )
        assert text == answer.replace(placeholder, CF)
        assert reasoning == thought.replace(placeholder, CF)
        assert placeholder not in orjson.dumps(got).decode()


class TestMessagesSurface:
    BODY: ClassVar[dict[str, Any]] = {
        "max_tokens": 100,
        "stream": True,
        "messages": [{"role": "user", "content": f"my code is {CF}"}],
    }

    async def test_a_buffered_answer_is_restored_and_signed_thinking_is_left_alone(
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
                    {"type": "thinking", "thinking": f"saw {placeholder}", "signature": "sig"},
                    {"type": "text", "text": f"Use {placeholder}"},
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        )
        response = await client.post(
            "/v1/messages",
            json={**self.BODY, "model": seeded.model.name, "stream": False},
            headers=seeded.auth,
        )
        content = response.json()["content"]
        assert content[0]["thinking"] == f"saw {placeholder}"
        assert content[1]["text"] == f"Use {CF}"
