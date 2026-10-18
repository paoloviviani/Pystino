"""Catalogue discovery and import.

The catalogue is an allowlist an administrator curates. These endpoints remove the
tedium of finding out what a provider offers without moving the decision away from a
person — so most of what is asserted here is what the endpoints refuse to do.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import httpx
import pytest
from conftest import Seeded
from gateway.fx import FXService
from gateway.models import (
    FXRate,
    GroupModelAccess,
    ModelDef,
    ModelKind,
    ModelPrice,
    PriceSource,
)
from gateway.types import utcnow
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin

CATALOGUE = {
    "data": [
        {
            "id": "upstream/test-model",
            "context_length": 8192,
            "pricing": {"input_token": "1", "output_token": "2", "currency": "EUR"},
        },
        {
            "id": "vendor/new-small",
            "context_length": 32000,
            "pricing": {"input_token": "0.10", "output_token": "0.40", "currency": "EUR"},
        },
        {
            "id": "vendor/new-large",
            "context_length": 200000,
            "pricing": {"input_token": "3", "output_token": "15", "currency": "EUR"},
        },
        {
            "id": "vendor/dollar-model",
            "pricing": {"input_token": "1", "output_token": "2", "currency": "USD"},
        },
    ]
}


#: A provider that lists a model and publishes no price for it — the shape of
#: every first-party API (OpenAI, Anthropic, Mistral, Nebius) and the reason the
#: community fill exists at all (ADR 0053).
CATALOGUE_WITH_A_GAP = {
    "data": [
        {
            "id": "vendor/priced-1",
            "pricing": {"input_token": "1", "output_token": "2", "currency": "EUR"},
        },
        {"id": "vendor/unpriced-1"},
        {"id": "vendor/nobody-prices-this"},
    ]
}

#: LiteLLM's file, keyed by model id. It knows `unpriced-1`, and it also carries
#: a figure for `priced-1` that must never be used.
COMMUNITY_CATALOGUE = {
    "vendor/unpriced-1": {
        "litellm_provider": "generic",
        "input_cost_per_token": 0.0000025,
        "output_cost_per_token": 0.00001,
        "max_input_tokens": 128000,
        "mode": "chat",
    },
    "vendor/priced-1": {
        "litellm_provider": "generic",
        "input_cost_per_token": 0.99,
        "output_cost_per_token": 0.99,
        "mode": "chat",
    },
}


#: Entrim's shape: no prices, limits on the entry (verified 2026-10-18).
ENTRIM_CATALOGUE = {
    "data": [
        {
            "id": "zai-org/GLM-5.3",
            "object": "model",
            "owned_by": "zai-org",
            "max_input_tokens": 1048576,
            "max_output_tokens": 65536,
            "datacenters": [{"country_code": "SI"}],
        }
    ]
}


@pytest.fixture
def entrim_catalogue(app: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ENTRIM_CATALOGUE)

    app.state.control_http = httpx.AsyncClient(  # type: ignore[attr-defined]
        transport=httpx.MockTransport(handler)
    )


class TestUnpricedCatalogueLimits:
    async def test_discovery_shows_an_unpriced_models_limits(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        entrim_catalogue: None,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        body = (
            await client.get("/api/admin/models/discover?provider_id=" + str(seeded.provider.id))
        ).json()
        row = body["available"][0]
        assert row["price_source"] is None
        assert row["context_window"] == 1048576
        assert row["max_output_tokens"] == 65536
        assert row["max_input_tokens"] is None

    async def test_a_manual_price_import_carries_the_limits(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        entrim_catalogue: None,
        session_factory: async_sessionmaker[AsyncSession],
        session: AsyncSession,
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/models/import?provider_id=" + str(seeded.provider.id),
            json={
                "models": [
                    {
                        "upstream_model": "zai-org/GLM-5.3",
                        "manual_price": {"input_per_mtok": "1", "output_per_mtok": "2"},
                    }
                ]
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["results"][0]["imported"] is True
        model = (
            await session.execute(
                select(ModelDef).where(ModelDef.upstream_model == "zai-org/GLM-5.3")
            )
        ).scalar_one()
        assert model.context_window == 1048576
        assert model.max_output_tokens == 65536
        # The operator's own cap is never filled from a catalogue.
        assert model.max_input_tokens is None


@pytest.fixture
def catalogue(app: object) -> None:
    """Point the control-plane client at a canned provider catalogue."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=CATALOGUE)

    app.state.control_http = httpx.AsyncClient(  # type: ignore[attr-defined]
        transport=httpx.MockTransport(handler)
    )


@pytest.fixture
def catalogue_with_a_gap(app: object) -> None:
    """Both sources, answered by URL: the provider's own, and LiteLLM's file."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "litellm" in str(request.url):
            return httpx.Response(200, json=COMMUNITY_CATALOGUE)
        return httpx.Response(200, json=CATALOGUE_WITH_A_GAP)

    app.state.control_http = httpx.AsyncClient(  # type: ignore[attr-defined]
        transport=httpx.MockTransport(handler)
    )


class TestCommunityPriceFill:
    """The provider lists what it offers; the community file fills gaps only.

    Written after the flow was reversed (ADR 0053): the question used to be
    *either* the provider's catalogue *or* LiteLLM's, asked before a provider
    had even been chosen — which meant the model list itself could come from a
    community file, and an operator who picked wrong got an empty screen.
    """

    async def test_an_unpriced_model_is_listed_and_says_nobody_priced_it(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue_with_a_gap: None,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Visible without the fill, with the reason. Hiding it is what made a
        discovery against OpenAI look like an endpoint serving nothing."""
        as_user(app, await make_admin(session_factory, seeded))
        body = (
            await client.get("/api/admin/models/discover?provider_id=" + str(seeded.provider.id))
        ).json()

        rows = {row["upstream_model"]: row for row in body["available"]}
        assert set(rows) == {"vendor/priced-1", "vendor/unpriced-1", "vendor/nobody-prices-this"}
        assert rows["vendor/priced-1"]["price_source"] == "provider"
        assert rows["vendor/unpriced-1"]["price_source"] is None
        assert "no price" in rows["vendor/unpriced-1"]["blocked_reason"]
        # Offered means listed, priced or not.
        assert body["provider_model_count"] == 3

    async def test_the_fill_prices_the_gap_and_marks_it(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue_with_a_gap: None,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        body = (
            await client.get(
                "/api/admin/models/discover?fill_missing_prices=true&provider_id="
                + str(seeded.provider.id)
            )
        ).json()

        rows = {row["upstream_model"]: row for row in body["available"]}
        filled = rows["vendor/unpriced-1"]
        assert filled["price_source"] == "community"
        assert filled["blocked_reason"] is None
        # 0.0000025 USD per token is 2.50 per million — the unit conversion the
        # per-token price implies, and the off-by-a-million the parsers exist
        # to prevent.
        assert filled["input_per_mtok"].startswith("2.5")
        assert filled["currency"] == "USD"
        assert filled["context_window"] == 128000
        # Still nobody's price, and still not importable.
        assert rows["vendor/nobody-prices-this"]["price_source"] is None

    async def test_the_fill_never_replaces_a_published_price(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue_with_a_gap: None,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The community file quotes 0.99/token for this model. The provider
        that will invoice us says 1 per million, and that is what stands."""
        as_user(app, await make_admin(session_factory, seeded))
        body = (
            await client.get(
                "/api/admin/models/discover?fill_missing_prices=true&provider_id="
                + str(seeded.provider.id)
            )
        ).json()

        priced = next(r for r in body["available"] if r["upstream_model"] == "vendor/priced-1")
        assert priced["price_source"] == "provider"
        assert priced["input_per_mtok"].startswith("1")
        assert priced["currency"] == "EUR"

    async def test_an_imported_community_price_is_recorded_as_one(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue_with_a_gap: None,
        session_factory: async_sessionmaker[AsyncSession],
        session: AsyncSession,
    ) -> None:
        """The durable half. Until this, every catalogue import wrote
        `cortecs`, so a community figure for someone else's model was stored as
        that counterparty's own published price."""
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/models/import?fill_missing_prices=true&provider_id="
            + str(seeded.provider.id),
            json={"models": [{"upstream_model": "vendor/unpriced-1"}]},
        )
        assert response.status_code == 201
        assert response.json()["results"][0]["price_source"] == "community"

        price = (
            await session.execute(
                select(ModelPrice)
                .join(ModelDef, ModelDef.id == ModelPrice.model_id)
                .where(ModelDef.upstream_model == "vendor/unpriced-1")
            )
        ).scalar_one()
        assert price.source is PriceSource.COMMUNITY

    async def test_a_provider_price_imports_as_the_providers_own(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue_with_a_gap: None,
        session_factory: async_sessionmaker[AsyncSession],
        session: AsyncSession,
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/models/import?fill_missing_prices=true&provider_id="
            + str(seeded.provider.id),
            json={"models": [{"upstream_model": "vendor/priced-1"}]},
        )
        assert response.json()["results"][0]["price_source"] == "provider"

        price = (
            await session.execute(
                select(ModelPrice)
                .join(ModelDef, ModelDef.id == ModelPrice.model_id)
                .where(ModelDef.upstream_model == "vendor/priced-1")
            )
        ).scalar_one()
        assert price.source is PriceSource.CATALOGUE

    async def test_an_unpriced_model_cannot_be_imported(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue_with_a_gap: None,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """An unpriced model serves happily and records a cost of zero, which is
        a quiet way to give away money."""
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/models/import?provider_id=" + str(seeded.provider.id),
            json={"models": [{"upstream_model": "vendor/unpriced-1"}]},
        )
        result = response.json()["results"][0]
        assert result["imported"] is False
        assert result["priced"] is False
        # The refusal is worded as it always was: without a price from anyone,
        # the model is simply not offered for import.
        assert result["reason"] == "not offered by the provider"


#: Unpriced entries that say what they are, so a hand-typed price can be judged
#: against the kind it must serve. `kinds_by_id` reads these without a price —
#: that is what it is for.
CATALOGUE_OF_KINDS = {
    "data": [
        {"id": "vendor/plain-chat"},
        {"id": "vendor/the-embedder", "output_modalities": ["embeddings"]},
        {"id": "vendor/the-reader", "tags": ["OCR"]},
        {"id": "vendor/the-finder", "tags": ["Search"]},
    ]
}


@pytest.fixture
def catalogue_of_kinds(app: object) -> None:
    """Unpriced entries of every kind, answered by the control-plane client."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=CATALOGUE_OF_KINDS)

    app.state.control_http = httpx.AsyncClient(  # type: ignore[attr-defined]
        transport=httpx.MockTransport(handler)
    )


class TestManualPriceImport:
    """A price typed by an administrator, for a model nobody prices.

    The refusal to import an unpriced model stands: what is new is that the
    operator can *supply* the price at import, in the same shape the model
    page's price form takes, recorded in the same append-only history with the
    source that says a person typed it. A catalogue that does publish a price
    wins — the counterparty's own figure outranks one typed from memory, and a
    hand-typed price that silently lost to a published one would be exactly the
    mismatch between reviewed and written prices this screen exists to prevent,
    so the result says so when it happens.
    """

    async def test_an_unpriced_model_imports_with_the_price_entered_by_hand(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue_with_a_gap: None,
        session_factory: async_sessionmaker[AsyncSession],
        session: AsyncSession,
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/models/import?provider_id=" + str(seeded.provider.id),
            json={
                "models": [
                    {
                        "upstream_model": "vendor/unpriced-1",
                        "manual_price": {
                            "input_per_mtok": "1.5",
                            "output_per_mtok": "6",
                            "currency": "EUR",
                        },
                    }
                ]
            },
        )
        assert response.status_code == 201, response.text
        result = response.json()["results"][0]
        assert result["imported"] is True
        assert result["priced"] is True
        assert result["price_source"] == "manual"
        assert result["reason"] is None

        price = (
            await session.execute(
                select(ModelPrice)
                .join(ModelDef, ModelDef.id == ModelPrice.model_id)
                .where(ModelDef.upstream_model == "vendor/unpriced-1")
            )
        ).scalar_one()
        assert price.source is PriceSource.MANUAL
        assert str(price.input_per_mtok) == "1.500000000000"
        assert str(price.output_per_mtok) == "6.000000000000"
        assert price.currency == "EUR"

    async def test_cache_rates_entered_by_hand_land_on_the_price_row(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue_with_a_gap: None,
        session_factory: async_sessionmaker[AsyncSession],
        session: AsyncSession,
    ) -> None:
        """The price form can set cache rates; the import door must too, or an
        unpriced provider's cached tokens are billed at the full input rate."""
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/models/import?provider_id=" + str(seeded.provider.id),
            json={
                "models": [
                    {
                        "upstream_model": "vendor/unpriced-1",
                        "manual_price": {
                            "input_per_mtok": "1.5",
                            "output_per_mtok": "6",
                            "cache_read_per_mtok": "0.15",
                            "cache_write_per_mtok": "1.875",
                        },
                    }
                ]
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["results"][0]["imported"] is True
        price = (
            await session.execute(
                select(ModelPrice)
                .join(ModelDef, ModelDef.id == ModelPrice.model_id)
                .where(ModelDef.upstream_model == "vendor/unpriced-1")
            )
        ).scalar_one()
        assert price.source is PriceSource.MANUAL
        assert str(price.cache_read_per_mtok) == "0.150000000000"
        assert str(price.cache_write_per_mtok) == "1.875000000000"

    async def test_a_manual_price_without_cache_rates_leaves_them_null(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue_with_a_gap: None,
        session_factory: async_sessionmaker[AsyncSession],
        session: AsyncSession,
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/models/import?provider_id=" + str(seeded.provider.id),
            json={
                "models": [
                    {
                        "upstream_model": "vendor/unpriced-1",
                        "manual_price": {"input_per_mtok": "1", "output_per_mtok": "2"},
                    }
                ]
            },
        )
        assert response.status_code == 201, response.text
        price = (
            await session.execute(
                select(ModelPrice)
                .join(ModelDef, ModelDef.id == ModelPrice.model_id)
                .where(ModelDef.upstream_model == "vendor/unpriced-1")
            )
        ).scalar_one()
        assert price.cache_read_per_mtok is None
        assert price.cache_write_per_mtok is None

    async def test_a_negative_cache_rate_is_refused_as_on_the_price_form(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue_with_a_gap: None,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/models/import?provider_id=" + str(seeded.provider.id),
            json={
                "models": [
                    {
                        "upstream_model": "vendor/unpriced-1",
                        "manual_price": {
                            "input_per_mtok": "1",
                            "output_per_mtok": "2",
                            "cache_read_per_mtok": "-0.1",
                        },
                    }
                ]
            },
        )
        assert response.status_code == 400, response.text
        assert "cache_read_per_mtok" in response.text

    async def test_the_price_entered_by_hand_round_trips_as_a_string(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue_with_a_gap: None,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Money is a string end to end: what the operator typed reaches the
        price history as digits, never a float and never an exponent."""
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/models/import?provider_id=" + str(seeded.provider.id),
            json={
                "models": [
                    {
                        "upstream_model": "vendor/unpriced-1",
                        "manual_price": {"input_per_mtok": "0.001", "output_per_mtok": "0.002"},
                    }
                ]
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["results"][0]["imported"] is True
        items = (await client.get("/api/admin/models?limit=200")).json()["items"]
        model_id = next(m["id"] for m in items if m["upstream_model"] == "vendor/unpriced-1")
        listed = (await client.get(f"/api/admin/models/{model_id}/prices")).json()["items"]
        assert len(listed) == 1
        assert listed[0]["input_per_mtok"] == "0.001000000000"
        assert listed[0]["output_per_mtok"] == "0.002000000000"
        # The stored precision is twelve places; the point here is the type.
        assert isinstance(listed[0]["input_per_mtok"], str)

    async def test_a_published_price_beats_the_one_entered_by_hand(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue_with_a_gap: None,
        session_factory: async_sessionmaker[AsyncSession],
        session: AsyncSession,
    ) -> None:
        """The counterparty's own figure outranks one typed from memory — but
        the result says the hand-typed price was not used, so nothing is
        silently different from what the operator reviewed."""
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/models/import?provider_id=" + str(seeded.provider.id),
            json={
                "models": [
                    {
                        "upstream_model": "vendor/priced-1",
                        "manual_price": {"input_per_mtok": "9", "output_per_mtok": "9"},
                    }
                ]
            },
        )
        assert response.status_code == 201, response.text
        result = response.json()["results"][0]
        assert result["imported"] is True
        assert result["priced"] is True
        assert result["price_source"] == "provider"
        assert "by hand" in result["reason"]

        price = (
            await session.execute(
                select(ModelPrice)
                .join(ModelDef, ModelDef.id == ModelPrice.model_id)
                .where(ModelDef.upstream_model == "vendor/priced-1")
            )
        ).scalar_one()
        assert price.source is PriceSource.CATALOGUE
        assert str(price.input_per_mtok) == "1.000000000000"

    async def test_a_manual_price_that_cannot_serve_the_kind_is_refused(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue_of_kinds: None,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A chat model bills output tokens too; a price with no input rate
        prices nothing at all. The refusal is per item, so the rest of the
        batch still imports."""
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/models/import?provider_id=" + str(seeded.provider.id),
            json={
                "models": [
                    {
                        "upstream_model": "vendor/plain-chat",
                        "manual_price": {"per_page": "0.01"},
                    },
                    {
                        "upstream_model": "vendor/the-embedder",
                        "manual_price": {"output_per_mtok": "3"},
                    },
                ]
            },
        )
        assert response.status_code == 201, response.text
        results = {r["upstream_model"]: r for r in response.json()["results"]}
        assert results["vendor/plain-chat"]["imported"] is False
        assert "input rate" in results["vendor/plain-chat"]["reason"]
        assert results["vendor/the-embedder"]["imported"] is False
        assert "input rate" in results["vendor/the-embedder"]["reason"]

    async def test_an_ocr_model_needs_a_per_page_price(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue_of_kinds: None,
        session_factory: async_sessionmaker[AsyncSession],
        session: AsyncSession,
    ) -> None:
        """An OCR model's price is per page; token rates alone price nothing.
        With the page rate present, the token rates are stored as the zeros the
        catalogue path stores, and the currency defaults to the gateway's
        billing currency, exactly as the model page's price form does."""
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/models/import?provider_id=" + str(seeded.provider.id),
            json={
                "models": [
                    {
                        "upstream_model": "vendor/the-reader",
                        "manual_price": {"input_per_mtok": "1", "output_per_mtok": "2"},
                    }
                ]
            },
        )
        results = {r["upstream_model"]: r for r in response.json()["results"]}
        assert results["vendor/the-reader"]["imported"] is False
        assert "per page" in results["vendor/the-reader"]["reason"]

        response = await client.post(
            "/api/admin/models/import?provider_id=" + str(seeded.provider.id),
            json={
                "models": [
                    {"upstream_model": "vendor/the-reader", "manual_price": {"per_page": "0.01"}}
                ]
            },
        )
        assert response.status_code == 201, response.text
        result = response.json()["results"][0]
        assert result["imported"] is True
        assert result["priced"] is True
        assert result["price_source"] == "manual"

        price = (
            await session.execute(
                select(ModelPrice)
                .join(ModelDef, ModelDef.id == ModelPrice.model_id)
                .where(ModelDef.upstream_model == "vendor/the-reader")
            )
        ).scalar_one()
        assert str(price.per_page) == "0.010000000000"
        # Zero round-trips as `Decimal("0E-12")` off a Numeric column — the
        # boundary serialiser, not the column, renders plain digits — so the
        # zero rates are asserted by value.
        assert price.input_per_mtok == 0
        assert price.currency == "EUR"

    async def test_a_search_tier_is_unchanged(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue_of_kinds: None,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A search tier is metered per request (ADR 0071); there is no token
        rate to supply and no price to type, with or without one in hand."""
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/models/import?provider_id=" + str(seeded.provider.id),
            json={
                "models": [
                    {"upstream_model": "vendor/the-finder"},
                    {
                        "upstream_model": "vendor/the-finder",
                        "manual_price": {"input_per_mtok": "1", "output_per_mtok": "2"},
                    },
                ]
            },
        )
        assert response.status_code == 201, response.text
        results = response.json()["results"]
        assert all(r["imported"] is False and r["priced"] is False for r in results)

    async def test_zero_rates_are_allowed_as_on_the_price_form(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue_of_kinds: None,
        session_factory: async_sessionmaker[AsyncSession],
        session: AsyncSession,
    ) -> None:
        """The model page's price form accepts a rate of zero — a deliberate
        "free" — so a hand-typed one may say the same thing. What is refused is
        an *absent* rate, not a zero one."""
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/models/import?provider_id=" + str(seeded.provider.id),
            json={
                "models": [
                    {
                        "upstream_model": "vendor/plain-chat",
                        "manual_price": {"input_per_mtok": "0", "output_per_mtok": "0"},
                    }
                ]
            },
        )
        assert response.status_code == 201, response.text
        result = response.json()["results"][0]
        assert result["imported"] is True
        assert result["priced"] is True
        price = (
            await session.execute(
                select(ModelPrice)
                .join(ModelDef, ModelDef.id == ModelPrice.model_id)
                .where(ModelDef.upstream_model == "vendor/plain-chat")
            )
        ).scalar_one()
        assert price.input_per_mtok == 0

    async def test_a_foreign_currency_is_kept_and_converted_at_billing(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue_with_a_gap: None,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A price typed in USD is stored in USD, the same as a catalogue
        price in USD: ADR 0054 converts it at admission and in reports with
        the day's rate, so refusing it here would only send the operator to
        convert by hand with a rate nobody records."""
        async with session_factory() as session:
            session.add(FXRate(base="USD", quote="EUR", rate=Decimal("0.9"), fetched_at=utcnow()))
            await session.commit()
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/models/import?provider_id=" + str(seeded.provider.id),
            json={
                "models": [
                    {
                        "upstream_model": "vendor/unpriced-1",
                        "manual_price": {
                            "input_per_mtok": "1",
                            "output_per_mtok": "2",
                            "currency": "usd",
                        },
                    }
                ]
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["results"][0]["imported"] is True
        async with session_factory() as session:
            price = (
                await session.execute(
                    select(ModelPrice)
                    .join(ModelDef, ModelDef.id == ModelPrice.model_id)
                    .where(ModelDef.upstream_model == "vendor/unpriced-1")
                )
            ).scalar_one()
        assert price.currency == "USD"

    async def test_a_currency_with_no_exchange_rate_is_refused(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue_with_a_gap: None,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Stored anyway, a price in a currency no rate exists for would make
        admission refuse every request on the model, with no hint why."""

        async def no_rate(*_args: object) -> None:
            return None

        monkeypatch.setattr(FXService, "_fetch", no_rate)
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/models/import?provider_id=" + str(seeded.provider.id),
            json={
                "models": [
                    {
                        "upstream_model": "vendor/unpriced-1",
                        "manual_price": {
                            "input_per_mtok": "1",
                            "output_per_mtok": "2",
                            "currency": "XYZ",
                        },
                    }
                ]
            },
        )
        assert response.status_code == 201, response.text
        result = response.json()["results"][0]
        assert result["imported"] is False
        assert "No exchange rate from XYZ to EUR" in result["reason"]

    async def test_a_manual_price_cannot_import_a_model_the_provider_does_not_list(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue_with_a_gap: None,
        session_factory: async_sessionmaker[AsyncSession],
        session: AsyncSession,
    ) -> None:
        """ "Unpriced" is a statement about a model the provider *lists*. A
        manual price is a figure for one of those, not a licence to invent an
        upstream id: an id the catalogue does not name at all is refused
        exactly as before, price or no price — it would otherwise come in with
        the chat default for a kind nobody declared."""
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/models/import?provider_id=" + str(seeded.provider.id),
            json={
                "models": [
                    {
                        "upstream_model": "vendor/imaginary",
                        "manual_price": {"input_per_mtok": "1", "output_per_mtok": "2"},
                    }
                ]
            },
        )
        assert response.status_code == 201, response.text
        result = response.json()["results"][0]
        assert result["imported"] is False
        assert result["priced"] is False
        assert result["reason"] == "not offered by the provider"

        # And nothing was created: no model row, no price row.
        found = (
            await session.execute(
                select(ModelDef).where(ModelDef.upstream_model == "vendor/imaginary")
            )
        ).scalar_one_or_none()
        assert found is None

    async def test_the_block_reason_offers_entering_a_price(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue_with_a_gap: None,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A model the provider lists unpriced is no longer a dead end: the
        reason says a price can be entered, rather than pointing at the
        community file or hand-added models as the only ways in."""
        as_user(app, await make_admin(session_factory, seeded))
        body = (
            await client.get("/api/admin/models/discover?provider_id=" + str(seeded.provider.id))
        ).json()
        rows = {row["upstream_model"]: row for row in body["available"]}
        assert rows["vendor/unpriced-1"]["blocked_reason"] == (
            "the provider publishes no price: enter one to import it"
        )

        filled = (
            await client.get(
                "/api/admin/models/discover?fill_missing_prices=true&provider_id="
                + str(seeded.provider.id)
            )
        ).json()
        nobody = {row["upstream_model"]: row for row in filled["available"]}[
            "vendor/nobody-prices-this"
        ]
        assert "enter one" in nobody["blocked_reason"]
        assert "fill missing prices" not in nobody["blocked_reason"]
        assert "by hand" not in nobody["blocked_reason"]


class TestDiscovery:
    async def test_reports_what_we_do_not_have(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue: None,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        body = (
            await client.get("/api/admin/models/discover?provider_id=" + str(seeded.provider.id))
        ).json()

        assert body["provider_model_count"] == 4
        available = {m["upstream_model"] for m in body["available"]}
        assert available == {"vendor/new-small", "vendor/new-large", "vendor/dollar-model"}
        # The one we already serve is not offered as a candidate.
        assert "upstream/test-model" not in available

    async def test_suggests_a_name_without_the_provider_prefix(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue: None,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        body = (
            await client.get("/api/admin/models/discover?provider_id=" + str(seeded.provider.id))
        ).json()
        small = next(m for m in body["available"] if m["upstream_model"] == "vendor/new-small")
        assert small["suggested_name"] == "new-small"
        assert small["context_window"] == 32000

    async def test_a_foreign_currency_is_importable_not_blocked(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue: None,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """ADR 0054: a USD-priced model imports with its USD prices — the
        conversion happens at admission, using the day's rate."""
        as_user(app, await make_admin(session_factory, seeded))
        body = (
            await client.get("/api/admin/models/discover?provider_id=" + str(seeded.provider.id))
        ).json()
        dollar = next(m for m in body["available"] if m["upstream_model"] == "vendor/dollar-model")
        assert dollar["blocked_reason"] is None

    async def test_reports_models_the_provider_no_longer_offers(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue: None,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Drift in the direction that fails at 3am rather than at review time."""
        async with session_factory() as session:
            session.add(
                ModelDef(
                    name="retired",
                    upstream_model="vendor/gone",
                    provider_id=seeded.provider.id,
                )
            )
            await session.commit()

        as_user(app, await make_admin(session_factory, seeded))
        body = (
            await client.get("/api/admin/models/discover?provider_id=" + str(seeded.provider.id))
        ).json()
        assert [m["name"] for m in body["missing_upstream"]] == ["retired"]
        assert [m["name"] for m in body["catalogued"]] == ["test-model"]

    async def test_an_unreachable_provider_is_502_not_500(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        def broken(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        app.state.control_http = httpx.AsyncClient(  # type: ignore[attr-defined]
            transport=httpx.MockTransport(broken)
        )
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.get(
            "/api/admin/models/discover?provider_id=" + str(seeded.provider.id)
        )
        assert response.status_code == 502

    async def test_a_non_admin_cannot_probe_the_provider(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue: None,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded, admin=False))
        assert (
            await client.get("/api/admin/models/discover?provider_id=" + str(seeded.provider.id))
        ).status_code == 403


#: What an "everything" fetch answers, in the shape the tag vocabulary is
#: read from. An entry the pricing parser would skip still says what slices
#: exist, and the counterparty's casing is kept — the live API matches it.
TAGGED_CATALOGUE = {
    "data": [
        {
            "id": "vendor/instruct-1",
            "tags": ["Instruct", "Tools"],
            "pricing": {"input_token": "1", "output_token": "2", "currency": "EUR"},
        },
        {
            "id": "vendor/embed-1",
            "tags": ["Embedding", "Tools"],
            "pricing": {"currency": "EUR", "input_token": 0.03, "output_token": 0.0},
            "output_modalities": ["embeddings"],
        },
        {
            "id": "vendor/ocr-1",
            "tags": ["OCR"],
            "pricing": {
                "currency": "EUR",
                "input_token": 0.0,
                "output_token": 0.0,
                "ocr_cost": 3.5904,
            },
            "input_modalities": ["file", "image"],
            "output_modalities": ["text"],
        },
        {
            "id": "vendor/tagless",
            "pricing": {"input_token": "1", "output_token": "2", "currency": "EUR"},
        },
    ]
}


@pytest.fixture
def catalogue_by_tag(app: object) -> None:
    """A counterparty that slices by tag, in Cortecs' measured manner: the
    lower-case sentinel answers with everything, an unknown tag with nothing."""

    def handler(request: httpx.Request) -> httpx.Response:
        tag = request.url.params.get("tag")
        if tag == "all":
            return httpx.Response(200, json=TAGGED_CATALOGUE)
        if tag is None:
            return httpx.Response(200, json=CATALOGUE)
        return httpx.Response(200, json={"data": []})

    app.state.control_http = httpx.AsyncClient(  # type: ignore[attr-defined]
        transport=httpx.MockTransport(handler)
    )


class TestTagVocabulary:
    """The discovery dialog's tag dropdown is fed from a live fetch.

    Never from a list compiled here: the vocabulary is the counterparty's, and
    whoever compiles one owns its staleness — free text was only ever the
    answer to "nobody here knows the words". What these tests pin is that the
    gateway reads the words from the counterparty's own "everything" answer,
    and admits rather than hides when its plugin knows no such fetch.
    """

    async def _cortecs_provider(self, client: httpx.AsyncClient) -> str:
        response = await client.post(
            "/api/admin/providers", json={"name": "cortecs-live", "plugin": "cortecs"}
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    async def test_the_vocabulary_is_read_from_an_everything_fetch(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue_by_tag: None,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        provider_id = await self._cortecs_provider(client)
        body = (await client.get("/api/admin/models/tags?provider_id=" + provider_id)).json()

        assert body["supported"] is True
        # The answer says where it came from, and the fetch named the sentinel
        # the plugin measured — not a spelling a reviewer "corrected".
        assert body["provider_url"] == "https://api.cortecs.ai/v1/models?tag=all"
        # Sorted, deduplicated across entries, casing kept: these strings go
        # back as `tag=…`, and the live API matches `all` while returning
        # nothing for `All`.
        assert body["tags"] == ["Embedding", "Instruct", "OCR", "Tools"]

    async def test_a_provider_without_a_measured_sentinel_is_unsupported(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The generic plugin names no "everything" spelling — so unsupported,
        not a fetch. Guessing a sentinel against a live API is exactly how
        `tag=All` returns an empty catalogue: an unmeasured vocabulary would
        be a dropdown of one option presented as the whole story. (No catalogue
        fixture here: reaching the network at all fails this test.)"""
        as_user(app, await make_admin(session_factory, seeded))
        body = (
            await client.get("/api/admin/models/tags?provider_id=" + str(seeded.provider.id))
        ).json()

        assert body == {
            "provider_url": f"{seeded.provider.base_url}/models",
            "supported": False,
            "tags": [],
        }

    async def test_an_unreachable_provider_is_502_not_500(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        provider_id = await self._cortecs_provider(client)

        def broken(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        app.state.control_http = httpx.AsyncClient(  # type: ignore[attr-defined]
            transport=httpx.MockTransport(broken)
        )
        response = await client.get("/api/admin/models/tags?provider_id=" + provider_id)
        assert response.status_code == 502


class TestDiscoveryAcrossTags:
    """`tag` scopes `available`, never the drift judgement.

    Cortecs slices its catalogue by tag; picking one to see what's importable
    used to also become the universe "missing_upstream" was checked against,
    so every model this gateway served under a different tag — an embedding
    or OCR model while `tag=Instruct` was selected — was reported withdrawn.
    It was never gone, only filed under a tag nobody asked for.
    """

    async def _cortecs_provider(self, client: httpx.AsyncClient) -> str:
        response = await client.post(
            "/api/admin/providers", json={"name": "cortecs-live", "plugin": "cortecs"}
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    async def test_a_served_model_under_another_tag_is_not_reported_missing(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue_by_tag: None,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        provider_id = await self._cortecs_provider(client)
        async with session_factory() as session:
            session.add(
                ModelDef(
                    name="our-embedder",
                    upstream_model="vendor/embed-1",
                    provider_id=uuid.UUID(provider_id),
                )
            )
            await session.commit()

        body = (
            await client.get(f"/api/admin/models/discover?provider_id={provider_id}&tag=Instruct")
        ).json()

        assert body["missing_upstream"] == []
        assert [m["name"] for m in body["catalogued"]] == ["our-embedder"]

    async def test_a_model_genuinely_absent_is_still_reported_missing(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue_by_tag: None,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The fix above must not cost the drift signal itself: a model
        dropped from the whole catalogue, not just the selected tag, is a
        real withdrawal and has to keep failing this check."""
        as_user(app, await make_admin(session_factory, seeded))
        provider_id = await self._cortecs_provider(client)
        async with session_factory() as session:
            session.add(
                ModelDef(
                    name="retired",
                    upstream_model="vendor/gone",
                    provider_id=uuid.UUID(provider_id),
                )
            )
            await session.commit()

        body = (
            await client.get(f"/api/admin/models/discover?provider_id={provider_id}&tag=Instruct")
        ).json()

        assert [m["name"] for m in body["missing_upstream"]] == ["retired"]

    async def test_available_stays_scoped_to_the_selected_tag(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue_by_tag: None,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The refetch above is for the drift judgement alone — what an
        operator can *import* stays whatever the selected tag actually
        offers, never the whole catalogue fetched to judge missing rows."""
        as_user(app, await make_admin(session_factory, seeded))
        provider_id = await self._cortecs_provider(client)

        body = (
            await client.get(f"/api/admin/models/discover?provider_id={provider_id}&tag=Instruct")
        ).json()

        # `catalogue_by_tag` answers an unmeasured tag like "Instruct" with an
        # empty catalogue (only its lower-case "everything" sentinel and no
        # tag at all return real data) — so nothing is importable under it,
        # even though the full catalogue behind the drift check is not empty.
        assert body["available"] == []
        assert body["provider_model_count"] == len(TAGGED_CATALOGUE["data"])


class TestImport:
    async def test_imports_with_the_published_price(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue: None,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/models/import?provider_id=" + str(seeded.provider.id),
            json={"models": [{"upstream_model": "vendor/new-small"}]},
        )
        assert response.status_code == 201, response.text
        result = response.json()["results"][0]
        assert result == {
            "upstream_model": "vendor/new-small",
            "name": "new-small",
            "imported": True,
            "priced": True,
            "reason": None,
            # The provider published this one, and the row that stores it says so.
            "price_source": "provider",
        }

        async with session_factory() as session:
            model = (
                await session.execute(select(ModelDef).where(ModelDef.name == "new-small"))
            ).scalar_one()
            price = (
                await session.execute(select(ModelPrice).where(ModelPrice.model_id == model.id))
            ).scalar_one()
        assert model.context_window == 32000
        assert str(price.input_per_mtok) == "0.100000000000"

    async def test_import_grants_no_access(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue: None,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Absence of a grant means no access, and import must not quietly weaken it."""
        as_user(app, await make_admin(session_factory, seeded))
        await client.post(
            "/api/admin/models/import?provider_id=" + str(seeded.provider.id),
            json={"models": [{"upstream_model": "vendor/new-large"}]},
        )
        async with session_factory() as session:
            model = (
                await session.execute(select(ModelDef).where(ModelDef.name == "new-large"))
            ).scalar_one()
            grants = (
                (
                    await session.execute(
                        select(GroupModelAccess).where(GroupModelAccess.model_id == model.id)
                    )
                )
                .scalars()
                .all()
            )
        assert grants == []
        # And it is therefore invisible to a caller.
        visible = (await client.get("/v1/models", headers=seeded.auth)).json()["data"]
        assert "new-large" not in {m["id"] for m in visible}

    async def test_a_foreign_currency_imports_with_its_own_prices(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue: None,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """ADR 0054: the USD prices come along as they are published. The
        conversion happens at admission, so an unpriced-model import — the
        failure this test used to pin — is no longer the outcome; the model
        is priced and the daily rate decides the rest."""
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/models/import?provider_id=" + str(seeded.provider.id),
            json={"models": [{"upstream_model": "vendor/dollar-model"}]},
        )
        result = response.json()["results"][0]
        assert result["imported"] is True
        assert result["priced"] is True

        async with session_factory() as session:
            found = (
                await session.execute(
                    select(ModelDef).where(ModelDef.upstream_model == "vendor/dollar-model")
                )
            ).scalar_one_or_none()
        assert found is not None
        price = (
            await session.execute(select(ModelPrice).where(ModelPrice.model_id == found.id))
        ).scalar_one()
        assert price.currency == "USD"

    async def test_reimport_is_reported_not_duplicated(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue: None,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/models/import?provider_id=" + str(seeded.provider.id),
            json={"models": [{"upstream_model": "upstream/test-model"}]},
        )
        result = response.json()["results"][0]
        assert result["imported"] is False
        assert result["reason"] == "already in the catalogue"

    async def test_a_name_collision_is_reported_per_item(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue: None,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/models/import?provider_id=" + str(seeded.provider.id),
            json={"models": [{"upstream_model": "vendor/new-small", "name": "test-model"}]},
        )
        result = response.json()["results"][0]
        assert result["imported"] is False
        assert "already taken" in result["reason"]

    async def test_a_model_the_provider_does_not_offer(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue: None,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/models/import?provider_id=" + str(seeded.provider.id),
            json={"models": [{"upstream_model": "vendor/imaginary"}]},
        )
        result = response.json()["results"][0]
        assert result["imported"] is False
        assert result["reason"] == "not offered by the provider"

    async def test_a_partial_batch_reports_each_outcome(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue: None,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """One bad entry must not lose the good ones."""
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/models/import?provider_id=" + str(seeded.provider.id),
            json={
                "models": [
                    {"upstream_model": "vendor/new-small"},
                    {"upstream_model": "vendor/dollar-model"},
                    {"upstream_model": "vendor/new-large"},
                ]
            },
        )
        results = {r["upstream_model"]: r["imported"] for r in response.json()["results"]}
        # The dollar model imports too now (ADR 0054): its USD prices come
        # along, and conversion happens at admission.
        assert results == {
            "vendor/new-small": True,
            "vendor/dollar-model": True,
            "vendor/new-large": True,
        }

    async def test_an_empty_batch_is_rejected(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        catalogue: None,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        assert (
            await client.post(
                "/api/admin/models/import?provider_id=" + str(seeded.provider.id),
                json={"models": []},
            )
        ).status_code == 400


class TestSearchBackendAnchor:
    """A search backend's grant anchor, created with the provider.

    The passthrough (ADR 0071) grants per backend, and the anchor — one model
    row named after it — is what the grant hangs on and what /v1/search
    resolves. It exists the moment the provider does: there is nothing to
    discover and nothing to import, because a tier is a body field now.
    """

    @pytest.mark.asyncio
    async def test_creating_a_search_provider_creates_its_anchor(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/providers",
            json={"name": "acme-search", "plugin": "linkup", "kind": "search"},
        )
        assert response.status_code == 201, response.text

        async with session_factory() as session:
            anchor = (
                await session.execute(select(ModelDef).where(ModelDef.name == "acme-search"))
            ).scalar_one()
            assert anchor.kind == ModelKind.SEARCH
            assert anchor.upstream_model == "search"

    @pytest.mark.asyncio
    async def test_a_colliding_model_name_is_reported_not_500(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        # A model already holds the name the anchor would take.
        await client.post(
            "/api/admin/models",
            json={
                "name": "clash",
                "upstream_model": "x",
                "provider_id": str(seeded.provider.id),
            },
        )
        response = await client.post(
            "/api/admin/providers",
            json={"name": "clash", "plugin": "linkup", "kind": "search"},
        )
        assert response.status_code == 409, response.text
