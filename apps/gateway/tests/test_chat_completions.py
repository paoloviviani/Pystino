"""End-to-end behaviour of the OpenAI-compatible surface.

These go through the real ASGI app with a fake upstream transport, so they cover
the wiring as well as the logic: auth, model access, quota refusal, the forcing of
``include_usage``, usage-frame stripping, and what ends up in the ledger.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import orjson
from conftest import FakeUpstream, Seeded
from fastapi import FastAPI
from gateway.models import (
    ApiKey,
    Group,
    LimitMetric,
    LimitRule,
    LimitScope,
    Membership,
    ModelDef,
    UsageRecord,
    UsageSource,
    UsageStatus,
    User,
)
from gateway.security import generate_api_key
from gateway.types import utcnow
from helpers import chunk, completion_body, sse, usage_only_frame
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def latest_usage(
    session_factory: async_sessionmaker[AsyncSession],
) -> UsageRecord:
    async with session_factory() as session:
        row = (
            await session.execute(
                select(UsageRecord).order_by(UsageRecord.created_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
        assert row is not None, "no usage record was written"
        return row


def basic_request(**overrides: object) -> dict:
    payload: dict = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hello there"}],
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------
# authentication
# --------------------------------------------------------------------------


class TestAuthentication:
    async def test_missing_key_is_401_with_openai_error_shape(
        self, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        response = await client.post("/v1/chat/completions", json=basic_request())
        assert response.status_code == 401
        body = response.json()
        assert body["error"]["type"] == "invalid_request_error"
        assert body["error"]["code"] == "invalid_api_key"

    async def test_unknown_key_is_401(self, client: httpx.AsyncClient, seeded: Seeded) -> None:
        response = await client.post(
            "/v1/chat/completions",
            json=basic_request(),
            headers={"authorization": "Bearer gwk_deadbeef_nope"},
        )
        assert response.status_code == 401

    async def test_malformed_key_is_401(self, client: httpx.AsyncClient, seeded: Seeded) -> None:
        response = await client.post(
            "/v1/chat/completions",
            json=basic_request(),
            headers={"authorization": "Bearer garbage"},
        )
        assert response.status_code == 401

    async def test_revoked_key_is_refused(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        fake_upstream: FakeUpstream,
    ) -> None:
        async with session_factory() as session:
            key = (
                await session.execute(select(ApiKey).where(ApiKey.id == seeded.api_key.id))
            ).scalar_one()
            key.revoked_at = utcnow()
            await session.commit()

        fake_upstream.set_json(completion_body())
        response = await client.post(
            "/v1/chat/completions", json=basic_request(), headers=seeded.auth
        )
        assert response.status_code == 401
        assert not fake_upstream.bodies, "a revoked key reached the upstream"

    async def test_key_pinned_to_a_group_the_user_left_is_refused(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Membership is re-checked per request, not trusted from minting time."""
        async with session_factory() as session:
            membership = (
                await session.execute(
                    select(Membership).where(Membership.user_id == seeded.user.id)
                )
            ).scalar_one()
            await session.delete(membership)
            await session.commit()

        response = await client.post(
            "/v1/chat/completions", json=basic_request(), headers=seeded.auth
        )
        assert response.status_code == 403

    async def test_bare_key_without_bearer_scheme_works(
        self, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        fake_upstream.set_json(completion_body())
        response = await client.post(
            "/v1/chat/completions",
            json=basic_request(),
            headers={"authorization": seeded.secret},
        )
        assert response.status_code == 200


# --------------------------------------------------------------------------
# model access
# --------------------------------------------------------------------------


class TestModelAccess:
    async def test_unknown_model_is_404(self, client: httpx.AsyncClient, seeded: Seeded) -> None:
        response = await client.post(
            "/v1/chat/completions",
            json=basic_request(model="no-such-model"),
            headers=seeded.auth,
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "model_not_found"

    async def test_model_not_granted_to_the_group_is_also_404(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Same status as "does not exist": other groups' catalogues are private."""
        async with session_factory() as session:
            session.add(
                ModelDef(
                    name="secret-model",
                    upstream_model="upstream/secret",
                    provider_id=seeded.provider.id,
                )
            )
            await session.commit()

        response = await client.post(
            "/v1/chat/completions",
            json=basic_request(model="secret-model"),
            headers=seeded.auth,
        )
        assert response.status_code == 404

    async def test_models_endpoint_lists_only_accessible_models(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as session:
            session.add(
                ModelDef(
                    name="hidden-model",
                    upstream_model="upstream/hidden",
                    provider_id=seeded.provider.id,
                )
            )
            await session.commit()

        response = await client.get("/v1/models", headers=seeded.auth)
        assert response.status_code == 200
        names = {entry["id"] for entry in response.json()["data"]}
        assert names == {"test-model"}

    async def test_models_endpoint_reports_our_names_not_upstream_ones(
        self, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        response = await client.get("/v1/models", headers=seeded.auth)
        entry = response.json()["data"][0]
        assert entry["id"] == "test-model"
        assert entry["object"] == "model"
        assert entry["context_window"] == 8192

    async def test_inactive_model_disappears(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as session:
            model = (
                await session.execute(select(ModelDef).where(ModelDef.id == seeded.model.id))
            ).scalar_one()
            model.is_active = False
            await session.commit()

        assert (await client.get("/v1/models", headers=seeded.auth)).json()["data"] == []


# --------------------------------------------------------------------------
# non-streaming
# --------------------------------------------------------------------------


class TestNonStreaming:
    async def test_happy_path_records_usage_and_cost(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fake_upstream.set_json(
            completion_body("the answer", prompt=1_000_000, completion=1_000_000)
        )
        response = await client.post(
            "/v1/chat/completions", json=basic_request(), headers=seeded.auth
        )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "the answer"

        row = await latest_usage(session_factory)
        assert row.status is UsageStatus.COMPLETED
        assert row.usage_source is UsageSource.UPSTREAM_EXACT
        assert row.total_tokens == 2_000_000
        # 1M in at 1/M + 1M out at 2/M = 3
        assert row.cost == Decimal(3)
        assert row.streamed is False
        assert row.assistant_text == "the answer"

    async def test_upstream_model_name_is_used_outbound_and_ours_inbound(
        self, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        fake_upstream.set_json(completion_body())
        response = await client.post(
            "/v1/chat/completions", json=basic_request(), headers=seeded.auth
        )
        # The provider sees its own name...
        assert fake_upstream.last_body["model"] == "upstream/test-model"
        # ...and the client sees ours.
        assert response.json()["model"] == "test-model"

    async def test_unknown_parameters_are_forwarded_untouched(
        self, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        """A proxy must not need a release to support a new provider parameter."""
        fake_upstream.set_json(completion_body())
        await client.post(
            "/v1/chat/completions",
            json=basic_request(
                temperature=0.3,
                top_p=0.9,
                response_format={"type": "json_object"},
                some_future_field={"nested": True},
            ),
            headers=seeded.auth,
        )
        body = fake_upstream.last_body
        assert body["temperature"] == 0.3
        assert body["top_p"] == 0.9
        assert body["response_format"] == {"type": "json_object"}
        assert body["some_future_field"] == {"nested": True}

    async def test_parameters_we_were_not_given_are_not_invented(
        self, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        fake_upstream.set_json(completion_body())
        await client.post("/v1/chat/completions", json=basic_request(), headers=seeded.auth)
        body = fake_upstream.last_body
        assert "temperature" not in body
        assert "stream_options" not in body

    async def test_non_streaming_does_not_force_stream_options(
        self, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        fake_upstream.set_json(completion_body())
        await client.post("/v1/chat/completions", json=basic_request(), headers=seeded.auth)
        assert "stream_options" not in fake_upstream.last_body

    async def test_upstream_error_is_passed_through_with_its_status(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fake_upstream.set_raw(
            orjson.dumps({"error": {"message": "provider is on fire", "type": "api_error"}}),
            status=503,
        )
        response = await client.post(
            "/v1/chat/completions", json=basic_request(), headers=seeded.auth
        )
        assert response.status_code == 503
        assert response.json()["error"]["message"] == "provider is on fire"

        row = await latest_usage(session_factory)
        assert row.status is UsageStatus.UPSTREAM_ERROR
        assert row.upstream_status == 503

    async def test_empty_messages_is_rejected_before_the_upstream(
        self, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        response = await client.post(
            "/v1/chat/completions", json=basic_request(messages=[]), headers=seeded.auth
        )
        assert response.status_code == 400
        assert not fake_upstream.bodies

    async def test_upstream_receives_its_own_credentials_not_ours(
        self, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        """The caller's key must never be forwarded to the provider."""
        fake_upstream.set_json(completion_body())
        await client.post("/v1/chat/completions", json=basic_request(), headers=seeded.auth)
        sent = fake_upstream.headers[-1]
        assert sent["authorization"] == "Bearer upstream-key"
        assert seeded.secret not in sent["authorization"]


# --------------------------------------------------------------------------
# streaming
# --------------------------------------------------------------------------


class TestStreaming:
    async def test_include_usage_is_forced_on_the_upstream_request(
        self, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        """Without this, streamed accounting silently records zero."""
        fake_upstream.set_stream([sse(chunk("hi")), sse(usage_only_frame(3, 1)), sse("[DONE]")])
        await client.post(
            "/v1/chat/completions", json=basic_request(stream=True), headers=seeded.auth
        )
        assert fake_upstream.last_body["stream_options"] == {"include_usage": True}

    async def test_client_stream_options_are_merged_not_replaced(
        self, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        fake_upstream.set_stream([sse(chunk("hi")), sse("[DONE]")])
        await client.post(
            "/v1/chat/completions",
            json=basic_request(stream=True, stream_options={"some_other_option": 7}),
            headers=seeded.auth,
        )
        options = fake_upstream.last_body["stream_options"]
        assert options["include_usage"] is True
        assert options["some_other_option"] == 7

    async def test_usage_frame_is_stripped_when_the_client_did_not_ask(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """We always ask upstream; the client only sees it if it asked too."""
        fake_upstream.set_stream(
            [
                sse(chunk("hello")),
                sse(chunk(finish_reason="stop")),
                sse(usage_only_frame(1_000_000, 1_000_000)),
                sse("[DONE]"),
            ]
        )
        response = await client.post(
            "/v1/chat/completions", json=basic_request(stream=True), headers=seeded.auth
        )
        assert response.status_code == 200
        assert "usage" not in response.text
        assert "hello" in response.text
        assert "[DONE]" in response.text

        # Accounting still saw it.
        row = await latest_usage(session_factory)
        assert row.usage_source is UsageSource.UPSTREAM_EXACT
        assert row.total_tokens == 2_000_000
        assert row.cost == Decimal(3)

    async def test_usage_frame_is_forwarded_when_the_client_asked(
        self, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        fake_upstream.set_stream(
            [
                sse(chunk("hello")),
                sse(usage_only_frame(11, 22)),
                sse("[DONE]"),
            ]
        )
        response = await client.post(
            "/v1/chat/completions",
            json=basic_request(stream=True, stream_options={"include_usage": True}),
            headers=seeded.auth,
        )
        assert '"total_tokens":33' in response.text.replace(" ", "")

    async def test_usage_attached_to_a_content_frame_only_loses_the_usage_key(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Dropping such a frame whole would swallow generated text."""
        fake_upstream.set_stream(
            [
                sse(
                    chunk(
                        "important text",
                        usage={"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
                    )
                ),
                sse("[DONE]"),
            ]
        )
        response = await client.post(
            "/v1/chat/completions", json=basic_request(stream=True), headers=seeded.auth
        )
        assert "important text" in response.text
        assert "usage" not in response.text

        row = await latest_usage(session_factory)
        assert row.total_tokens == 10

    async def test_model_name_is_rewritten_in_every_streamed_frame(
        self, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        fake_upstream.set_stream([sse(chunk("a")), sse(chunk("b")), sse("[DONE]")])
        response = await client.post(
            "/v1/chat/completions", json=basic_request(stream=True), headers=seeded.auth
        )
        assert "upstream/test-model" not in response.text
        assert '"model":"test-model"' in response.text.replace(" ", "")

    async def test_streamed_text_is_persisted(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fake_upstream.set_stream(
            [sse(chunk("one ")), sse(chunk("two ")), sse(chunk("three")), sse("[DONE]")]
        )
        await client.post(
            "/v1/chat/completions", json=basic_request(stream=True), headers=seeded.auth
        )
        row = await latest_usage(session_factory)
        assert row.assistant_text == "one two three"
        assert row.streamed is True

    async def test_missing_usage_frame_is_estimated_not_zero(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A provider that ignores include_usage must still be billed for."""
        fake_upstream.set_stream(
            [sse(chunk("some real output")), sse(chunk(finish_reason="stop")), sse("[DONE]")]
        )
        await client.post(
            "/v1/chat/completions", json=basic_request(stream=True), headers=seeded.auth
        )
        row = await latest_usage(session_factory)
        assert row.usage_source is UsageSource.ESTIMATED
        assert row.total_tokens > 0
        assert row.cost > Decimal(0)

    async def test_response_has_event_stream_headers(
        self, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        fake_upstream.set_stream([sse(chunk("x")), sse("[DONE]")])
        response = await client.post(
            "/v1/chat/completions", json=basic_request(stream=True), headers=seeded.auth
        )
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"

    async def test_upstream_error_before_the_stream_starts_keeps_its_status(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Checked before returning a StreamingResponse, so a real status is possible."""
        fake_upstream.set_raw(
            orjson.dumps({"error": {"message": "no capacity", "type": "api_error"}}),
            status=429,
        )
        response = await client.post(
            "/v1/chat/completions", json=basic_request(stream=True), headers=seeded.auth
        )
        assert response.status_code == 429
        assert response.json()["error"]["message"] == "no capacity"
        assert (await latest_usage(session_factory)).status is UsageStatus.UPSTREAM_ERROR

    async def test_keepalive_comments_reach_the_client(
        self, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        fake_upstream.set_stream([b": ping\n\n", sse(chunk("hi")), sse("[DONE]")])
        response = await client.post(
            "/v1/chat/completions", json=basic_request(stream=True), headers=seeded.auth
        )
        assert ": ping" in response.text

    async def test_chunk_boundaries_do_not_affect_output(
        self, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        """The same frames, delivered one byte at a time."""
        raw = b"".join(
            [sse(chunk("hello ")), sse(chunk("world")), sse(usage_only_frame(2, 2)), sse("[DONE]")]
        )
        fake_upstream.set_stream([raw[i : i + 1] for i in range(len(raw))])
        response = await client.post(
            "/v1/chat/completions", json=basic_request(stream=True), headers=seeded.auth
        )
        assert "hello " in response.text
        assert "world" in response.text
        assert "usage" not in response.text


# --------------------------------------------------------------------------
# quota enforcement
# --------------------------------------------------------------------------


class TestQuotaEnforcement:
    async def test_exceeded_quota_returns_429_with_retry_after(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as session:
            session.add(
                LimitRule(
                    name="no requests at all",
                    scope=LimitScope.GROUP,
                    scope_id=seeded.group.id,
                    metric=LimitMetric.REQUESTS,
                    window_seconds=3600,
                    limit_value=Decimal(0),
                )
            )
            await session.commit()

        fake_upstream.set_json(completion_body())
        response = await client.post(
            "/v1/chat/completions", json=basic_request(), headers=seeded.auth
        )
        assert response.status_code == 429
        assert int(response.headers["retry-after"]) >= 1
        body = response.json()
        assert body["error"]["type"] == "insufficient_quota"
        assert body["error"]["code"] == "quota_exceeded"

    async def test_refusal_happens_before_the_upstream_is_called(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Quota checks before, accounting after — no tokens may be spent."""
        async with session_factory() as session:
            session.add(
                LimitRule(
                    name="zero cost ceiling",
                    scope=LimitScope.GROUP,
                    scope_id=seeded.group.id,
                    metric=LimitMetric.COST,
                    window_seconds=3600,
                    limit_value=Decimal(0),
                )
            )
            await session.commit()

        fake_upstream.set_json(completion_body())
        await client.post("/v1/chat/completions", json=basic_request(), headers=seeded.auth)
        assert not fake_upstream.bodies, "a refused request still called the provider"

    async def test_a_refused_request_leaves_no_ledger_row(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as session:
            session.add(
                LimitRule(
                    name="zero requests",
                    scope=LimitScope.GLOBAL,
                    scope_id=None,
                    metric=LimitMetric.REQUESTS,
                    window_seconds=3600,
                    limit_value=Decimal(0),
                )
            )
            await session.commit()

        await client.post("/v1/chat/completions", json=basic_request(), headers=seeded.auth)
        async with session_factory() as session:
            rows = (await session.execute(select(UsageRecord))).scalars().all()
        assert rows == []

    async def test_generous_limit_admits_the_request(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as session:
            session.add(
                LimitRule(
                    name="plenty",
                    scope=LimitScope.GROUP,
                    scope_id=seeded.group.id,
                    metric=LimitMetric.COST,
                    window_seconds=3600,
                    limit_value=Decimal(1000),
                )
            )
            await session.commit()

        fake_upstream.set_json(completion_body(prompt=10, completion=10))
        response = await client.post(
            "/v1/chat/completions", json=basic_request(), headers=seeded.auth
        )
        assert response.status_code == 200

    async def test_second_request_is_refused_after_the_first_exhausts_the_budget(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The documented overrun policy: finish this one, refuse the next."""
        async with session_factory() as session:
            session.add(
                LimitRule(
                    name="one euro per hour",
                    scope=LimitScope.GROUP,
                    scope_id=seeded.group.id,
                    metric=LimitMetric.COST,
                    window_seconds=3600,
                    limit_value=Decimal(1),
                )
            )
            await session.commit()

        # 1M prompt tokens at 1 EUR/M spends the whole budget in one request.
        fake_upstream.set_json(completion_body(prompt=1_000_000, completion=0))
        first = await client.post("/v1/chat/completions", json=basic_request(), headers=seeded.auth)
        assert first.status_code == 200

        second = await client.post(
            "/v1/chat/completions", json=basic_request(), headers=seeded.auth
        )
        assert second.status_code == 429


# --------------------------------------------------------------------------
# client disconnect
# --------------------------------------------------------------------------


class TestClientDisconnect:
    async def test_abandoned_stream_still_records_usage_and_partial_text(
        self,
        app: FastAPI,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        settings: object,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The endpoint is driven directly so the stream can be abandoned mid-flight.

        An ASGI test transport consumes a response to completion, which is exactly
        what must *not* happen here: the interesting path is the one where the
        consumer stops reading and the generator is closed underneath it, as
        happens when a phone is suspended mid-generation.
        """
        from gateway.deps import Principal
        from gateway.routers.chat import chat_completions
        from gateway.schemas import ChatCompletionRequest
        from starlette.requests import Request

        fake_upstream.set_stream(
            [
                sse(chunk("first ")),
                sse(chunk("second ")),
                sse(chunk("third ")),
                sse(chunk(finish_reason="stop")),
                sse(usage_only_frame(10, 10)),
                sse("[DONE]"),
            ]
        )

        async with session_factory() as session:
            user = (
                await session.execute(select(User).where(User.id == seeded.user.id))
            ).scalar_one()
            group = (
                await session.execute(select(Group).where(Group.id == seeded.group.id))
            ).scalar_one()
            key = (
                await session.execute(select(ApiKey).where(ApiKey.id == seeded.api_key.id))
            ).scalar_one()
            principal = Principal(user=user, billing_group=group, api_key=key)

            request = Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/v1/chat/completions",
                    "headers": [],
                    "query_string": b"",
                    "app": app,
                }
            )

            response = await chat_completions(
                request=request,
                body=ChatCompletionRequest(**basic_request(stream=True)),
                principal=principal,
                session=session,
                settings=app.state.settings,
                quota=app.state.quota_engine,
                redactor=app.state.redactor,
                providers=app.state.providers,
                estimator=app.state.token_estimator,
            )

            # Read only the first frame, then walk away.
            iterator = response.body_iterator
            first = await iterator.__anext__()
            assert b"first" in first
            await iterator.aclose()

        # The disconnect handler finalises on a detached task; wait for it.
        pending = set(app.state.background_tasks)
        assert pending, "no finalisation task was scheduled for the abandoned stream"
        import asyncio

        await asyncio.wait(pending, timeout=5.0)

        row = await latest_usage(session_factory)
        assert row.status is UsageStatus.CLIENT_DISCONNECTED
        # Whatever had been generated is on disk, and is billed as an estimate
        # because the usage frame never arrived.
        assert row.assistant_text
        assert row.total_tokens > 0
        assert row.usage_source is UsageSource.ESTIMATED
        assert row.finalised_at is not None


# --------------------------------------------------------------------------
# billing group resolution
# --------------------------------------------------------------------------


class TestBillingGroupResolution:
    async def test_key_without_a_group_falls_back_to_the_user_default(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as session:
            generated = generate_api_key()
            session.add(
                ApiKey(
                    user_id=seeded.user.id,
                    prefix=generated.prefix,
                    key_hash=generated.key_hash,
                    name="unpinned",
                    billing_group_id=None,
                )
            )
            await session.commit()

        fake_upstream.set_json(completion_body())
        response = await client.post(
            "/v1/chat/completions",
            json=basic_request(),
            headers={"authorization": f"Bearer {generated.secret}"},
        )
        assert response.status_code == 200
        assert (await latest_usage(session_factory)).group_id == seeded.group.id

    async def test_no_group_anywhere_is_a_clear_403(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as session:
            user = (
                await session.execute(select(User).where(User.id == seeded.user.id))
            ).scalar_one()
            user.default_billing_group_id = None
            key = (
                await session.execute(select(ApiKey).where(ApiKey.id == seeded.api_key.id))
            ).scalar_one()
            key.billing_group_id = None
            await session.commit()

        response = await client.post(
            "/v1/chat/completions", json=basic_request(), headers=seeded.auth
        )
        assert response.status_code == 403
        assert "billing group" in response.json()["error"]["message"].lower()

    async def test_disabled_group_is_refused(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as session:
            group = (
                await session.execute(select(Group).where(Group.id == seeded.group.id))
            ).scalar_one()
            group.is_active = False
            await session.commit()

        response = await client.post(
            "/v1/chat/completions", json=basic_request(), headers=seeded.auth
        )
        assert response.status_code == 403


# --------------------------------------------------------------------------
# health
# --------------------------------------------------------------------------


class TestHealth:
    async def test_liveness_touches_no_dependency(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    async def test_readiness_checks_the_database(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/readyz")
        assert response.status_code == 200
        assert response.json()["checks"]["database"] == "ok"
