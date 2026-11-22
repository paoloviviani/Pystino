"""``GET /metrics``: off by default, token-gated, and free of anything about a caller.

Three promises, tested as promises (docs/metrics.md):

* the endpoint is not there unless an operator turned it on, and then it is
  never open;
* the numbers follow the requests, including the in-flight gauge *while* a
  stream is still open;
* nothing that identifies a person, a key or a prompt reaches the exposition
  text, however the request went.

The registry is process-wide, so counters are compared as before/after deltas.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import FakeUpstream, Seeded
from fastapi import FastAPI
from gateway import metrics
from gateway.config import MetricsSettings, Settings
from gateway.metrics_env import prepare_multiprocess
from gateway.models import LimitMetric, LimitRule, LimitScope
from helpers import chunk, completion_body, sse, usage_only_frame
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TOKEN = "scrape-token-0123456789abcdef"
SCRAPE = {"authorization": f"Bearer {TOKEN}"}
PROMPT = "PROMPT-CANARY-do-not-leak-4f2a"


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"metrics": MetricsSettings(enabled=True, token=TOKEN)})


def sample(name: str, **labels: str) -> float:
    """One sample from the registry, 0 when it does not exist yet."""
    return metrics.REGISTRY.get_sample_value(name, labels) or 0.0


class Moved:
    """The change in some samples since this object was made."""

    def __init__(self, *series: tuple[str, dict[str, str]]) -> None:
        self.series = series
        self.before = [sample(n, **labels) for n, labels in series]

    def deltas(self) -> list[float]:
        return [
            sample(n, **labels) - b for (n, labels), b in zip(self.series, self.before, strict=True)
        ]


async def chat(client: httpx.AsyncClient, seeded: Seeded, **extra: Any) -> httpx.Response:
    return await client.post(
        "/v1/chat/completions",
        json={
            "model": seeded.model.name,
            "messages": [{"role": "user", "content": PROMPT}],
            **extra,
        },
        headers=seeded.auth,
    )


async def scrape(client: httpx.AsyncClient) -> str:
    response = await client.get("/metrics", headers=SCRAPE)
    assert response.status_code == 200
    return response.text


# -- access ------------------------------------------------------------------


class TestAccess:
    async def test_off_by_default_the_route_does_not_exist(
        self, settings: Settings, fake_upstream: FakeUpstream
    ) -> None:
        from gateway.main import create_app

        plain = settings.model_copy(update={"metrics": MetricsSettings()})
        transport = httpx.ASGITransport(app=create_app(plain))
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as http:
            assert (await http.get("/metrics", headers=SCRAPE)).status_code == 404
            assert (await http.get("/metrics")).status_code == 404

    def test_enabling_it_without_a_token_refuses_to_start(self) -> None:
        with pytest.raises(ValueError, match="GATEWAY_METRICS__TOKEN"):
            MetricsSettings(enabled=True)
        with pytest.raises(ValueError, match="at least 16"):
            MetricsSettings(enabled=True, token="short")
        assert MetricsSettings().enabled is False

    async def test_no_token_and_a_wrong_token_are_refused(self, client: httpx.AsyncClient) -> None:
        missing = await client.get("/metrics")
        wrong = await client.get("/metrics", headers={"authorization": "Bearer nope"})
        other_scheme = await client.get("/metrics", headers={"authorization": f"Basic {TOKEN}"})
        bare = await client.get("/metrics", headers={"authorization": TOKEN})
        assert [r.status_code for r in (missing, wrong, other_scheme, bare)] == [401] * 4
        assert "pystino_" not in missing.text + wrong.text

    async def test_a_session_cookie_or_an_api_key_is_not_the_token(
        self, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        assert (await client.get("/metrics", headers=seeded.auth)).status_code == 401

    async def test_the_right_token_gets_prometheus_text(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/metrics", headers=SCRAPE)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert "# TYPE pystino_requests_in_flight gauge" in response.text
        assert 'pystino_build_info{version="' in response.text

    async def test_it_is_not_in_the_openapi_schema(self, app: FastAPI) -> None:
        assert "/metrics" not in app.openapi()["paths"]

    def test_the_proxy_answers_404_for_it(self) -> None:
        caddyfile = (Path(__file__).parents[3] / "deploy/caddy/Caddyfile").read_text()
        block = caddyfile.index("@metrics path /metrics /metrics/*")
        assert "respond 404" in caddyfile[block : block + 120]
        # Before the catch-all that proxies to the gateway.
        assert block < caddyfile.index("import /etc/caddy/modes/gateway.caddy")


# -- the numbers follow the requests -----------------------------------------


class TestCounters:
    async def test_a_metered_request_moves_the_counters(
        self, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        fake_upstream.set_json(completion_body("fine", prompt=10, completion=5))
        provider = {"provider": "fake"}
        moved = Moved(
            (
                "pystino_requests_total",
                {
                    "surface": "chat_completions",
                    "provider": "fake",
                    "model": "test-model",
                    "status": "completed",
                },
            ),
            ("pystino_tokens_total", {**provider, "model": "test-model", "direction": "prompt"}),
            (
                "pystino_tokens_total",
                {**provider, "model": "test-model", "direction": "completion"},
            ),
            ("pystino_cost_total", {**provider, "currency": "EUR"}),
            (
                "pystino_request_duration_seconds_count",
                {"surface": "chat_completions", **provider},
            ),
        )

        assert (await chat(client, seeded)).status_code == 200
        text = await scrape(client)

        requests, prompt, completion, cost, observed = moved.deltas()
        assert (requests, prompt, completion, observed) == (1, 10, 5, 1)
        # 10 tokens at 1 EUR/Mtok and 5 at 2 EUR/Mtok.
        assert cost == pytest.approx(2e-5)
        assert "pystino_requests_total" in text

    async def test_a_cache_hit_is_its_own_direction_and_the_slices_do_not_overlap(
        self, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        body = completion_body("fine", prompt=100, completion=1)
        body["usage"]["prompt_tokens_details"] = {"cached_tokens": 60}
        fake_upstream.set_json(body)
        labels = {"provider": "fake", "model": "test-model"}
        moved = Moved(
            ("pystino_tokens_total", {**labels, "direction": "prompt"}),
            ("pystino_tokens_total", {**labels, "direction": "cached_read"}),
        )
        assert (await chat(client, seeded)).status_code == 200
        # prompt is what was not served from the cache: 100 - 60.
        assert moved.deltas() == [40, 60]

    async def test_an_upstream_failure_counts_by_kind_and_status(
        self, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        fake_upstream.set_json({"error": {"message": "boom"}}, status=503)
        moved = Moved(
            ("pystino_upstream_errors_total", {"provider": "fake", "kind": "5xx"}),
            (
                "pystino_requests_total",
                {
                    "surface": "chat_completions",
                    "provider": "fake",
                    "model": "test-model",
                    "status": "upstream_error",
                },
            ),
        )
        assert (await chat(client, seeded)).status_code == 503
        assert moved.deltas() == [1, 1]

        fake_upstream.set_json({"error": {"message": "bad"}}, status=400)
        again = Moved(("pystino_upstream_errors_total", {"provider": "fake", "kind": "4xx"}))
        assert (await chat(client, seeded)).status_code == 400
        assert again.deltas() == [1]

    async def test_a_bad_key_is_an_auth_failure_with_no_model_or_provider(
        self, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        moved = Moved(
            (
                "pystino_requests_total",
                {
                    "surface": "chat_completions",
                    "provider": "none",
                    "model": "none",
                    "status": "auth_failed",
                },
            )
        )
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": []},
            headers={"authorization": "Bearer gw_definitely_not_a_key"},
        )
        assert response.status_code == 401
        assert moved.deltas() == [1]

    async def test_a_quota_refusal_counts_the_metric_and_scope_kind_and_no_id(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        fake_upstream: FakeUpstream,
    ) -> None:
        async with session_factory() as db:
            db.add(
                LimitRule(
                    name="no requests",
                    scope=LimitScope.GROUP,
                    scope_id=seeded.group.id,
                    metric=LimitMetric.REQUESTS,
                    window_seconds=3600,
                    limit_value=Decimal(0),
                )
            )
            await db.commit()
        fake_upstream.set_json(completion_body())
        moved = Moved(
            (
                "pystino_quota_refusals_total",
                {"metric": "requests", "scope": "group", "filter": "none"},
            ),
            (
                "pystino_requests_total",
                {
                    "surface": "chat_completions",
                    "provider": "fake",
                    "model": "test-model",
                    "status": "quota_exceeded",
                },
            ),
        )
        assert (await chat(client, seeded)).status_code == 429
        assert moved.deltas() == [1, 1]
        assert str(seeded.group.id) not in await scrape(client)

    async def test_a_privacy_refusal_is_counted_as_one(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        fake_upstream: FakeUpstream,
    ) -> None:
        from privacy_helpers import add_pipeline, rule

        await add_pipeline(app, session_factory, "chat", "Refuse chat", rules=[rule("Nope")])
        moved = Moved(
            ("pystino_privacy_actions_total", {"kind": "content", "action": "blocked"}),
            (
                "pystino_requests_total",
                {
                    "surface": "chat_completions",
                    "provider": "fake",
                    "model": "test-model",
                    "status": "privacy_refused",
                },
            ),
        )
        assert (await chat(client, seeded)).status_code == 403
        assert moved.deltas() == [1, 1]
        assert fake_upstream.bodies == []

    async def test_redaction_is_timed_and_a_redacted_request_counted(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
    ) -> None:
        from gateway.redaction.base import RedactionOutcome

        real = app.state.redactor

        class Hides:
            name = "stub"

            async def redact_request(self, messages: Any, *, policy: Any = None) -> Any:
                return RedactionOutcome(messages=messages, entity_count=2, engine="stub")

            def response_stage(self, outcome: Any, **kwargs: Any) -> Any:
                return real.response_stage(outcome, **kwargs)

            async def restore_response(self, text: str, outcome: Any) -> Any:
                return await real.restore_response(text, outcome)

        app.state.redactor = Hides()
        fake_upstream.set_json(completion_body())
        moved = Moved(
            ("pystino_privacy_actions_total", {"kind": "content", "action": "redacted"}),
            ("pystino_redaction_duration_seconds_count", {}),
        )
        assert (await chat(client, seeded)).status_code == 200
        assert moved.deltas() == [1, 1]

    async def test_a_content_block_counts_as_blocked(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
    ) -> None:
        from gateway.errors import ContentBlockedError

        class Blocks:
            name = "stub"

            async def redact_request(self, messages: Any, *, policy: Any = None) -> Any:
                raise ContentBlockedError("a credential is in the prompt")

        app.state.redactor = Blocks()
        moved = Moved(
            ("pystino_privacy_actions_total", {"kind": "content", "action": "blocked"}),
            (
                "pystino_requests_total",
                {
                    "surface": "chat_completions",
                    "provider": "fake",
                    "model": "test-model",
                    "status": "blocked",
                },
            ),
        )
        assert (await chat(client, seeded)).status_code == 403
        assert moved.deltas() == [1, 1]
        assert fake_upstream.bodies == []

    async def test_a_routed_alias_counts_the_failover_and_the_recovery(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        fake_upstream: FakeUpstream,
    ) -> None:
        from test_routing_policies import Upstreams, make_route

        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1), name="routed-m")
        upstreams = Upstreams(fake_upstream)
        upstreams.set("up-t-a", "timeout")
        moved = Moved(
            ("pystino_route_failovers_total", {"alias": "routed-m", "outcome": "failover"}),
            ("pystino_route_failovers_total", {"alias": "routed-m", "outcome": "recovered"}),
            ("pystino_route_failovers_total", {"alias": "routed-m", "outcome": "exhausted"}),
            ("pystino_upstream_errors_total", {"provider": "fake", "kind": "timeout"}),
        )
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "routed-m", "messages": [{"role": "user", "content": "hi"}]},
            headers=seeded.auth,
        )
        assert response.status_code == 200
        assert moved.deltas() == [1, 1, 0, 1]

    async def test_a_routed_alias_that_runs_out_of_targets_is_exhausted(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        fake_upstream: FakeUpstream,
    ) -> None:
        from test_routing_policies import Upstreams, make_route

        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1), name="routed-x")
        upstreams = Upstreams(fake_upstream)
        upstreams.set("up-t-a", "connect")
        upstreams.set("up-t-b", 503)
        moved = Moved(
            ("pystino_route_failovers_total", {"alias": "routed-x", "outcome": "failover"}),
            ("pystino_route_failovers_total", {"alias": "routed-x", "outcome": "exhausted"}),
            ("pystino_upstream_errors_total", {"provider": "fake", "kind": "network"}),
        )
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "routed-x", "messages": [{"role": "user", "content": "hi"}]},
            headers=seeded.auth,
        )
        assert response.status_code == 503
        assert moved.deltas() == [1, 1, 1]


class TestLedgerGaps:
    """Refusals the ledger does not write a row for still show up in the counters."""

    async def test_an_undecidable_privacy_rule_counts_one_503(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        monkeypatch: pytest.MonkeyPatch,
        fake_upstream: FakeUpstream,
    ) -> None:
        from gateway.errors import ServiceUnavailableError
        from gateway.routers import _metered

        async def undecidable(*args: Any, **kwargs: Any) -> Any:
            raise ServiceUnavailableError("cannot decide", code="privacy_undecidable")

        monkeypatch.setattr(_metered, "plan", undecidable)
        moved = Moved(
            ("pystino_privacy_actions_total", {"kind": "content", "action": "undecidable"}),
            (
                "pystino_requests_total",
                {
                    "surface": "chat_completions",
                    "provider": "fake",
                    "model": "test-model",
                    "status": "privacy_undecidable",
                },
            ),
        )
        assert (await chat(client, seeded)).status_code == 503
        assert moved.deltas() == [1, 1]
        assert fake_upstream.bodies == []

    async def test_an_unavailable_quota_store_counts_one_503(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        monkeypatch: pytest.MonkeyPatch,
        fake_upstream: FakeUpstream,
    ) -> None:
        from gateway.quota import QuotaEngine, QuotaUnavailable

        async def down(*args: Any, **kwargs: Any) -> Any:
            raise QuotaUnavailable("no counter store could answer")

        monkeypatch.setattr(QuotaEngine, "check_and_reserve", down)
        moved = Moved(
            (
                "pystino_requests_total",
                {
                    "surface": "chat_completions",
                    "provider": "fake",
                    "model": "test-model",
                    "status": "quota_unavailable",
                },
            ),
        )
        assert (await chat(client, seeded)).status_code == 503
        assert moved.deltas() == [1]
        assert fake_upstream.bodies == []

    async def test_an_ocr_redaction_service_that_is_down_is_an_undecidable_action(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        from gateway.redaction.http import RedactionUnavailableError
        from test_ocr_surface import DOCUMENT, add_ocr_model, ocr_response

        class Down:
            name = "stub"

            async def redact_request(self, messages: Any, *, policy: Any = None) -> Any:
                raise RedactionUnavailableError("down")

        app.state.redactor = Down()
        model = await add_ocr_model(session, seeded, name="metrics-ocr")
        fake_upstream.set_json(ocr_response(1))
        moved = Moved(
            ("pystino_privacy_actions_total", {"kind": "extract", "action": "undecidable"}),
        )
        response = await client.post(
            "/v1/ocr", json={"model": model.name, "document": DOCUMENT}, headers=seeded.auth
        )
        assert response.status_code >= 500
        assert moved.deltas() == [1]


class TestQuotaFilter:
    async def _rule(
        self, session_factory: async_sessionmaker[AsyncSession], seeded: Seeded, **where: Any
    ) -> None:
        async with session_factory() as db:
            db.add(
                LimitRule(
                    name="cap",
                    scope=LimitScope.GROUP,
                    scope_id=seeded.group.id,
                    metric=LimitMetric.REQUESTS,
                    window_seconds=3600,
                    limit_value=Decimal(0),
                    **where,
                )
            )
            await db.commit()

    @pytest.mark.parametrize("filter_", ["provider", "model"])
    async def test_a_provider_or_model_rule_says_which_kind_and_not_which(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        fake_upstream: FakeUpstream,
        filter_: str,
    ) -> None:
        where = (
            {"provider_id": seeded.provider.id}
            if filter_ == "provider"
            else {"model_id": seeded.model.id}
        )
        await self._rule(session_factory, seeded, **where)
        moved = Moved(
            (
                "pystino_quota_refusals_total",
                {"metric": "requests", "scope": "group", "filter": filter_},
            ),
            (
                "pystino_quota_refusals_total",
                {"metric": "requests", "scope": "group", "filter": "none"},
            ),
        )
        assert (await chat(client, seeded)).status_code == 429
        assert moved.deltas() == [1, 0]
        assert str(seeded.provider.id) not in await scrape(client)

    async def test_a_cap_on_the_first_attempt_is_not_a_failover(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        fake_upstream: FakeUpstream,
    ) -> None:
        from test_routing_policies import make_route

        await make_route(session_factory, seeded, ("q-a", 1), ("q-b", 1), name="routed-cap")
        await self._rule(session_factory, seeded, provider_id=seeded.provider.id)
        moved = Moved(
            ("pystino_route_failovers_total", {"alias": "routed-cap", "outcome": "failover"}),
            ("pystino_route_failovers_total", {"alias": "routed-cap", "outcome": "exhausted"}),
        )
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "routed-cap", "messages": [{"role": "user", "content": "hi"}]},
            headers=seeded.auth,
        )
        assert response.status_code == 429
        assert moved.deltas() == [0, 0]


class TestFailoverAccounting:
    async def test_a_last_attempt_that_raises_still_counts_as_exhausted(self) -> None:
        from types import SimpleNamespace
        from typing import cast

        from gateway.errors import ServiceUnavailableError
        from gateway.routers import _metered
        from gateway.routers._metered import AttemptFailed

        def target(attempt: int) -> Any:
            return SimpleNamespace(
                attempt=attempt,
                last=attempt == 2,
                requested_name="routed-raises",
                model=SimpleNamespace(is_private=False, name=f"t{attempt}"),
            )

        async def serve(resolved: Any) -> Any:
            if resolved.attempt == 1:
                raise AttemptFailed("upstream_error", response=None)
            raise ServiceUnavailableError("unreachable")

        plan = SimpleNamespace(attempts=[target(1), target(2)], sticky=None)
        moved = Moved(
            ("pystino_route_failovers_total", {"alias": "routed-raises", "outcome": "failover"}),
            ("pystino_route_failovers_total", {"alias": "routed-raises", "outcome": "exhausted"}),
        )
        with pytest.raises(ServiceUnavailableError):
            await _metered.serve_with_failover(cast(Any, None), cast(Any, plan), serve)
        assert moved.deltas() == [1, 1]


# -- streams -----------------------------------------------------------------


class TestStreams:
    async def test_in_flight_is_one_while_a_stream_is_open_and_zero_after(
        self, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        opened = asyncio.Event()
        release = asyncio.Event()

        def responder(_: httpx.Request) -> httpx.Response:
            async def body() -> AsyncIterator[bytes]:
                yield sse(chunk("he"))
                opened.set()
                await release.wait()
                yield sse(chunk("llo", finish_reason="stop"))
                yield sse(usage_only_frame(4, 2))
                yield sse("[DONE]")

            return httpx.Response(
                200, content=body(), headers={"content-type": "text/event-stream"}
            )

        fake_upstream._responder = responder
        stream_name = "pystino_requests_in_flight"
        ttft = Moved(("pystino_time_to_first_token_seconds_count", {"provider": "fake"}))
        baseline = sample(stream_name, surface="chat_completions")

        request = asyncio.create_task(chat(client, seeded, stream=True))
        await asyncio.wait_for(opened.wait(), timeout=5)
        # The scrape itself is not counted, so the one in flight is the stream.
        text = await scrape(client)
        assert f'{stream_name}{{surface="chat_completions"}} {baseline + 1}' in text
        release.set()
        assert (await asyncio.wait_for(request, timeout=5)).status_code == 200
        assert sample(stream_name, surface="chat_completions") == baseline
        assert ttft.deltas() == [1]

    async def test_a_client_that_hangs_up_is_counted_and_the_gauge_returns(
        self, app: FastAPI, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        """Raw ASGI, because the httpx transport cannot hang up mid-body."""
        release = asyncio.Event()

        def responder(_: httpx.Request) -> httpx.Response:
            async def body() -> AsyncIterator[bytes]:
                yield sse(chunk("he"))
                await release.wait()
                yield sse(chunk("llo"))

            return httpx.Response(
                200, content=body(), headers={"content-type": "text/event-stream"}
            )

        fake_upstream._responder = responder
        import orjson

        payload = orjson.dumps(
            {
                "model": seeded.model.name,
                "stream": True,
                "messages": [{"role": "user", "content": "x"}],
            }
        )
        disconnect = asyncio.Event()
        sent = asyncio.Event()
        first_chunk = asyncio.Event()

        async def receive() -> dict[str, Any]:
            if not sent.is_set():
                sent.set()
                return {"type": "http.request", "body": payload, "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.body" and message.get("body"):
                first_chunk.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "headers": [
                (b"content-type", b"application/json"),
                (b"authorization", f"Bearer {seeded.secret}".encode()),
                (b"content-length", str(len(payload)).encode()),
            ],
            "server": ("gateway", 80),
            "client": ("127.0.0.1", 1),
            "scheme": "http",
            "root_path": "",
            "app": app,
        }
        moved = Moved(
            (
                "pystino_requests_total",
                {
                    "surface": "chat_completions",
                    "provider": "fake",
                    "model": "test-model",
                    "status": "client_disconnected",
                },
            )
        )
        baseline = sample("pystino_requests_in_flight", surface="chat_completions")
        task = asyncio.create_task(app(scope, receive, send))  # type: ignore[arg-type]
        await asyncio.wait_for(first_chunk.wait(), timeout=5)
        assert sample("pystino_requests_in_flight", surface="chat_completions") == baseline + 1
        disconnect.set()
        release.set()
        await asyncio.wait_for(task, timeout=5)
        for _ in range(50):
            if moved.deltas() == [1]:
                break
            await asyncio.sleep(0.05)
        assert moved.deltas() == [1]
        assert sample("pystino_requests_in_flight", surface="chat_completions") == baseline


# -- nothing about a caller ----------------------------------------------------


class TestNoIdentity:
    async def test_no_email_id_key_name_group_or_prompt_in_the_text(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fake_upstream.set_json(completion_body("a reply that must not appear either"))
        assert (await chat(client, seeded)).status_code == 200
        await client.get("/v1/models", headers=seeded.auth)
        # Make the tracker see this caller, through the sampler's own path.
        text = await scrape(client)

        assert 'pystino_active_users{window="5m"} 1.0' in text
        assert 'pystino_active_api_keys{window="5m"} 1.0' in text
        forbidden = [
            seeded.user.email,
            "member@example.org",
            "example.org",
            str(seeded.user.id),
            str(seeded.user.id).replace("-", ""),
            seeded.user.subject,
            seeded.user.display_name,
            "Test Member",
            seeded.secret,
            seeded.secret[:12],
            seeded.api_key.prefix,
            str(seeded.api_key.id),
            "test key",
            seeded.group.name,
            str(seeded.group.id),
            PROMPT,
            "a reply that must not appear",
            "upstream-key",
        ]
        leaked = [needle for needle in forbidden if needle and needle in text]
        assert leaked == []

    async def test_every_label_value_is_from_a_small_closed_set(
        self, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        fake_upstream.set_json(completion_body())
        await chat(client, seeded)
        text = await scrape(client)
        allowed = {
            "surface",
            "status",
            "provider",
            "model",
            "kind",
            "outcome",
            "alias",
            "direction",
            "currency",
            "metric",
            "scope",
            "filter",
            "action",
            "backend",
            "window",
            "state",
            "version",
            "le",
            "quantile",
        }
        labels: set[str] = set()
        for family in text_string_to_metric_families(text):
            if not family.name.startswith("pystino_"):
                continue
            for s in family.samples:
                labels |= set(s.labels)
        assert labels <= allowed, labels - allowed


# -- the active-caller estimate --------------------------------------------------


class FakeValkey:
    """Just enough of Valkey for the HyperLogLog calls the tracker makes, with
    exact sets standing in for the sketches."""

    def __init__(self) -> None:
        self.sets: dict[str, set[str]] = {}

    def pipeline(self, transaction: bool = False) -> FakeValkey.Pipe:
        return FakeValkey.Pipe(self)

    class Pipe:
        def __init__(self, owner: FakeValkey) -> None:
            self.owner = owner

        async def __aenter__(self) -> FakeValkey.Pipe:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        def pfadd(self, key: str, *values: str) -> None:
            self.owner.sets.setdefault(key, set()).update(values)

        def expire(self, key: str, seconds: int) -> None:
            return None

        async def execute(self) -> list[Any]:
            return []

    async def pfcount(self, *keys: str) -> int:
        union: set[str] = set()
        for key in keys:
            union |= self.sets.get(key, set())
        return len(union)


class TestActiveTracker:
    def test_windows_count_distinct_callers_and_forget(self) -> None:
        now = [10_000.0]
        tracker = metrics.ActiveTracker(clock=lambda: now[0])
        tracker.observe("alice", "key-a")
        tracker.observe("alice", "key-a")
        tracker.observe("bob")
        counts = asyncio.run(tracker.counts())
        assert counts["user"] == {"5m": 2, "15m": 2}
        assert counts["key"] == {"5m": 1, "15m": 1}

        now[0] += 6 * 60
        tracker.observe("carol")
        counts = asyncio.run(tracker.counts())
        assert counts["user"] == {"5m": 1, "15m": 3}

        now[0] += 20 * 60
        assert asyncio.run(tracker.counts())["user"] == {"5m": 0, "15m": 0}

    def test_the_tracker_keeps_no_identity(self) -> None:
        tracker = metrics.ActiveTracker(clock=lambda: 600.0)
        tracker.observe("alice@example.org", "gw_secretkey")
        held = repr(tracker.__dict__)
        assert "alice" not in held
        assert "secretkey" not in held

    async def test_two_workers_sharing_valkey_report_one_deployment_wide_number(self) -> None:
        shared = FakeValkey()
        a = metrics.ActiveTracker(shared, clock=lambda: 600.0)
        b = metrics.ActiveTracker(shared, clock=lambda: 600.0)
        a.observe("alice")
        a.observe("bob")
        b.observe("bob")
        b.observe("carol")
        await a.flush()
        await b.flush()
        assert (await a.counts())["user"]["5m"] == 3
        assert (await b.counts())["user"]["5m"] == 3
        assert "alice" not in repr(shared.sets)

    async def test_without_valkey_each_worker_counts_its_own(self) -> None:
        a = metrics.ActiveTracker(clock=lambda: 600.0)
        b = metrics.ActiveTracker(clock=lambda: 600.0)
        a.observe("alice")
        a.observe("bob")
        b.observe("bob")
        assert (await a.counts())["user"]["5m"] == 2
        assert (await b.counts())["user"]["5m"] == 1

    async def test_a_valkey_outage_keeps_the_unsent_callers_for_the_next_try(self) -> None:
        class Down(FakeValkey):
            def pipeline(self, transaction: bool = False) -> Any:
                raise ConnectionError("down")

        down = Down()
        tracker = metrics.ActiveTracker(down, clock=lambda: 600.0)
        tracker.observe("alice")
        await tracker.flush()
        assert sum(len(v) for v in tracker._unsent["user"].values()) == 1


class TestLabels:
    def test_a_members_own_provider_and_model_fold_into_byok(self) -> None:
        assert metrics.provider_label("alice's gpu box", byok=True) == "byok"
        assert metrics.model_label("my/secret-name", byok=True) == "byok"
        assert metrics.provider_label(None) == "unknown"
        assert metrics.model_label("") == "unknown"

    def test_an_unbounded_source_cannot_make_unbounded_series(self) -> None:
        seen = {metrics.provider_label(f"p{i}") for i in range(500)}
        assert "other" in seen
        assert len(seen) <= metrics._MAX_NAMES + 1

    @pytest.mark.parametrize(
        ("path", "surface"),
        [
            ("/v1/chat/completions", "chat_completions"),
            ("/v1/messages", "messages"),
            ("/v1/embeddings", "embeddings"),
            ("/v1/audio/transcriptions", "transcription"),
            ("/v1/files", "v1_other"),
            ("/api/admin/users", "api"),
            ("/console/assets/x.js", "console"),
            ("/whatever/" + "x" * 50, "other"),
        ],
    )
    def test_surfaces_are_a_closed_set(self, path: str, surface: str) -> None:
        assert metrics.surface_for_path(path) == surface


# -- several workers ---------------------------------------------------------------


def _python(code: str, env: dict[str, str], **kwargs: Any) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603 - our own interpreter, our own snippets
        [sys.executable, "-I", "-c", textwrap.dedent(code)],
        env={**os.environ, **env},
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **kwargs,
    )


WORKER = """
    import sys
    from gateway import metrics
    metrics.request_started("chat_completions")
    metrics.finished_request(surface="chat_completions", provider="fake", model="m",
                             status="completed", duration_seconds=0.2, prompt=7, completion=3)
    print("ready", flush=True)
    sys.stdin.readline()
"""

SCRAPER = """
    import asyncio, types
    from gateway import metrics
    app = types.SimpleNamespace(state=types.SimpleNamespace())
    print(asyncio.run(metrics.render(app)).decode())
"""


class TestSeveralWorkers:
    def test_a_scrape_merges_every_live_worker(self, tmp_path: Path) -> None:
        env = {"PROMETHEUS_MULTIPROC_DIR": str(tmp_path)}
        workers = [_python(WORKER, env), _python(WORKER, env)]
        try:
            for worker in workers:
                assert worker.stdout is not None
                assert worker.stdout.readline().strip() == "ready"
            scraper = _python(SCRAPER, env)
            out, err = scraper.communicate(timeout=60)
            assert scraper.returncode == 0, err
        finally:
            for worker in workers:
                worker.communicate("\n", timeout=30)

        values = {
            (s.name, tuple(sorted(s.labels.items()))): s.value
            for family in text_string_to_metric_families(out)
            for s in family.samples
        }
        in_flight = values[("pystino_requests_in_flight", (("surface", "chat_completions"),))]
        requests = values[
            (
                "pystino_requests_total",
                (
                    ("model", "m"),
                    ("provider", "fake"),
                    ("status", "completed"),
                    ("surface", "chat_completions"),
                ),
            )
        ]
        tokens = values[
            (
                "pystino_tokens_total",
                (("direction", "prompt"), ("model", "m"), ("provider", "fake")),
            )
        ]
        # Two workers, one each: summed, not the answering worker's alone.
        assert (in_flight, requests, tokens) == (2.0, 2.0, 14.0)

    def test_a_worker_that_exits_leaves_its_counters_but_not_its_gauges(
        self, tmp_path: Path
    ) -> None:
        env = {"PROMETHEUS_MULTIPROC_DIR": str(tmp_path)}
        worker = _python(WORKER + "\n    metrics.shutdown(None)\n", env)
        assert worker.stdout is not None
        assert worker.stdout.readline().strip() == "ready"
        worker.communicate("\n", timeout=30)
        out, err = _python(SCRAPER, env).communicate(timeout=60)
        assert 'pystino_requests_total{model="m"' in out, err
        assert 'pystino_requests_in_flight{surface="chat_completions"}' not in out

    def test_starting_the_server_empties_the_directory_and_exports_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "counter_123.db").write_bytes(b"stale")
        (tmp_path / "nested").mkdir()
        (tmp_path / "nested" / "x").write_bytes(b"x")
        # prepare_multiprocess writes os.environ directly; registering the name with
        # monkeypatch first makes teardown remove it again instead of leaking a
        # stale directory into every later test and subprocess.
        monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", "")
        monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR")
        assert prepare_multiprocess(str(tmp_path)) == str(tmp_path)
        assert list(tmp_path.iterdir()) == []
        assert os.environ["PROMETHEUS_MULTIPROC_DIR"] == str(tmp_path)


class TestHealthFlags:
    def test_the_flags_are_absent_until_something_is_probed(self, tmp_path: Path) -> None:
        env = {"PROMETHEUS_MULTIPROC_DIR": str(tmp_path)}
        out, err = _python(SCRAPER, env).communicate(timeout=60)
        assert "pystino_requests_in_flight" in out or "pystino_build_info" in out or err == ""
        assert "pystino_valkey_up" not in out
        assert "pystino_redaction_service_up" not in out

    def test_the_flags_appear_once_a_dependency_is_probed(self, tmp_path: Path) -> None:
        env = {"PROMETHEUS_MULTIPROC_DIR": str(tmp_path)}
        code = """
            import asyncio, types
            from gateway import metrics

            class Valkey:
                async def ping(self):
                    return True

            class Redactor:
                async def service_reachable(self):
                    return False

            state = types.SimpleNamespace(valkey=Valkey(), redactor=Redactor())
            app = types.SimpleNamespace(state=state)
            print(asyncio.run(metrics.render(app)).decode())
        """
        out, err = _python(code, env).communicate(timeout=60)
        assert "\npystino_valkey_up 1.0" in out, err
        assert "\npystino_redaction_service_up 0.0" in out

    async def test_the_default_deployment_exports_neither(
        self, client: httpx.AsyncClient, app: FastAPI
    ) -> None:
        # The test app has no Valkey and no HTTP redaction service.
        text = await scrape(client)
        if "pystino_valkey_up" not in metrics._up_gauges:
            assert "pystino_valkey_up" not in text
        if "pystino_redaction_service_up" not in metrics._up_gauges:
            assert "pystino_redaction_service_up" not in text


class TestTimeToFirstToken:
    async def test_a_non_streamed_request_does_not_touch_the_ttft_histogram(
        self, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        fake_upstream.set_json(completion_body())
        moved = Moved(("pystino_time_to_first_token_seconds_count", {"provider": "fake"}))
        assert (await chat(client, seeded)).status_code == 200
        assert moved.deltas() == [0]


class TestDeadWorkers:
    def test_a_killed_workers_live_gauge_files_are_removed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        monkeypatch.setattr(metrics, "MULTIPROCESS", True)
        monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
        gone = [
            tmp_path / f"gauge_livesum_{dead.pid}.db",
            tmp_path / f"gauge_livemin_{dead.pid}.db",
        ]
        alive = [
            tmp_path / f"gauge_livesum_{os.getpid()}.db",
            tmp_path / f"counter_{dead.pid}.db",
        ]
        for path in (*gone, *alive):
            path.write_bytes(b"")
        assert metrics.reap_dead_workers() == [dead.pid]
        assert not any(p.exists() for p in gone)
        assert all(p.exists() for p in alive)

    def test_a_pid_we_may_not_signal_is_alive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(metrics, "MULTIPROCESS", True)
        monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
        (tmp_path / "gauge_livesum_999999.db").write_bytes(b"")

        def kill(pid: int, sig: int) -> None:
            raise PermissionError

        monkeypatch.setattr(os, "kill", kill)
        assert metrics.reap_dead_workers() == []
        assert (tmp_path / "gauge_livesum_999999.db").exists()


# -- the hot path ------------------------------------------------------------------


class TestQueryBudget:
    async def test_collection_adds_no_query_to_a_metered_request(
        self, client: httpx.AsyncClient, seeded: Seeded, app: FastAPI, fake_upstream: FakeUpstream
    ) -> None:
        """test_query_counts.py's budget, with metrics on."""
        from test_query_counts import counted

        fake_upstream.set_json(completion_body())
        await chat(client, seeded)
        with counted(app.state.engine) as statements:
            assert (await chat(client, seeded)).status_code == 200
        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        writes = [
            s for s in statements if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]
        assert len(selects) <= 5
        assert len(writes) <= 2
