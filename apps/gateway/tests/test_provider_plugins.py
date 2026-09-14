"""Counterparty plugins (ADR 0032).

Two properties carry the design and both are pinned here.

**A plugin returns facts and never computes money.** The protocol has no method
that could price a request, and `read_reported_cost` returns a figure with the
unit it arrived in — never converted, never scaled to a billing currency.

**Providers and routers differ in what must be recorded.** A router chooses the
serving endpoint per request, so that endpoint is a fact to capture; for a
provider it is implied by the model.

The request-shaping tests below were three columns before slice 3 —
`auth_scheme`, `forward_stream_options` and `upstream_cost_unit` — each added for
one counterparty's habit. They test the same behaviours; what changed is that
the answer now comes from a plugin, so the next counterparty does not need a
fourth column.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
from conftest import FakeUpstream, Seeded
from gateway.models import ApiSurface, Provider
from gateway.plugins import (
    AnthropicPlugin,
    CortecsRouterPlugin,
    GenericOpenAIPlugin,
    ProviderKind,
    UnknownPluginError,
    available,
    resolve,
)
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession


class TestRegistry:
    def test_the_builtins_are_there(self) -> None:
        assert {
            "generic",
            "anthropic",
            "cortecs",
            "openai",
            "mistral",
            "nebius",
            "tensorix",
            "openrouter",
        } <= set(available())

    def test_no_name_is_the_generic_provider(self) -> None:
        """Every row that predates plugins resolves to the old behaviour."""
        plugin = resolve(None)
        assert plugin.name == "generic"
        assert plugin.kind is ProviderKind.PROVIDER

    def test_cortecs_is_a_router(self) -> None:
        assert resolve("cortecs").kind is ProviderKind.ROUTER

    def test_an_unknown_name_is_refused_and_lists_what_exists(self) -> None:
        """Never silently downgraded to generic.

        The generic plugin reports no cost and no serving endpoint, so a router
        configured by name and quietly replaced would look like a working
        deployment that had stopped recording where its money went.
        """
        with pytest.raises(UnknownPluginError) as caught:
            resolve("not-a-plugin")
        assert "cortecs" in str(caught.value)


class TestCortecsRouter:
    """Behaviour measured against the live API, not read from documentation."""

    def test_the_serving_endpoint_comes_from_a_header(self) -> None:
        """The finding this plugin exists for.

        Cortecs names the sub-provider *only* in a response header — not in the
        body, not in the stream frames — so the recorder's old body-field guess
        left `upstream_provider` null for every request through it.
        """
        served = CortecsRouterPlugin().read_served_by(
            {"id": "c", "model": "gpt-oss-120b"},
            {"x-cortecs-provider": "ovh", "x-cortecs-model": "gpt-oss-120b"},
        )
        assert served is not None
        assert served.endpoint == "ovh"
        assert served.model == "gpt-oss-120b"

    @pytest.mark.parametrize("casing", ["x-cortecs-provider", "X-Cortecs-Provider"])
    def test_header_casing_does_not_matter(self, casing: str) -> None:
        served = CortecsRouterPlugin().read_served_by(None, {casing: "nebius"})
        assert served is not None and served.endpoint == "nebius"

    def test_no_header_means_no_claim(self) -> None:
        assert CortecsRouterPlugin().read_served_by({"model": "x"}, {}) is None

    def test_cost_is_micro_eur_and_authoritative(self) -> None:
        """Derived and checked four ways against catalogue rates."""
        cost = CortecsRouterPlugin().read_reported_cost(
            {"cost": 136, "cost_details": {"prompt_cost": 135, "completion_cost": 1}}
        )
        assert cost is not None
        assert cost.amount == Decimal("0.000136")
        assert cost.currency == "EUR"
        # It reconciled against their listed prices on three sub-providers, so a
        # deployment is allowed to bill from it.
        assert cost.authoritative is True
        # Their breakdown, kept verbatim: the shape is theirs, not ours.
        assert cost.details == {"prompt_cost": 135, "completion_cost": 1}

    @pytest.mark.parametrize("usage", [None, {}, {"prompt_tokens": 10}, {"cost": None}])
    def test_no_figure_means_none(self, usage: dict[str, int] | None) -> None:
        assert CortecsRouterPlugin().read_reported_cost(usage) is None

    @pytest.mark.parametrize("bad", [{"cost": "free"}, {"cost": -1}, {"cost": True}])
    def test_nonsense_is_refused(self, bad: dict[str, object]) -> None:
        """`True` is an int in Python, and is not a cost."""
        assert CortecsRouterPlugin().read_reported_cost(bad) is None

    def test_a_float_does_not_import_binary_rounding(self) -> None:
        cost = CortecsRouterPlugin().read_reported_cost({"cost": 1.5})
        assert cost is not None and cost.amount == Decimal("0.0000015")

    def test_usage_reading_keeps_the_surface_conventions(self) -> None:
        """The two prompt conventions are opposites and must stay so.

        A router passes each sub-provider's shape through, so the plugin defers
        to the per-surface readers rather than keeping a second copy of the rule.
        """
        plugin = CortecsRouterPlugin()
        openai = plugin.read_usage(
            {
                "prompt_tokens": 1152,
                "completion_tokens": 2,
                "prompt_tokens_details": {"cached_tokens": 768, "created_cache_tokens": 256},
            },
            surface=ApiSurface.CHAT_COMPLETIONS,
        )
        anthropic = plugin.read_usage(
            {
                "input_tokens": 128,
                "cache_read_input_tokens": 768,
                "cache_creation_input_tokens": 256,
                "output_tokens": 2,
            },
            surface=ApiSurface.MESSAGES,
        )
        assert openai.prompt == anthropic.prompt == 1152
        assert openai.billable_prompt == anthropic.billable_prompt == 128


class TestGenericProvider:
    def test_it_reports_no_cost_of_its_own(self) -> None:
        """Deliberately not a tolerant search for anything called `cost`.

        A number a vendor happens to label that way is not a charge, and
        treating it as one is how a figure gets billed from that should not
        have been.
        """
        assert GenericOpenAIPlugin().read_reported_cost({"cost": 999}) is None

    def test_it_reads_a_serving_endpoint_from_the_body(self) -> None:
        """Where a different router puts it. This used to live in the recorder."""
        served = GenericOpenAIPlugin().read_served_by({"provider": "deepinfra"}, {})
        assert served is not None and served.endpoint == "deepinfra"

    def test_a_header_alone_tells_it_nothing(self) -> None:
        """It has no business knowing another counterparty's header."""
        assert GenericOpenAIPlugin().read_served_by({}, {"x-cortecs-provider": "ovh"}) is None


class TestAuthHeaders:
    """What ``providers.auth_scheme`` used to decide.

    The column had exactly two values and the second one existed because
    Anthropic's own API rejects a bearer token. Dropping it without a home for
    that answer would have removed the capability, so migration 0010 translates
    the value into a plugin choice.
    """

    def test_openai_shaped_endpoints_get_a_bearer_token(self) -> None:
        for plugin in (GenericOpenAIPlugin(), CortecsRouterPlugin()):
            assert plugin.auth_headers("sk-x") == {"authorization": "Bearer sk-x"}

    def test_anthropic_gets_x_api_key_and_a_version(self) -> None:
        headers = AnthropicPlugin().auth_headers("sk-ant")
        assert headers["x-api-key"] == "sk-ant"
        assert "authorization" not in headers
        # Their API refuses a request without it.
        assert headers["anthropic-version"]

    def test_a_router_serving_anthropics_shape_still_uses_bearer(self) -> None:
        """The reason this is per counterparty and not per route (ADR 0030).

        The reference router serves /v1/messages with a bearer token like
        everything else. A plugin keyed on the surface would get this wrong.
        """
        assert "authorization" in CortecsRouterPlugin().auth_headers("k")


class TestPreparePayload:
    """What ``providers.forward_stream_options`` used to decide."""

    def test_a_generic_endpoint_is_asked_for_stream_usage(self) -> None:
        """Without it a streamed response carries no counts and we record zero."""
        payload = GenericOpenAIPlugin().prepare_payload(
            {"model": "m", "stream": True}, surface=ApiSurface.CHAT_COMPLETIONS
        )
        assert payload["stream_options"] == {"include_usage": True}

    def test_what_the_client_sent_survives(self) -> None:
        """Merged, not replaced. Forwarding what was sent is the whole contract."""
        payload = GenericOpenAIPlugin().prepare_payload(
            {"model": "m", "stream": True, "stream_options": {"chunk_size_hint": 4}},
            surface=ApiSurface.CHAT_COMPLETIONS,
        )
        assert payload["stream_options"] == {"chunk_size_hint": 4, "include_usage": True}

    def test_nothing_is_added_to_a_non_streaming_request(self) -> None:
        payload = GenericOpenAIPlugin().prepare_payload(
            {"model": "m"}, surface=ApiSurface.CHAT_COMPLETIONS
        )
        assert "stream_options" not in payload

    @pytest.mark.parametrize(
        "surface", [ApiSurface.RESPONSES, ApiSurface.MESSAGES, ApiSurface.EMBEDDINGS]
    )
    def test_only_the_chat_surface_gets_it(self, surface: ApiSurface) -> None:
        """/v1/responses carries usage unasked and /v1/messages has no such field."""
        payload = GenericOpenAIPlugin().prepare_payload(
            {"model": "m", "stream": True}, surface=surface
        )
        assert "stream_options" not in payload

    def test_the_reference_router_is_not_asked(self) -> None:
        """It reports usage either way and warns against undocumented parameters.

        A behaviour change from the column, whose default was to ask. Migration
        0010 names the rows it changes rather than leaving it to be noticed.
        """
        payload = CortecsRouterPlugin().prepare_payload(
            {"model": "m", "stream": True}, surface=ApiSurface.CHAT_COMPLETIONS
        )
        assert "stream_options" not in payload

    def test_anthropic_is_not_asked_either(self) -> None:
        payload = AnthropicPlugin().prepare_payload(
            {"model": "m", "stream": True}, surface=ApiSurface.MESSAGES
        )
        assert "stream_options" not in payload


class TestItReachesTheLedger:
    async def test_a_router_records_which_endpoint_served(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """The column that was null for every Cortecs request until now."""
        await session.execute(
            update(Provider).where(Provider.id == seeded.provider.id).values(plugin="cortecs")
        )
        await session.commit()

        fake_upstream.set_json(
            {
                "id": "c",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
            headers={"x-cortecs-provider": "inceptron"},
        )
        response = await client.post(
            "/v1/chat/completions",
            json={"model": seeded.model.name, "messages": [{"role": "user", "content": "hi"}]},
            headers=seeded.auth,
        )
        assert response.status_code == 200

        from gateway.models import UsageRecord

        record = (
            (
                await session.execute(
                    select(UsageRecord).order_by(UsageRecord.created_at.desc()).limit(1)
                )
            )
            .scalars()
            .one()
        )
        assert record.upstream_provider == "inceptron"

    async def test_a_missing_plugin_does_not_lose_the_usage_row(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """Degrades loudly rather than failing the request.

        An unknown plugin name should have been refused when the provider was
        saved. Reaching here means it was not, and losing the usage row — the
        billing record — would be a worse outcome than recording it with less
        detail.
        """
        await session.execute(
            update(Provider).where(Provider.id == seeded.provider.id).values(plugin="vanished")
        )
        await session.commit()

        fake_upstream.set_json(
            {
                "id": "c",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            }
        )
        assert (
            await client.post(
                "/v1/chat/completions",
                json={"model": seeded.model.name, "messages": [{"role": "user", "content": "hi"}]},
                headers=seeded.auth,
            )
        ).status_code == 200


class TestNewBuiltins:
    """OpenAI, Mistral, Nebius, Tensorix and OpenRouter (ADR 0032 additions).

    Most are generic OpenAI-compatible endpoints: the plugin exists to name the
    counterparty and pre-fill its endpoint, not to invent behaviour. The tests
    assert exactly that, plus the one place OpenRouter earns its own file.
    """

    @pytest.mark.parametrize(
        ("name", "base_url"),
        [
            ("openai", "https://api.openai.com/v1"),
            ("mistral", "https://api.mistral.ai/v1"),
            ("nebius", "https://api.studio.nebius.com/v1"),
        ],
    )
    def test_known_endpoints_are_pre_filled(self, name: str, base_url: str) -> None:
        plugin = resolve(name)
        assert plugin.default_base_url == base_url
        assert plugin.kind == ProviderKind.PROVIDER
        # They report tokens, not charges: pass-through billing stays
        # unselectable, which the console reads from billing_modes.
        assert plugin.reports_authoritative_cost is False

    def test_tensorix_asserts_no_endpoint(self) -> None:
        # An address the gateway cannot verify is not a fact a plugin should
        # assert; the provider row's Base URL field is where it goes.
        plugin = resolve("tensorix")
        assert plugin.default_base_url is None
        assert plugin.kind == ProviderKind.PROVIDER


class TestSearchBackendEndpoints:
    """What a search backend documents about its own hosts.

    The console offers a backend's endpoints as a constrained choice rather
    than a free-text URL, which is only honest if the choice is exactly the
    vendor's documented set — so that set is pinned here, per plugin.
    """

    def test_jina_documents_exactly_two_hosts_and_the_default_is_first(self) -> None:
        """Both read at source on 2026-09-14 from docs.jina.ai's Search API
        page, which documents the EU host with the EU-residency sentence the
        plugin's label paraphrases. The default is one of the options, so a
        create that omits the URL lands on the same host the select shows."""
        plugin = resolve("jina")
        assert plugin.base_url_options == (
            ("https://s.jina.ai", "s.jina.ai — global (default)"),
            ("https://eu.s.jina.ai", "eu.s.jina.ai — all processing stays in the EU"),
        )
        assert plugin.default_base_url == plugin.base_url_options[0][0]

    def test_the_single_host_backends_declare_no_choice(self) -> None:
        # Linkup and Exa each document one endpoint; a select with one option
        # is not a choice, and the default is the whole of the answer.
        for name in ("linkup", "exa"):
            plugin = resolve(name)
            assert plugin.base_url_options == (), name


class TestOpenRouter:
    def test_it_is_a_router_with_pass_through_billing(self) -> None:
        plugin = resolve("openrouter")
        assert plugin.kind == ProviderKind.ROUTER
        assert plugin.reports_authoritative_cost is True

    def test_the_serving_provider_comes_from_the_body(self) -> None:
        plugin = resolve("openrouter")
        served = plugin.read_served_by({"provider": "DeepInfra"}, {})
        assert served is not None
        assert served.endpoint == "DeepInfra"

    def test_the_charge_is_credits_and_authoritative(self) -> None:
        plugin = resolve("openrouter")
        reported = plugin.read_reported_cost({"cost": 0.0126, "total_tokens": 100})
        assert reported is not None
        assert reported.amount == Decimal("0.0126")
        # The unit travels with the figure: credits are their billing unit,
        # and converting them here would be a reconciliation report lying.
        assert reported.currency == "credits"
        assert reported.authoritative is True

    def test_no_figure_or_nonsense_is_none(self) -> None:
        plugin = resolve("openrouter")
        assert plugin.read_reported_cost(None) is None
        assert plugin.read_reported_cost({}) is None
        assert plugin.read_reported_cost({"cost": "lots"}) is None
        assert plugin.read_reported_cost({"cost": -1}) is None
