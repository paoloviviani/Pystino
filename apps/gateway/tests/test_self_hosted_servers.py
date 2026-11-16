"""Self-hosted servers (vLLM, SGLang, llama.cpp): presets, import, probe, default
effort and accounting. The probe's parsing is in ``test_server_probe.py``."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from conftest import FakeUpstream, Seeded
from gateway.catalogue import load_catalogue
from gateway.models import ModelDef, ModelPrice, PriceSource, Provider, UsageRecord
from helpers import chunk, completion_body, sse, usage_only_frame, usage_payload
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin
from test_chat_completions import basic_request, latest_usage

VLLM_MODELS = {
    "data": [
        {"id": "Qwen/Qwen2.5-7B-Instruct", "owned_by": "vllm", "max_model_len": 32768},
        {"id": "other/model", "owned_by": "vllm", "max_model_len": 4096},
    ]
}
TOOL_CALLS = {
    "choices": [
        {
            "message": {
                "content": None,
                "tool_calls": [
                    {"id": "c", "type": "function", "function": {"name": "get_weather"}}
                ],
            }
        }
    ]
}
HERMES_MARKUP = {
    "choices": [{"message": {"content": '<tool_call>{"name": "get_weather"}</tool_call>'}}]
}


class TestPresets:
    @pytest.mark.parametrize(
        ("entry_id", "plugin", "flags"),
        [
            (
                "vllm",
                "vllm",
                ["--enable-auto-tool-choice", "--tool-call-parser", "--reasoning-parser"],
            ),
            (
                "sglang",
                "sglang",
                ["--tool-call-parser", "--reasoning-parser", "--enable-cache-report"],
            ),
            ("llamacpp", "llamacpp", ["--jinja"]),
        ],
    )
    def test_the_preset_loads_as_an_openai_compatible_entry(
        self, entry_id: str, plugin: str, flags: list[str]
    ) -> None:
        from gateway.plugins import registry

        entry = load_catalogue(override_dir=None).entries[entry_id]
        assert entry.plugin == plugin
        # No endpoint to pre-fill: the administrator's own, shown as an example.
        assert entry.base_url is None and entry.effective_base_url is None
        assert entry.base_url_example and entry.base_url_example.startswith("http://host:")
        notes = " ".join(entry.notes)
        for flag in flags:
            assert flag in notes
        described = {row["name"]: row for row in registry.describe()}[plugin]
        # A provider's type is its protocol; the vendor is only a quirk.
        assert described["protocol"] == "openai"
        assert described["self_hosted"] is True
        assert described["requires_api_key"] is False

    async def test_the_catalogue_endpoint_carries_the_example_and_notes(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        rows = (await client.get("/api/admin/provider-catalogue")).json()
        vllm = next(r for r in rows if r["id"] == "vllm")
        assert vllm["base_url_example"] == "http://host:8000/v1"
        assert any("--tool-call-parser" in n for n in vllm["notes"])


@pytest.fixture
async def vllm(
    app: Any, seeded: Seeded, session_factory: async_sessionmaker[AsyncSession]
) -> list[httpx.Request]:
    """The seeded provider made a vLLM; the control-plane client is its fake server."""
    async with session_factory() as db:
        await db.execute(
            update(Provider)
            .where(Provider.id == seeded.provider.id)
            .values(plugin="vllm", catalogue_id="vllm", base_url="http://gpu:8000/v1")
        )
        await db.commit()
    seen: list[httpx.Request] = []
    state: dict[str, Any] = {"chat": TOOL_CALLS}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/v1/models":
            return httpx.Response(200, json=VLLM_MODELS)
        if path == "/version":
            return httpx.Response(200, json={"version": "0.10.1"})
        if path == "/v1/chat/completions":
            return httpx.Response(200, json=state["chat"])
        return httpx.Response(404)

    app.state.control_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.test_vllm_state = state
    return seen


class TestDiscoveryAndImport:
    async def test_models_are_listed_free_with_the_servers_findings(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        vllm: list[httpx.Request],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        body = (
            await client.get(f"/api/admin/models/discover?provider_id={seeded.provider.id}")
        ).json()
        assert body["server"] == "vLLM 0.10.1"
        rows = {r["upstream_model"]: r for r in body["available"]}
        qwen = rows["Qwen/Qwen2.5-7B-Instruct"]
        # Zero rates, importable without a hand-typed price.
        assert Decimal(qwen["input_per_mtok"]) == 0 and Decimal(qwen["output_per_mtok"]) == 0
        assert qwen["blocked_reason"] is None
        assert qwen["context_window"] == 32768
        assert "tools" in qwen["supported_features"]
        assert qwen["input_modalities"] == ["text"]
        assert qwen["warnings"] == []

    async def test_a_missing_parser_is_a_warning_on_the_model(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        vllm: list[httpx.Request],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        app.state.test_vllm_state["chat"] = HERMES_MARKUP
        as_user(app, await make_admin(session_factory, seeded))
        body = (
            await client.get(f"/api/admin/models/discover?provider_id={seeded.provider.id}")
        ).json()
        row = next(r for r in body["available"] if r["upstream_model"] == "other/model")
        assert "tools" not in row["supported_features"]
        assert "e.g. hermes" in row["warnings"][0]

    async def test_the_probe_is_not_metered(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        vllm: list[httpx.Request],
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """It goes to the administrator's own server on the control-plane client:
        no usage row, and nothing through the metered upstream."""
        as_user(app, await make_admin(session_factory, seeded))
        await client.get(f"/api/admin/models/discover?provider_id={seeded.provider.id}")
        assert any(r.url.path == "/v1/chat/completions" for r in vllm)
        assert not fake_upstream.bodies
        async with session_factory() as db:
            assert (await db.execute(select(UsageRecord))).first() is None

    async def test_import_adopts_the_window_features_and_a_zero_price(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        vllm: list[httpx.Request],
        session_factory: async_sessionmaker[AsyncSession],
        session: AsyncSession,
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            f"/api/admin/models/import?provider_id={seeded.provider.id}",
            json={"models": [{"upstream_model": "Qwen/Qwen2.5-7B-Instruct"}]},
        )
        assert response.status_code == 201, response.text
        result = response.json()["results"][0]
        assert result["imported"] is True and result["priced"] is True
        model = (
            await session.execute(
                select(ModelDef).where(ModelDef.upstream_model == "Qwen/Qwen2.5-7B-Instruct")
            )
        ).scalar_one()
        assert model.context_window == 32768
        assert model.supported_features == ["tools"]
        assert model.input_modalities == ["text"]
        price = (
            await session.execute(select(ModelPrice).where(ModelPrice.model_id == model.id))
        ).scalar_one()
        assert price.input_per_mtok == 0 and price.output_per_mtok == 0
        assert price.source is PriceSource.HINT

    async def test_a_hosted_provider_is_never_probed(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(200, json={"data": [{"id": "x", "context_length": 8}]})

        app.state.control_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        as_user(app, await make_admin(session_factory, seeded))
        body = (
            await client.get(f"/api/admin/models/discover?provider_id={seeded.provider.id}")
        ).json()
        assert body["server"] is None and body["warnings"] == []
        assert seen == ["/v1/models"]


class TestRefreshOfAnExistingModel:
    async def adopt(
        self, session_factory: async_sessionmaker[AsyncSession], seeded: Seeded, **values: Any
    ) -> None:
        async with session_factory() as db:
            await db.execute(
                update(ModelDef)
                .where(ModelDef.id == seeded.model.id)
                .values(upstream_model="Qwen/Qwen2.5-7B-Instruct", **values)
            )
            await db.commit()

    async def test_it_proposes_only_where_the_model_has_nothing(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        vllm: list[httpx.Request],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await self.adopt(
            session_factory, seeded, context_window=None, supported_features=[], input_modalities=[]
        )
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(f"/api/admin/models/{seeded.model.id}/probe")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["tools"] is True
        assert body["proposal"] == {
            "context_window": 32768,
            "add_features": ["tools"],
            "input_modalities": ["text"],
        }

    async def test_it_never_proposes_over_what_an_admin_set(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        vllm: list[httpx.Request],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await self.adopt(
            session_factory,
            seeded,
            context_window=8000,
            supported_features=["tools", "json_mode"],
            input_modalities=["text", "image"],
        )
        as_user(app, await make_admin(session_factory, seeded))
        body = (await client.post(f"/api/admin/models/{seeded.model.id}/probe")).json()
        # The server's figure is shown; the admin's is not a candidate for change.
        assert body["reported_context_window"] == 32768
        assert body["proposal"] == {
            "context_window": None,
            "add_features": [],
            "input_modalities": [],
        }
        async with session_factory() as db:
            row = (
                await db.execute(select(ModelDef).where(ModelDef.id == seeded.model.id))
            ).scalar_one()
        # And the probe itself wrote nothing.
        assert row.context_window == 8000
        assert row.supported_features == ["tools", "json_mode"]
        assert row.input_modalities == ["text", "image"]

    async def test_a_non_admin_cannot_probe_or_discover(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        vllm: list[httpx.Request],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded, admin=False))
        for method, url in (
            ("POST", f"/api/admin/models/{seeded.model.id}/probe"),
            ("GET", f"/api/admin/models/discover?provider_id={seeded.provider.id}"),
            ("POST", f"/api/admin/models/import?provider_id={seeded.provider.id}"),
        ):
            response = await client.request(method, url, json={"models": []})
            assert response.status_code == 403, url
        # And nothing was sent to the server.
        assert vllm == []

    async def test_a_hosted_provider_cannot_be_probed(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(f"/api/admin/models/{seeded.model.id}/probe")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "not_self_hosted"


class TestDefaultReasoningEffort:
    async def set_default(
        self, session_factory: async_sessionmaker[AsyncSession], seeded: Seeded, effort: str | None
    ) -> None:
        async with session_factory() as db:
            await db.execute(
                update(ModelDef)
                .where(ModelDef.id == seeded.model.id)
                .values(default_reasoning_effort=effort)
            )
            await db.commit()

    async def test_a_request_with_no_effort_gets_the_models_default(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await self.set_default(session_factory, seeded, "high")
        fake_upstream.set_json(completion_body())
        await client.post("/v1/chat/completions", json=basic_request(), headers=seeded.auth)
        assert fake_upstream.last_body["reasoning_effort"] == "high"

    async def test_the_clients_own_effort_wins(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await self.set_default(session_factory, seeded, "high")
        fake_upstream.set_json(completion_body())
        await client.post(
            "/v1/chat/completions", json=basic_request(reasoning_effort="low"), headers=seeded.auth
        )
        assert fake_upstream.last_body["reasoning_effort"] == "low"
        await client.post(
            "/v1/chat/completions",
            json=basic_request(reasoning={"effort": "minimal"}),
            headers=seeded.auth,
        )
        assert "reasoning_effort" not in fake_upstream.last_body

    async def test_no_default_adds_nothing(
        self, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        fake_upstream.set_json(completion_body())
        await client.post("/v1/chat/completions", json=basic_request(), headers=seeded.auth)
        assert "reasoning_effort" not in fake_upstream.last_body

    async def test_v1_models_exposes_it(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await self.set_default(session_factory, seeded, "medium")
        cards = (await client.get("/v1/models", headers=seeded.auth)).json()["data"]
        card = next(c for c in cards if c["id"] == "test-model")
        assert card["default_reasoning_effort"] == "medium"

    async def test_it_is_editable_and_clearable_in_the_admin_api(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        url = f"/api/admin/models/{seeded.model.id}"
        on = await client.patch(url, json={"default_reasoning_effort": "xhigh"})
        assert on.json()["default_reasoning_effort"] == "xhigh"
        off = await client.patch(url, json={"default_reasoning_effort": None})
        assert off.json()["default_reasoning_effort"] is None
        bad = await client.patch(url, json={"default_reasoning_effort": "extreme"})
        assert bad.status_code == 400


class TestUsageAccounting:
    async def test_llamacpp_cache_n_is_read_as_cached_tokens(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # llama.cpp: usage carries no cache figure; `timings` beside it does.
        final = chunk(None, usage=usage_payload(100, 5))
        final["timings"] = {"cache_n": 60, "prompt_n": 40}
        fake_upstream.set_stream([sse(chunk("hi")), sse(final), sse("[DONE]")])
        await client.post(
            "/v1/chat/completions", json=basic_request(stream=True), headers=seeded.auth
        )
        row = await latest_usage(session_factory)
        assert row.cached_prompt_tokens == 60
        assert row.prompt_tokens == 100

    async def test_a_servers_own_cache_figure_is_not_overridden(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        body = completion_body()
        body["usage"] = usage_payload(100, 5, prompt_tokens_details={"cached_tokens": 30})
        body["timings"] = {"cache_n": 90}
        fake_upstream.set_json(body)
        await client.post("/v1/chat/completions", json=basic_request(), headers=seeded.auth)
        assert (await latest_usage(session_factory)).cached_prompt_tokens == 30

    async def test_cache_n_is_clamped_to_the_prompt(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        body = completion_body()
        body["usage"] = usage_payload(10, 5)
        body["timings"] = {"cache_n": 500}
        fake_upstream.set_json(body)
        await client.post("/v1/chat/completions", json=basic_request(), headers=seeded.auth)
        assert (await latest_usage(session_factory)).cached_prompt_tokens == 10

    async def test_usage_on_every_chunk_counts_once_with_the_last(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """vLLM's continuous_usage_stats: a running total on every frame."""
        fake_upstream.set_stream(
            [
                sse(chunk("a", usage=usage_payload(7, 1))),
                sse(chunk("b", usage=usage_payload(7, 2))),
                sse(chunk("c", finish_reason="stop", usage=usage_payload(7, 3))),
                sse(usage_only_frame(7, 3)),
                sse("[DONE]"),
            ]
        )
        await client.post(
            "/v1/chat/completions", json=basic_request(stream=True), headers=seeded.auth
        )
        row = await latest_usage(session_factory)
        assert (row.prompt_tokens, row.completion_tokens) == (7, 3)
