"""``/v1/chat/completions`` served by an Anthropic provider, end to end.

The real ASGI app, a fake upstream that speaks Anthropic's Messages shapes, and
assertions from both sides: what reached ``/v1/messages`` and what the caller
and the ledger saw. Anthropic's bodies and streams below follow the shapes the
docs give (see ``gateway.translate.anthropic_chat``).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import orjson
import pytest
from conftest import FakeUpstream, Seeded
from gateway.models import ModelDef, ModelPrice, UsageRecord
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_redaction_restore_surfaces import CF, install, issued

UPSTREAM_MODEL = "claude-sonnet-4-5-20250929"


@pytest.fixture
async def anthropic(seeded: Seeded, session_factory: async_sessionmaker[AsyncSession]) -> Seeded:
    """The seeded model, served by an Anthropic provider with cache prices."""
    from gateway.models import Provider

    async with session_factory() as db:
        await db.execute(
            update(Provider).where(Provider.id == seeded.provider.id).values(plugin="anthropic")
        )
        await db.execute(
            update(ModelDef)
            .where(ModelDef.id == seeded.model.id)
            .values(upstream_model=UPSTREAM_MODEL, max_output_tokens=4000)
        )
        await db.execute(
            update(ModelPrice)
            .where(ModelPrice.model_id == seeded.model.id)
            .values(
                input_per_mtok=Decimal(3),
                output_per_mtok=Decimal(15),
                cache_read_per_mtok=Decimal("0.3"),
                cache_write_per_mtok=Decimal("3.75"),
            )
        )
        await db.commit()
    return seeded


def message(
    content: list[dict[str, Any]],
    *,
    stop_reason: str = "end_turn",
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": "msg_01ABC",
        "type": "message",
        "role": "assistant",
        "model": UPSTREAM_MODEL,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage or {"input_tokens": 12, "output_tokens": 6},
    }


def event(kind: str, **fields: Any) -> bytes:
    return f"event: {kind}\ndata: ".encode() + orjson.dumps({"type": kind, **fields}) + b"\n\n"


def stream_events(
    blocks: list[tuple[dict[str, Any], list[dict[str, Any]]]],
    *,
    stop_reason: str = "end_turn",
    start_usage: dict[str, Any] | None = None,
    end_usage: dict[str, Any] | None = None,
    split: int | None = None,
) -> list[bytes]:
    """A Messages stream: ``blocks`` is ``[(content_block_start, [deltas])]``."""
    out = [
        event(
            "message_start",
            message={
                "id": "msg_01ABC",
                "type": "message",
                "role": "assistant",
                "model": UPSTREAM_MODEL,
                "content": [],
                "stop_reason": None,
                "usage": start_usage or {"input_tokens": 25, "output_tokens": 1},
            },
        ),
        event("ping"),
    ]
    for index, (start, deltas) in enumerate(blocks):
        out.append(event("content_block_start", index=index, content_block=start))
        out += [event("content_block_delta", index=index, delta=d) for d in deltas]
        out.append(event("content_block_stop", index=index))
    out.append(
        event(
            "message_delta",
            delta={"stop_reason": stop_reason, "stop_sequence": None},
            usage=end_usage or {"output_tokens": 15},
        )
    )
    out.append(event("message_stop"))
    if split:
        data = b"".join(out)
        return [data[i : i + split] for i in range(0, len(data), split)]
    return out


def text_block(*pieces: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return (
        {"type": "text", "text": ""},
        [{"type": "text_delta", "text": piece} for piece in pieces],
    )


def thinking_block(
    *pieces: str, signature: str = "EuYBCkQ"
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return (
        {"type": "thinking", "thinking": "", "signature": ""},
        [
            *({"type": "thinking_delta", "thinking": p} for p in pieces),
            {"type": "signature_delta", "signature": signature},
        ],
    )


def tool_block(
    call_id: str, name: str, *json_pieces: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return (
        {"type": "tool_use", "id": call_id, "name": name, "input": {}},
        [{"type": "input_json_delta", "partial_json": p} for p in json_pieces],
    )


def parse_sse(text: str) -> list[dict[str, Any]]:
    return [
        orjson.loads(line[6:])
        for line in text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


async def last_usage(session_factory: async_sessionmaker[AsyncSession]) -> UsageRecord:
    async with session_factory() as db:
        return (
            await db.execute(select(UsageRecord).order_by(UsageRecord.created_at.desc()).limit(1))
        ).scalar_one()


def ask(anthropic: Seeded, **extra: Any) -> dict[str, Any]:
    return {
        "model": anthropic.model.name,
        "messages": [{"role": "user", "content": "hello"}],
        **extra,
    }


class TestNonStreaming:
    async def test_the_call_goes_to_messages_with_anthropic_auth_and_the_reply_is_openai_shaped(
        self,
        client: httpx.AsyncClient,
        anthropic: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fake_upstream.set_json(
            message(
                [{"type": "text", "text": "Hi there."}],
                usage={
                    "input_tokens": 50,
                    "cache_creation_input_tokens": 200,
                    "cache_read_input_tokens": 1000,
                    "output_tokens": 30,
                },
            )
        )
        response = await client.post(
            "/v1/chat/completions",
            json=ask(
                anthropic,
                messages=[
                    {"role": "system", "content": "Be brief."},
                    {"role": "user", "content": "hello"},
                ],
            ),
            headers=anthropic.auth,
        )
        assert response.status_code == 200, response.text
        assert fake_upstream.urls[-1].endswith("/v1/messages")
        sent = fake_upstream.headers[-1]
        assert sent["x-api-key"] == "upstream-key"
        assert sent["anthropic-version"] == "2023-06-01"
        assert "authorization" not in sent

        body = fake_upstream.last_body
        assert body["model"] == UPSTREAM_MODEL
        assert body["max_tokens"] == 4000  # the model row's max_output_tokens
        assert body["system"][0]["text"] == "Be brief."
        assert body["messages"][0]["content"][0]["text"] == "hello"

        reply = response.json()
        assert reply["object"] == "chat.completion"
        assert reply["model"] == anthropic.model.name  # not the upstream's name
        assert reply["choices"][0]["message"]["content"] == "Hi there."
        assert reply["usage"]["prompt_tokens"] == 1250

        # the ledger: reads and writes at their own prices, the rest at input
        row = await last_usage(session_factory)
        assert (row.prompt_tokens, row.cached_prompt_tokens, row.cache_write_tokens) == (
            1250,
            1000,
            200,
        )
        assert row.completion_tokens == 30
        expected = (
            Decimal("50") * 3
            + Decimal("1000") * Decimal("0.3")
            + Decimal("200") * Decimal("3.75")
            + 30 * 15
        )
        assert abs(Decimal(str(row.cost)) - expected / 1_000_000) < Decimal("0.000001")

    async def test_the_prompt_cache_breakpoints_reach_the_provider(
        self, client: httpx.AsyncClient, anthropic: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        fake_upstream.set_json(message([{"type": "text", "text": "ok"}]))
        await client.post(
            "/v1/chat/completions",
            json=ask(
                anthropic,
                messages=[
                    {"role": "system", "content": "S" * 200},
                    {"role": "user", "content": "q"},
                ],
            ),
            headers=anthropic.auth,
        )
        body = fake_upstream.last_body
        assert body["system"][-1]["cache_control"] == {"type": "ephemeral"}
        assert body["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}

    async def test_prompt_cache_off_is_a_deployment_setting(
        self, app: Any, client: httpx.AsyncClient, anthropic: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        app.state.settings.anthropic.prompt_cache = "off"
        fake_upstream.set_json(message([{"type": "text", "text": "ok"}]))
        await client.post("/v1/chat/completions", json=ask(anthropic), headers=anthropic.auth)
        assert "cache_control" not in orjson.dumps(fake_upstream.last_body).decode()

    async def test_thinking_and_tool_calls_come_back_in_the_openai_fields(
        self, client: httpx.AsyncClient, anthropic: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        fake_upstream.set_json(
            message(
                [
                    {"type": "thinking", "thinking": "I should look it up.", "signature": "SIG"},
                    {"type": "tool_use", "id": "toolu_01", "name": "lookup", "input": {"q": "x"}},
                ],
                stop_reason="tool_use",
            )
        )
        reply = (
            await client.post(
                "/v1/chat/completions",
                json=ask(
                    anthropic,
                    reasoning_effort="medium",
                    max_tokens=16000,
                    tools=[
                        {
                            "type": "function",
                            "function": {"name": "lookup", "parameters": {"type": "object"}},
                        }
                    ],
                ),
                headers=anthropic.auth,
            )
        ).json()
        choice = reply["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        assert choice["message"]["reasoning_content"] == "I should look it up."
        assert choice["message"]["tool_calls"][0]["function"] == {
            "name": "lookup",
            "arguments": '{"q":"x"}',
        }
        assert fake_upstream.last_body["thinking"] == {"type": "enabled", "budget_tokens": 4096}

    async def test_anthropic_errors_become_openai_errors_with_the_status_kept(
        self, client: httpx.AsyncClient, anthropic: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        fake_upstream.set_json(
            {
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "messages.0: bad"},
            },
            status=400,
        )
        response = await client.post(
            "/v1/chat/completions", json=ask(anthropic), headers=anthropic.auth
        )
        assert response.status_code == 400
        assert response.json()["error"]["message"].startswith("messages.0: bad")

    async def test_overloaded_is_a_503(
        self, client: httpx.AsyncClient, anthropic: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        fake_upstream.set_json(
            {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
            status=529,
        )
        response = await client.post(
            "/v1/chat/completions", json=ask(anthropic), headers=anthropic.auth
        )
        assert response.status_code == 503
        assert response.json()["error"]["type"] == "server_error"

    async def test_unsupported_parameters_are_refused_before_any_upstream_call_or_ledger_row(
        self,
        client: httpx.AsyncClient,
        anthropic: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fake_upstream.set_json(message([]))
        response = await client.post(
            "/v1/chat/completions", json=ask(anthropic, n=2), headers=anthropic.auth
        )
        assert response.status_code == 400
        assert "'n' must be 1" in response.json()["error"]["message"]
        assert fake_upstream.bodies == []
        async with session_factory() as db:
            assert (await db.execute(select(UsageRecord))).first() is None

    async def test_a_message_that_cannot_be_expressed_is_a_400_and_settles_the_ledger(
        self, client: httpx.AsyncClient, anthropic: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        fake_upstream.set_json(message([]))
        response = await client.post(
            "/v1/chat/completions",
            json=ask(
                anthropic,
                messages=[
                    {"role": "user", "content": [{"type": "input_audio", "input_audio": {}}]}
                ],
            ),
            headers=anthropic.auth,
        )
        assert response.status_code == 400
        assert "messages[0]" in response.json()["error"]["param"]
        assert fake_upstream.bodies == []

    async def test_a_provider_that_is_not_anthropic_is_untouched(
        self, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        fake_upstream.set_json({"id": "x", "object": "chat.completion", "choices": [], "usage": {}})
        await client.post("/v1/chat/completions", json=ask(seeded), headers=seeded.auth)
        assert fake_upstream.urls[-1].endswith("/chat/completions")


class TestStreaming:
    async def test_text_thinking_tool_calls_and_usage(
        self,
        client: httpx.AsyncClient,
        anthropic: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fake_upstream.set_stream(
            stream_events(
                [
                    thinking_block("Let me ", "think."),
                    text_block("Looking ", "up."),
                    tool_block("toolu_A", "lookup", '{"q": ', '"x"}'),
                    tool_block("toolu_B", "other", "{}"),
                ],
                stop_reason="tool_use",
                start_usage={
                    "input_tokens": 25,
                    "cache_read_input_tokens": 1000,
                    "cache_creation_input_tokens": 200,
                    "output_tokens": 1,
                },
                end_usage={"output_tokens": 77},
            )
        )
        response = await client.post(
            "/v1/chat/completions",
            json=ask(anthropic, stream=True, stream_options={"include_usage": True}),
            headers=anthropic.auth,
        )
        assert response.status_code == 200, response.text
        assert response.text.rstrip().endswith("data: [DONE]")
        chunks = parse_sse(response.text)

        deltas = [c["choices"][0]["delta"] for c in chunks if c["choices"]]
        assert deltas[0] == {"role": "assistant", "content": ""}
        assert "".join(d.get("reasoning_content", "") for d in deltas) == "Let me think."
        assert "".join(d.get("content", "") for d in deltas if d.get("content")) == "Looking up."
        calls = [tc for d in deltas for tc in d.get("tool_calls", [])]
        # tool call indices count tool calls, not content blocks (thinking and text came first)
        assert [
            (c["index"], c.get("id"), c["function"].get("name")) for c in calls if "id" in c
        ] == [
            (0, "toolu_A", "lookup"),
            (1, "toolu_B", "other"),
        ]
        assert "".join(c["function"]["arguments"] for c in calls if c["index"] == 0) == '{"q": "x"}'
        finish = [c for c in chunks if c["choices"] and c["choices"][0]["finish_reason"]]
        assert finish[0]["choices"][0]["finish_reason"] == "tool_calls"
        assert all(c["model"] == anthropic.model.name for c in chunks)

        usage = chunks[-1]
        assert usage["choices"] == []
        # merged across message_start and message_delta
        assert usage["usage"]["prompt_tokens"] == 1225
        assert usage["usage"]["completion_tokens"] == 77

        row = await last_usage(session_factory)
        assert (row.prompt_tokens, row.cached_prompt_tokens, row.cache_write_tokens) == (
            1225,
            1000,
            200,
        )
        assert row.completion_tokens == 77

    async def test_the_usage_frame_is_hidden_unless_asked_for_but_still_billed(
        self,
        client: httpx.AsyncClient,
        anthropic: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fake_upstream.set_stream(stream_events([text_block("hi")]))
        response = await client.post(
            "/v1/chat/completions", json=ask(anthropic, stream=True), headers=anthropic.auth
        )
        assert all("usage" not in c for c in parse_sse(response.text))
        assert (await last_usage(session_factory)).completion_tokens == 15

    @pytest.mark.parametrize("cut", [1, 7, 64])
    async def test_any_chunking_of_the_upstream_bytes(
        self, client: httpx.AsyncClient, anthropic: Seeded, fake_upstream: FakeUpstream, cut: int
    ) -> None:
        fake_upstream.set_stream(stream_events([text_block("Hel", "lo")], split=cut))
        response = await client.post(
            "/v1/chat/completions", json=ask(anthropic, stream=True), headers=anthropic.auth
        )
        text = "".join(
            c["choices"][0]["delta"].get("content", "")
            for c in parse_sse(response.text)
            if c["choices"]
        )
        assert text == "Hello"

    async def test_an_upstream_status_error_before_the_stream_is_a_real_http_error(
        self, client: httpx.AsyncClient, anthropic: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        fake_upstream.set_stream(
            [
                orjson.dumps(
                    {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}
                )
            ],
            status=429,
        )
        response = await client.post(
            "/v1/chat/completions", json=ask(anthropic, stream=True), headers=anthropic.auth
        )
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "rate_limit_error"

    async def test_a_mid_stream_error_event_becomes_an_error_frame(
        self, client: httpx.AsyncClient, anthropic: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        events = stream_events([text_block("par")])[:4]  # start, ping, block start, one delta
        events.append(event("error", error={"type": "overloaded_error", "message": "Overloaded"}))
        fake_upstream.set_stream(events)
        response = await client.post(
            "/v1/chat/completions", json=ask(anthropic, stream=True), headers=anthropic.auth
        )
        frames = parse_sse(response.text)
        assert frames[-1]["error"]["type"] == "server_error"
        assert response.text.rstrip().endswith("[DONE]")

    async def test_a_stream_that_just_stops_is_reported_not_left_looking_complete(
        self, client: httpx.AsyncClient, anthropic: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        fake_upstream.set_stream(stream_events([text_block("par")])[:4])
        response = await client.post(
            "/v1/chat/completions", json=ask(anthropic, stream=True), headers=anthropic.auth
        )
        assert parse_sse(response.text)[-1]["error"]["code"] == "stream_interrupted"


LOOP_TOOL = {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}


def loop_messages(call_id: str, *, reasoning: str | None = None) -> list[dict[str, Any]]:
    assistant: dict[str, Any] = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"q":"x"}'},
            }
        ],
    }
    if reasoning:
        assistant["reasoning_content"] = reasoning
    return [
        {"role": "user", "content": "hello"},
        assistant,
        {"role": "tool", "tool_call_id": call_id, "content": "found"},
    ]


class TestThinkingSignaturesAcrossATurn:
    async def test_non_streamed_round_trip(
        self, client: httpx.AsyncClient, anthropic: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        fake_upstream.set_json(
            message(
                [
                    {"type": "thinking", "thinking": "plan", "signature": "SIG-A"},
                    {"type": "redacted_thinking", "data": "ENC-B"},
                    {"type": "tool_use", "id": "toolu_77", "name": "lookup", "input": {"q": "x"}},
                ],
                stop_reason="tool_use",
            )
        )
        first = await client.post(
            "/v1/chat/completions",
            json=ask(anthropic, tools=[LOOP_TOOL], reasoning_effort="low", max_tokens=16000),
            headers=anthropic.auth,
        )
        call_id = first.json()["choices"][0]["message"]["tool_calls"][0]["id"]

        fake_upstream.set_json(message([{"type": "text", "text": "done"}]))
        second = await client.post(
            "/v1/chat/completions",
            json=ask(
                anthropic,
                messages=loop_messages(call_id, reasoning="plan"),
                tools=[LOOP_TOOL],
                reasoning_effort="low",
                max_tokens=16000,
            ),
            headers=anthropic.auth,
        )
        assert second.status_code == 200, second.text
        sent = fake_upstream.last_body["messages"][1]["content"]
        assert sent[0] == {"type": "thinking", "thinking": "plan", "signature": "SIG-A"}
        assert sent[1] == {"type": "redacted_thinking", "data": "ENC-B"}
        assert sent[2]["type"] == "tool_use"
        assert sent[2]["id"] == "toolu_77"

    async def test_streamed_round_trip_including_the_signature_delta(
        self, client: httpx.AsyncClient, anthropic: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        fake_upstream.set_stream(
            stream_events(
                [
                    thinking_block("pl", "an", signature="SIG-S"),
                    tool_block("toolu_88", "lookup", "{}"),
                ],
                stop_reason="tool_use",
            )
        )
        first = await client.post(
            "/v1/chat/completions",
            json=ask(
                anthropic, stream=True, tools=[LOOP_TOOL], reasoning_effort="low", max_tokens=16000
            ),
            headers=anthropic.auth,
        )
        call_id = next(
            tc["id"]
            for c in parse_sse(first.text)
            if c["choices"]
            for tc in c["choices"][0]["delta"].get("tool_calls", [])
            if "id" in tc
        )
        assert call_id == "toolu_88"

        fake_upstream.set_json(message([{"type": "text", "text": "done"}]))
        await client.post(
            "/v1/chat/completions",
            json=ask(
                anthropic,
                messages=loop_messages(call_id),
                tools=[LOOP_TOOL],
                reasoning_effort="low",
                max_tokens=16000,
            ),
            headers=anthropic.auth,
        )
        sent = fake_upstream.last_body["messages"][1]["content"]
        assert sent[0] == {"type": "thinking", "thinking": "plan", "signature": "SIG-S"}

    async def test_another_caller_cannot_read_the_blocks(
        self, app: Any, client: httpx.AsyncClient, anthropic: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        from gateway.translate.thinking_store import principal_key, store_for

        store = store_for(app.state)
        record = {
            "model": UPSTREAM_MODEL,
            "order": [{"type": "thinking", "thinking": "", "signature": "S"}, "tool:toolu_1"],
        }
        await store.put(principal_key("someone-else"), ["toolu_1"], record, ttl_seconds=60)
        fake_upstream.set_json(message([{"type": "text", "text": "x"}]))
        await client.post(
            "/v1/chat/completions",
            json=ask(anthropic, messages=loop_messages("toolu_1"), tools=[LOOP_TOOL]),
            headers=anthropic.auth,
        )
        sent = fake_upstream.last_body["messages"][1]["content"]
        assert [b["type"] for b in sent] == ["tool_use"]

    async def test_expired_blocks_degrade_to_a_request_without_them(
        self, client: httpx.AsyncClient, anthropic: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        fake_upstream.set_json(message([{"type": "text", "text": "x"}]))
        response = await client.post(
            "/v1/chat/completions",
            json=ask(
                anthropic,
                messages=loop_messages("toolu_gone", reasoning="old"),
                tools=[LOOP_TOOL],
                reasoning_effort="medium",
                max_tokens=16000,
            ),
            headers=anthropic.auth,
        )
        assert response.status_code == 200
        body = fake_upstream.last_body
        assert "thinking" not in body  # manual thinking on this model would be a 400
        assert [b["type"] for b in body["messages"][1]["content"]] == ["tool_use"]


class TestRedactionAroundTheTranslation:
    async def test_redaction_is_applied_before_the_translation(
        self, app: Any, client: httpx.AsyncClient, anthropic: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        install(app)
        placeholder = await issued(app)
        fake_upstream.set_json(message([{"type": "text", "text": "ok"}]))
        await client.post(
            "/v1/chat/completions",
            json=ask(
                anthropic,
                messages=[
                    {"role": "system", "content": f"user code {CF}"},
                    {"role": "user", "content": f"my code is {CF}"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "t1",
                                "type": "function",
                                "function": {"name": "f", "arguments": f'{{"code":"{CF}"}}'},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "t1", "content": f"result {CF}"},
                ],
            ),
            headers=anthropic.auth,
        )
        wire = orjson.dumps(fake_upstream.last_body).decode()
        assert CF not in wire
        assert placeholder in wire

    async def test_restoration_in_a_non_streamed_reply_covers_text_reasoning_and_arguments(
        self, app: Any, client: httpx.AsyncClient, anthropic: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        install(app)
        ph = await issued(app)
        fake_upstream.set_json(
            message(
                [
                    {"type": "thinking", "thinking": f"the code is {ph}", "signature": "S"},
                    {"type": "text", "text": f"Using {ph}."},
                    {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"code": ph}},
                ],
                stop_reason="tool_use",
            )
        )
        reply = (
            await client.post(
                "/v1/chat/completions",
                json=ask(
                    anthropic,
                    messages=[{"role": "user", "content": f"code {CF}"}],
                    tools=[LOOP_TOOL],
                ),
                headers=anthropic.auth,
            )
        ).json()["choices"][0]["message"]
        assert reply["content"] == f"Using {CF}."
        assert reply["reasoning_content"] == f"the code is {CF}"
        assert orjson.loads(reply["tool_calls"][0]["function"]["arguments"]) == {"code": CF}

    @pytest.mark.parametrize("size", [1, 3, 8, 21])
    async def test_restoration_through_the_translated_stream_at_any_cut(
        self,
        app: Any,
        client: httpx.AsyncClient,
        anthropic: Seeded,
        fake_upstream: FakeUpstream,
        size: int,
    ) -> None:
        install(app)
        ph = await issued(app)

        def cut(text: str) -> list[str]:
            return [text[i : i + size] for i in range(0, len(text), size)]

        fake_upstream.set_stream(
            stream_events(
                [
                    thinking_block(*cut(f"The code is {ph} ok")),
                    text_block(*cut(f"Using `{ph}` now")),
                    tool_block("toolu_1", "lookup", *cut('{"code": "' + ph + '"}')),
                ],
                stop_reason="tool_use",
            )
        )
        response = await client.post(
            "/v1/chat/completions",
            json=ask(
                anthropic,
                stream=True,
                messages=[{"role": "user", "content": f"code {CF}"}],
                tools=[LOOP_TOOL],
            ),
            headers=anthropic.auth,
        )
        deltas = [c["choices"][0]["delta"] for c in parse_sse(response.text) if c["choices"]]
        assert "".join(d.get("reasoning_content", "") for d in deltas) == f"The code is {CF} ok"
        assert (
            "".join(d.get("content", "") for d in deltas if d.get("content")) == f"Using `{CF}` now"
        )
        args = "".join(
            tc["function"]["arguments"] for d in deltas for tc in d.get("tool_calls", [])
        )
        assert orjson.loads(args) == {"code": CF}

    async def test_the_stored_thinking_blocks_keep_the_placeholder_the_signature_was_made_over(
        self, app: Any, client: httpx.AsyncClient, anthropic: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        """The client is shown the real value, but what is replayed to Anthropic
        is exactly what Anthropic produced, signature intact."""
        install(app)
        ph = await issued(app)
        fake_upstream.set_json(
            message(
                [
                    {"type": "thinking", "thinking": f"code {ph}", "signature": "SIG"},
                    {"type": "tool_use", "id": "toolu_5", "name": "lookup", "input": {}},
                ],
                stop_reason="tool_use",
            )
        )
        first = (
            await client.post(
                "/v1/chat/completions",
                json=ask(
                    anthropic,
                    messages=[{"role": "user", "content": f"code {CF}"}],
                    tools=[LOOP_TOOL],
                ),
                headers=anthropic.auth,
            )
        ).json()
        assert first["choices"][0]["message"]["reasoning_content"] == f"code {CF}"

        fake_upstream.set_json(message([{"type": "text", "text": "x"}]))
        await client.post(
            "/v1/chat/completions",
            json=ask(
                anthropic,
                messages=[
                    {"role": "user", "content": f"code {CF}"},
                    {
                        "role": "assistant",
                        "content": None,
                        "reasoning_content": f"code {CF}",
                        "tool_calls": [
                            {
                                "id": "toolu_5",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": "{}"},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "toolu_5", "content": "ok"},
                ],
                tools=[LOOP_TOOL],
            ),
            headers=anthropic.auth,
        )
        thinking = fake_upstream.last_body["messages"][1]["content"][0]
        assert thinking == {"type": "thinking", "thinking": f"code {ph}", "signature": "SIG"}
        assert CF not in orjson.dumps(fake_upstream.last_body).decode()


class TestBothSurfacesBillTheSame:
    async def test_the_same_usage_costs_the_same_on_messages_and_on_chat(
        self,
        client: httpx.AsyncClient,
        anthropic: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        usage = {
            "input_tokens": 50,
            "cache_creation_input_tokens": 300,
            "cache_read_input_tokens": 1000,
            "output_tokens": 30,
            "cache_creation": {"ephemeral_5m_input_tokens": 200, "ephemeral_1h_input_tokens": 100},
        }
        fake_upstream.set_json(message([{"type": "text", "text": "hi"}], usage=usage))
        native = await client.post(
            "/v1/messages",
            json={
                "model": anthropic.model.name,
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "hello"}],
            },
            headers=anthropic.auth,
        )
        assert native.status_code == 200, native.text
        via_messages = await last_usage(session_factory)

        fake_upstream.set_json(message([{"type": "text", "text": "hi"}], usage=usage))
        chat = await client.post(
            "/v1/chat/completions", json=ask(anthropic), headers=anthropic.auth
        )
        assert chat.status_code == 200, chat.text
        via_chat = await last_usage(session_factory)

        assert via_messages.id != via_chat.id
        buckets = (
            "prompt_tokens",
            "cached_prompt_tokens",
            "cache_write_tokens",
            "completion_tokens",
        )
        assert [getattr(via_messages, b) for b in buckets] == [
            getattr(via_chat, b) for b in buckets
        ]
        assert [getattr(via_chat, b) for b in buckets] == [1350, 1000, 300, 30]
        assert Decimal(str(via_messages.cost)) == Decimal(str(via_chat.cost))
        # 50 in at 3, 1000 read at 0.3, 200 5m-writes at 3.75, 100 1h-writes at 6
        # (1.6 x 3.75), 30 out at 15
        expected = (
            50 * Decimal(3)
            + 1000 * Decimal("0.3")
            + 200 * Decimal("3.75")
            + 100 * Decimal(6)
            + 30 * 15
        ) / 1_000_000
        assert abs(Decimal(str(via_chat.cost)) - expected) < Decimal("0.0000001")
