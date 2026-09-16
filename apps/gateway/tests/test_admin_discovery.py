"""Catalogue discovery and import.

The catalogue is an allowlist an administrator curates. These endpoints remove the
tedium of finding out what a provider offers without moving the decision away from a
person — so most of what is asserted here is what the endpoints refuse to do.
"""

from __future__ import annotations

import httpx
import pytest
from conftest import Seeded
from gateway.models import (
    GroupModelAccess,
    ModelDef,
    ModelKind,
    ModelPrice,
    PriceSource,
)
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
            await session.execute(
                select(ModelPrice).where(ModelPrice.model_id == found.id)
            )
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
                await session.execute(
                    select(ModelDef).where(ModelDef.name == "acme-search")
                )
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
