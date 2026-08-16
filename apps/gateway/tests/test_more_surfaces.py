"""Responses, Anthropic Messages and image generation (ADR 0030).

Three surfaces, one ledger. What is pinned here is the part that would
otherwise be discovered from an invoice: that each surface's usage is read with
its own convention, that the usage split across two Anthropic frames is not
half-lost, that images are billed by the picture when that is how they are
priced, and that redaction still covers the request and the response on shapes
that keep their text somewhere new.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import orjson
from conftest import FakeUpstream, Seeded
from gateway.models import (
    ApiSurface,
    GroupModelAccess,
    ModelDef,
    ModelKind,
    ModelPrice,
    UsageRecord,
    UsageSource,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def add_model(
    session: AsyncSession,
    seeded: Seeded,
    *,
    name: str,
    kind: ModelKind,
    upstream: str = "vendor/x",
    per_image: str | None = None,
    input_per_mtok: str = "1",
    output_per_mtok: str = "2",
    cache_read_per_mtok: str | None = None,
    cache_write_per_mtok: str | None = None,
) -> ModelDef:
    model = ModelDef(name=name, upstream_model=upstream, provider_id=seeded.provider.id, kind=kind)
    session.add(model)
    await session.flush()
    session.add(
        ModelPrice(
            model_id=model.id,
            input_per_mtok=Decimal(input_per_mtok),
            output_per_mtok=Decimal(output_per_mtok),
            cache_read_per_mtok=Decimal(cache_read_per_mtok) if cache_read_per_mtok else None,
            cache_write_per_mtok=Decimal(cache_write_per_mtok) if cache_write_per_mtok else None,
            per_image=Decimal(per_image) if per_image else None,
            currency="EUR",
        )
    )
    session.add(GroupModelAccess(group_id=seeded.group.id, model_id=model.id))
    await session.commit()
    return model


async def latest_record(session: AsyncSession) -> UsageRecord:
    return (
        (
            await session.execute(
                select(UsageRecord).order_by(UsageRecord.created_at.desc()).limit(1)
            )
        )
        .scalars()
        .one()
    )


def sse(events: list[tuple[str | None, dict[str, Any]]]) -> bytes:
    out = b""
    for name, payload in events:
        if name:
            out += f"event: {name}\n".encode()
        out += b"data: " + orjson.dumps(payload) + b"\n\n"
    return out


# --------------------------------------------------------------------------
# Responses
# --------------------------------------------------------------------------


class TestResponses:
    async def test_a_string_input_stays_a_string_upstream(
        self, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        """Wrapping it in a list would change what the provider is asked."""
        fake_upstream.set_json(
            {
                "id": "r1",
                "object": "response",
                "output": [],
                "output_text": "hi",
                "usage": {"input_tokens": 5, "output_tokens": 2},
            }
        )
        response = await client.post(
            "/v1/responses",
            json={"model": seeded.model.name, "input": "hello"},
            headers=seeded.auth,
        )
        assert response.status_code == 200
        assert fake_upstream.last_body["input"] == "hello"

    async def test_a_list_input_is_forwarded_as_a_list(
        self, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        fake_upstream.set_json({"id": "r1", "object": "response", "output": [], "usage": {}})
        await client.post(
            "/v1/responses",
            json={
                "model": seeded.model.name,
                "input": [{"role": "user", "content": "hello"}],
            },
            headers=seeded.auth,
        )
        assert isinstance(fake_upstream.last_body["input"], list)

    async def test_usage_is_read_with_the_responses_names(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session: AsyncSession,
    ) -> None:
        fake_upstream.set_json(
            {
                "id": "r1",
                "object": "response",
                "output": [],
                "output_text": "",
                "usage": {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500},
            }
        )
        await client.post(
            "/v1/responses",
            json={"model": seeded.model.name, "input": "hi"},
            headers=seeded.auth,
        )
        record = await latest_record(session)
        assert (record.prompt_tokens, record.completion_tokens) == (1000, 500)
        assert record.usage_source is UsageSource.UPSTREAM_EXACT
        assert record.api_surface is ApiSurface.RESPONSES

    async def test_the_surface_is_recorded(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session: AsyncSession,
    ) -> None:
        """The same model is reachable three ways; the row says which was used."""
        fake_upstream.set_json({"id": "r1", "object": "response", "output": [], "usage": {}})
        await client.post(
            "/v1/responses",
            json={"model": seeded.model.name, "input": "hi"},
            headers=seeded.auth,
        )
        assert (await latest_record(session)).api_surface is ApiSurface.RESPONSES

    async def test_server_side_state_is_refused_with_a_reason(
        self, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        """A stored prefix is billed to us on every turn and is invisible to the ledger."""
        response = await client.post(
            "/v1/responses",
            json={"model": seeded.model.name, "input": "hi", "previous_response_id": "resp_1"},
            headers=seeded.auth,
        )
        assert response.status_code == 400
        assert "previous_response_id" in response.json()["error"]["message"]

    async def test_store_is_refused_too(self, client: httpx.AsyncClient, seeded: Seeded) -> None:
        response = await client.post(
            "/v1/responses",
            json={"model": seeded.model.name, "input": "hi", "store": True},
            headers=seeded.auth,
        )
        assert response.status_code == 400

    async def test_neither_flag_reaches_the_provider(
        self, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        """`store: false` is our default, not the caller's instruction."""
        fake_upstream.set_json({"id": "r1", "object": "response", "output": [], "usage": {}})
        await client.post(
            "/v1/responses",
            json={"model": seeded.model.name, "input": "hi"},
            headers=seeded.auth,
        )
        assert "store" not in fake_upstream.last_body
        assert "previous_response_id" not in fake_upstream.last_body

    async def test_our_model_name_is_echoed_back(
        self, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        fake_upstream.set_json(
            {"id": "r1", "object": "response", "model": "vendor/x", "output": [], "usage": {}}
        )
        response = await client.post(
            "/v1/responses",
            json={"model": seeded.model.name, "input": "hi"},
            headers=seeded.auth,
        )
        assert response.json()["model"] == seeded.model.name

    async def test_streaming_reads_usage_from_the_terminal_event(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session: AsyncSession,
    ) -> None:
        completed = {
            "type": "response.completed",
            "response": {
                "id": "r1",
                "object": "response",
                "output": [],
                "output_text": "hello",
                "usage": {"input_tokens": 700, "output_tokens": 300},
            },
        }
        fake_upstream.set_stream(
            [
                sse(
                    [
                        (None, {"type": "response.created", "response": {"id": "r1"}}),
                        (None, {"type": "response.output_text.delta", "delta": "hello"}),
                        (None, completed),
                    ]
                )
            ]
        )
        async with client.stream(
            "POST",
            "/v1/responses",
            json={"model": seeded.model.name, "input": "hi", "stream": True},
            headers=seeded.auth,
        ) as response:
            body = "".join([chunk async for chunk in response.aiter_text()])

        assert "hello" in body
        record = await latest_record(session)
        assert (record.prompt_tokens, record.completion_tokens) == (700, 300)
        assert record.streamed is True

    async def test_an_incomplete_generation_is_still_billed(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session: AsyncSession,
    ) -> None:
        """Truncated output was still generated, and the provider still charged."""
        fake_upstream.set_stream(
            [
                sse(
                    [
                        (None, {"type": "response.output_text.delta", "delta": "par"}),
                        (
                            None,
                            {
                                "type": "response.incomplete",
                                "response": {
                                    "id": "r1",
                                    "status": "incomplete",
                                    "output": [],
                                    "usage": {"input_tokens": 100, "output_tokens": 9},
                                },
                            },
                        ),
                    ]
                )
            ]
        )
        async with client.stream(
            "POST",
            "/v1/responses",
            json={"model": seeded.model.name, "input": "hi", "stream": True},
            headers=seeded.auth,
        ) as response:
            async for _ in response.aiter_text():
                pass

        record = await latest_record(session)
        assert record.completion_tokens == 9
        assert record.usage_source is UsageSource.UPSTREAM_EXACT


# --------------------------------------------------------------------------
# Anthropic Messages
# --------------------------------------------------------------------------


class TestMessages:
    async def test_the_prompt_is_the_sum_of_the_three_slices(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session: AsyncSession,
    ) -> None:
        """Anthropic's `input_tokens` is the uncached remainder, not the prompt."""
        fake_upstream.set_json(
            {
                "id": "m1",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "hi"}],
                "usage": {
                    "input_tokens": 600,
                    "cache_creation_input_tokens": 100,
                    "cache_read_input_tokens": 300,
                    "output_tokens": 50,
                },
            }
        )
        await client.post(
            "/v1/messages",
            json={
                "model": seeded.model.name,
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "hello"}],
            },
            headers=seeded.auth,
        )
        record = await latest_record(session)
        assert record.prompt_tokens == 1000
        assert record.cached_prompt_tokens == 300
        assert record.api_surface is ApiSurface.MESSAGES

    async def test_streaming_merges_usage_across_two_frames(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session: AsyncSession,
    ) -> None:
        """The load-bearing one.

        `message_start` has the input count and `message_delta` the output
        count. Letting the last frame win — correct for OpenAI — records a
        prompt of zero here.
        """
        fake_upstream.set_stream(
            [
                sse(
                    [
                        (
                            "message_start",
                            {
                                "type": "message_start",
                                "message": {
                                    "id": "m1",
                                    "type": "message",
                                    "role": "assistant",
                                    "content": [],
                                    "usage": {"input_tokens": 800, "output_tokens": 1},
                                },
                            },
                        ),
                        (
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": 0,
                                "delta": {"type": "text_delta", "text": "hello"},
                            },
                        ),
                        (
                            "message_delta",
                            {
                                "type": "message_delta",
                                "delta": {"stop_reason": "end_turn"},
                                "usage": {"output_tokens": 400},
                            },
                        ),
                        ("message_stop", {"type": "message_stop"}),
                    ]
                )
            ]
        )
        async with client.stream(
            "POST",
            "/v1/messages",
            json={
                "model": seeded.model.name,
                "max_tokens": 100,
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=seeded.auth,
        ) as response:
            body = "".join([chunk async for chunk in response.aiter_text()])

        assert "hello" in body
        record = await latest_record(session)
        assert record.prompt_tokens == 800, "the input count from message_start was lost"
        assert record.completion_tokens == 400
        assert record.finish_reason == "end_turn"

    async def test_named_events_survive_the_pipeline(
        self, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        """Clients switch on `event:`; a frame without one is invisible to them."""
        fake_upstream.set_stream(
            [
                sse(
                    [
                        (
                            "message_start",
                            {"type": "message_start", "message": {"id": "m1", "usage": {}}},
                        ),
                        ("message_stop", {"type": "message_stop"}),
                    ]
                )
            ]
        )
        async with client.stream(
            "POST",
            "/v1/messages",
            json={
                "model": seeded.model.name,
                "max_tokens": 10,
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=seeded.auth,
        ) as response:
            body = "".join([chunk async for chunk in response.aiter_text()])

        assert "event: message_start" in body
        assert "event: message_stop" in body

    async def test_the_system_prompt_is_redacted_and_put_back(
        self, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        """A name in a system prompt is as sensitive as one in a user turn."""
        fake_upstream.set_json(
            {"id": "m1", "type": "message", "content": [], "usage": {"input_tokens": 1}}
        )
        await client.post(
            "/v1/messages",
            json={
                "model": seeded.model.name,
                "max_tokens": 10,
                "system": "You help Alice.",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=seeded.auth,
        )
        # The noop redactor is configured in tests, so the assertion is about
        # shape rather than substitution: the system prompt is still a string
        # and the messages array does not contain it.
        assert fake_upstream.last_body["system"] == "You help Alice."
        assert len(fake_upstream.last_body["messages"]) == 1

    async def test_a_structured_system_prompt_keeps_its_shape(
        self, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        fake_upstream.set_json(
            {"id": "m1", "type": "message", "content": [], "usage": {"input_tokens": 1}}
        )
        await client.post(
            "/v1/messages",
            json={
                "model": seeded.model.name,
                "max_tokens": 10,
                "system": [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}],
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=seeded.auth,
        )
        assert fake_upstream.last_body["system"] == [
            {"type": "text", "text": "one"},
            {"type": "text", "text": "two"},
        ]

    async def test_max_tokens_is_required(self, client: httpx.AsyncClient, seeded: Seeded) -> None:
        """Anthropic requires it. Inventing a default would truncate silently."""
        response = await client.post(
            "/v1/messages",
            json={"model": seeded.model.name, "messages": [{"role": "user", "content": "hi"}]},
            headers=seeded.auth,
        )
        assert response.status_code == 400

    async def test_the_anthropic_version_header_is_sent(
        self, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        """Anthropic's own API rejects a request without it."""
        fake_upstream.set_json(
            {"id": "m1", "type": "message", "content": [], "usage": {"input_tokens": 1}}
        )
        await client.post(
            "/v1/messages",
            json={
                "model": seeded.model.name,
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=seeded.auth,
        )
        assert fake_upstream.headers[-1]["anthropic-version"] == "2023-06-01"


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------


class TestImages:
    async def test_a_per_image_model_is_billed_by_the_picture(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session: AsyncSession,
    ) -> None:
        model = await add_model(
            session,
            seeded,
            name="pic-1",
            kind=ModelKind.IMAGE,
            per_image="0.04",
            input_per_mtok="0",
            output_per_mtok="0",
        )
        fake_upstream.set_json(
            {"created": 1, "size": "1024x1024", "data": [{"b64_json": "aaa"}] * 3}
        )
        response = await client.post(
            "/v1/images/generations",
            json={"model": model.name, "prompt": "a cat", "n": 3},
            headers=seeded.auth,
        )
        assert response.status_code == 200
        record = await latest_record(session)
        assert record.image_count == 3
        assert record.cost == Decimal("0.12")
        assert record.api_surface is ApiSurface.IMAGES
        # The picture count is exact and is what the bill is made of, so the
        # row must not be labelled an estimate just because no tokens came back.
        assert record.usage_source is UsageSource.UPSTREAM_EXACT
        assert record.total_tokens == 0

    async def test_the_size_is_recorded(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session: AsyncSession,
    ) -> None:
        """Nothing prices by size yet; without this the ledger could never start."""
        model = await add_model(
            session, seeded, name="pic-2", kind=ModelKind.IMAGE, per_image="0.01"
        )
        fake_upstream.set_json({"created": 1, "size": "1536x1024", "data": [{"b64_json": "aaa"}]})
        await client.post(
            "/v1/images/generations",
            json={"model": model.name, "prompt": "a cat", "size": "auto"},
            headers=seeded.auth,
        )
        record = await latest_record(session)
        # The provider's answer, not the caller's "auto".
        assert record.image_size == "1536x1024"

    async def test_billing_follows_what_came_back_not_what_was_asked(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session: AsyncSession,
    ) -> None:
        model = await add_model(
            session,
            seeded,
            name="pic-3",
            kind=ModelKind.IMAGE,
            per_image="0.10",
            input_per_mtok="0",
            output_per_mtok="0",
        )
        fake_upstream.set_json({"created": 1, "data": [{"b64_json": "a"}, {"b64_json": "b"}]})
        await client.post(
            "/v1/images/generations",
            json={"model": model.name, "prompt": "a cat", "n": 4},
            headers=seeded.auth,
        )
        record = await latest_record(session)
        assert record.image_count == 2
        assert record.cost == Decimal("0.20")

    async def test_a_token_priced_image_model_uses_its_usage(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session: AsyncSession,
    ) -> None:
        model = await add_model(
            session,
            seeded,
            name="pic-4",
            kind=ModelKind.IMAGE,
            input_per_mtok="5",
            output_per_mtok="40",
        )
        fake_upstream.set_json(
            {
                "created": 1,
                "data": [{"b64_json": "a"}],
                "usage": {
                    "input_tokens": 1_000_000,
                    "output_tokens": 1_000_000,
                    "total_tokens": 2_000_000,
                },
            }
        )
        await client.post(
            "/v1/images/generations",
            json={"model": model.name, "prompt": "a cat"},
            headers=seeded.auth,
        )
        record = await latest_record(session)
        assert record.cost == Decimal("45")

    async def test_the_revised_prompt_is_restored(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session: AsyncSession,
    ) -> None:
        """It is our text coming back, so a placeholder in it would be ours."""
        model = await add_model(
            session, seeded, name="pic-5", kind=ModelKind.IMAGE, per_image="0.01"
        )
        fake_upstream.set_json(
            {"created": 1, "data": [{"b64_json": "a", "revised_prompt": "a fine cat"}]}
        )
        response = await client.post(
            "/v1/images/generations",
            json={"model": model.name, "prompt": "a cat"},
            headers=seeded.auth,
        )
        assert response.json()["data"][0]["revised_prompt"] == "a fine cat"

        # ...and it is not counted as generated output. It is the provider
        # echoing our own prompt, not text it charged for.
        record = await latest_record(session)
        assert record.completion_tokens == 0

    async def test_n_is_bounded(
        self, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        """`n` multiplies the bill, so it is checked before the request is made."""
        model = await add_model(
            session, seeded, name="pic-6", kind=ModelKind.IMAGE, per_image="0.01"
        )
        response = await client.post(
            "/v1/images/generations",
            json={"model": model.name, "prompt": "a cat", "n": 500},
            headers=seeded.auth,
        )
        assert response.status_code == 400


# --------------------------------------------------------------------------
# Kind routing across all five surfaces
# --------------------------------------------------------------------------


class TestKindRouting:
    async def test_an_image_model_is_refused_by_the_chat_route(
        self, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        model = await add_model(session, seeded, name="pic-7", kind=ModelKind.IMAGE)
        response = await client.post(
            "/v1/chat/completions",
            json={"model": model.name, "messages": [{"role": "user", "content": "hi"}]},
            headers=seeded.auth,
        )
        assert response.status_code == 400
        assert "/v1/images/generations" in response.json()["error"]["message"]

    async def test_a_chat_model_is_refused_by_the_image_route(
        self, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        response = await client.post(
            "/v1/images/generations",
            json={"model": seeded.model.name, "prompt": "a cat"},
            headers=seeded.auth,
        )
        assert response.status_code == 400
        message = response.json()["error"]["message"]
        assert "/v1/chat/completions" in message and "/v1/messages" in message

    async def test_a_chat_model_is_accepted_on_all_three_chat_surfaces(
        self, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        """Responses and Messages are shapes, not kinds."""
        fake_upstream.set_json(
            {
                "id": "x",
                "object": "response",
                "type": "message",
                "content": [],
                "output": [],
                "usage": {},
            }
        )
        first = await client.post(
            "/v1/responses",
            json={"model": seeded.model.name, "input": "hi"},
            headers=seeded.auth,
        )
        second = await client.post(
            "/v1/messages",
            json={
                "model": seeded.model.name,
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=seeded.auth,
        )
        assert (first.status_code, second.status_code) == (200, 200)

    async def test_an_embedding_model_is_refused_by_responses(
        self, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        model = await add_model(session, seeded, name="emb-x", kind=ModelKind.EMBEDDING)
        response = await client.post(
            "/v1/responses",
            json={"model": model.name, "input": "hi"},
            headers=seeded.auth,
        )
        assert response.status_code == 400
        assert "/v1/embeddings" in response.json()["error"]["message"]


class TestFailuresAreNotBilled:
    async def test_a_refused_response_releases_the_reservation(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session: AsyncSession,
    ) -> None:
        fake_upstream.set_json({"error": {"message": "nope"}}, status=400)
        response = await client.post(
            "/v1/responses",
            json={"model": seeded.model.name, "input": "hi"},
            headers=seeded.auth,
        )
        assert response.status_code == 400
        record = await latest_record(session)
        assert record.total_tokens == 0
        assert record.cost == Decimal(0)

    async def test_a_refused_image_request_is_not_billed_for_pictures(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session: AsyncSession,
    ) -> None:
        """`n` was reserved; none were produced, so none are charged."""
        model = await add_model(
            session, seeded, name="pic-8", kind=ModelKind.IMAGE, per_image="1.00"
        )
        fake_upstream.set_json({"error": {"message": "content policy"}}, status=400)
        await client.post(
            "/v1/images/generations",
            json={"model": model.name, "prompt": "x", "n": 5},
            headers=seeded.auth,
        )
        record = await latest_record(session)
        assert record.image_count == 0
        assert record.cost == Decimal(0)
