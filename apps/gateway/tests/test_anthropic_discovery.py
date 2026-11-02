"""Model discovery and prices for an Anthropic provider.

The listing below is the shape ``GET /v1/models`` answered with on 2026-11-02
(capabilities included), cut to what matters. Prices come from the built-in
catalogue entry, since Anthropic publishes no price API.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, ClassVar

import httpx
import pytest
from conftest import Seeded
from gateway.catalogue import BUILTIN_DIR, load_catalogue
from gateway.models import ModelDef, ModelPrice, PriceSource, Provider
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin


def cap(**supported: bool) -> dict[str, Any]:
    return {name: {"supported": value} for name, value in supported.items()}


def model(model_id: str, name: str, *, context: int = 1_000_000, out: int = 128_000) -> dict:
    return {
        "type": "model",
        "id": model_id,
        "display_name": name,
        "created_at": "2026-10-07T18:00:00Z",
        "line": "opus",
        "max_input_tokens": context,
        "max_tokens": out,
        "lifecycle": "active",
        "capabilities": {
            **cap(image_input=True, pdf_input=True, structured_outputs=True),
            "thinking": {"supported": True, "types": cap(adaptive=True, enabled=False)},
        },
    }


PAGE_1 = {
    "data": [
        model("claude-opus-5-5", "Claude Opus 5.5"),
        model("claude-haiku-5-5", "Claude Haiku 5.5"),
    ],
    "has_more": True,
    "last_id": "claude-haiku-5-5",
}
PAGE_2 = {
    "data": [
        model("claude-sonnet-4-5-20250929", "Claude Sonnet 4.5", context=200_000, out=64_000),
        model("claude-brand-new-9", "Claude Brand New 9"),
    ],
    "has_more": False,
    "last_id": "claude-brand-new-9",
}


@pytest.fixture
async def anthropic_provider(
    app: Any,
    seeded: Seeded,
    session_factory: async_sessionmaker[AsyncSession],
) -> list[httpx.Request]:
    async with session_factory() as db:
        await db.execute(
            update(Provider).where(Provider.id == seeded.provider.id).values(plugin="anthropic")
        )
        await db.commit()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        after = request.url.params.get("after_id")
        return httpx.Response(200, json=PAGE_2 if after else PAGE_1)

    app.state.control_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return seen


class TestDiscovery:
    async def test_every_page_is_read_with_x_api_key_and_the_version_header(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        anthropic_provider: list[httpx.Request],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.get(f"/api/admin/models/discover?provider_id={seeded.provider.id}")
        assert response.status_code == 200, response.text
        assert len(anthropic_provider) == 2
        assert anthropic_provider[1].url.params["after_id"] == "claude-haiku-5-5"
        for request in anthropic_provider:
            assert request.headers["x-api-key"] == "upstream-key"
            assert request.headers["anthropic-version"] == "2023-06-01"
            assert "authorization" not in request.headers
        ids = {row["upstream_model"] for row in response.json()["available"]}
        assert ids == {
            "claude-opus-5-5",
            "claude-haiku-5-5",
            "claude-sonnet-4-5-20250929",
            "claude-brand-new-9",
        }

    async def test_prices_limits_and_capabilities_arrive_with_each_model(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        anthropic_provider: list[httpx.Request],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        rows = {
            row["upstream_model"]: row
            for row in (
                await client.get(f"/api/admin/models/discover?provider_id={seeded.provider.id}")
            ).json()["available"]
        }
        opus = rows["claude-opus-5-5"]
        assert (opus["input_per_mtok"], opus["output_per_mtok"]) == ("4", "20")
        assert opus["currency"] == "USD"
        assert opus["price_source"] == "hint"
        assert (opus["context_window"], opus["max_output_tokens"]) == (1_000_000, 128_000)
        assert opus["kind"] == "chat"
        assert {"text", "image", "file"} <= set(opus["input_modalities"])
        assert {"tools", "reasoning", "json_mode"} <= set(opus["supported_features"])
        assert rows["claude-sonnet-4-5-20250929"]["context_window"] == 200_000

    async def test_a_model_with_no_price_in_the_table_is_offered_unpriced_never_free(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        anthropic_provider: list[httpx.Request],
        session_factory: async_sessionmaker[AsyncSession],
        session: AsyncSession,
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        rows = {
            row["upstream_model"]: row
            for row in (
                await client.get(f"/api/admin/models/discover?provider_id={seeded.provider.id}")
            ).json()["available"]
        }
        new = rows["claude-brand-new-9"]
        assert new["input_per_mtok"] is None
        assert new["price_source"] is None
        assert new["blocked_reason"]

        response = await client.post(
            f"/api/admin/models/import?provider_id={seeded.provider.id}",
            json={"models": [{"upstream_model": "claude-brand-new-9"}]},
        )
        result = response.json()["results"][0]
        assert result["imported"] is False
        assert (
            await session.execute(
                select(ModelDef).where(ModelDef.upstream_model == "claude-brand-new-9")
            )
        ).first() is None

    async def test_import_writes_the_cache_prices_and_the_capabilities(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        anthropic_provider: list[httpx.Request],
        session_factory: async_sessionmaker[AsyncSession],
        session: AsyncSession,
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            f"/api/admin/models/import?provider_id={seeded.provider.id}",
            json={"models": [{"upstream_model": "claude-opus-5-5", "name": "opus"}]},
        )
        assert response.status_code == 201, response.text
        assert response.json()["results"][0]["imported"] is True
        imported = (
            await session.execute(select(ModelDef).where(ModelDef.name == "opus"))
        ).scalar_one()
        assert imported.context_window == 1_000_000
        assert imported.max_output_tokens == 128_000
        assert "image" in imported.input_modalities
        price = (
            await session.execute(select(ModelPrice).where(ModelPrice.model_id == imported.id))
        ).scalar_one()
        assert (price.input_per_mtok, price.output_per_mtok) == (Decimal(4), Decimal(20))
        assert price.cache_read_per_mtok == Decimal("0.20")
        assert price.cache_write_per_mtok == Decimal(5)
        assert price.currency == "USD"
        assert price.source is PriceSource.HINT


class TestTheBuiltInPriceTable:
    """The table against the page it was read from (2026-11-02)."""

    ROWS: ClassVar[Any] = {
        # id: (input, 5m write, read, output)
        "claude-fable-5-1": ("10", "12.50", "0.25", "50"),
        "claude-opus-5-5": ("4", "5", "0.20", "20"),
        "claude-opus-5": ("5", "6.25", "0.50", "25"),
        "claude-opus-4-6": ("5", "6.25", "0.50", "25"),
        "claude-sonnet-5-5": ("2", "2.50", "0.10", "10"),
        "claude-sonnet-5": ("2", "2.50", "0.20", "10"),
        "claude-sonnet-4-6": ("3", "3.75", "0.30", "15"),
        "claude-haiku-5-5": ("0.50", "0.625", "0.05", "2.50"),
        "claude-haiku-4-5-20251001": ("1", "1.25", "0.10", "5"),
    }

    def entry(self) -> Any:
        catalogue = load_catalogue(builtin_dir=BUILTIN_DIR, override_dir=None)
        return catalogue.get("anthropic")

    @pytest.mark.parametrize("model_id", sorted(ROWS))
    def test_rates(self, model_id: str) -> None:
        from datetime import date

        hint = self.entry().price_hint_for(model_id, date(2026, 11, 2))
        want_input, want_write, want_read, want_output = (Decimal(x) for x in self.ROWS[model_id])
        assert (
            hint.input_per_mtok,
            hint.cache_write_per_mtok,
            hint.cache_read_per_mtok,
            hint.output_per_mtok,
        ) == (want_input, want_write, want_read, want_output)
        assert hint.currency == "USD"

    def test_every_write_rate_is_1_25x_input_and_reads_are_a_published_fraction(self) -> None:
        from datetime import date

        for hint in (self.entry().price_hint_for(m, date(2026, 11, 2)) for m in self.ROWS):
            assert hint.cache_write_per_mtok == hint.input_per_mtok * Decimal("1.25")
            assert hint.cache_read_per_mtok / hint.input_per_mtok in {
                Decimal("0.1"),
                Decimal("0.05"),
                Decimal("0.025"),
            }


class TestProviderType:
    """A provider's type is its API protocol: OpenAI-compatible or Anthropic."""

    def test_the_registry_says_which_protocol_each_plugin_speaks(self) -> None:
        from gateway.plugins import registry

        protocols = {entry["name"]: entry["protocol"] for entry in registry.describe()}
        assert protocols["anthropic"] == "anthropic"
        # every other plugin, router quirks included, is the OpenAI protocol
        assert {v for k, v in protocols.items() if k != "anthropic"} == {"openai"}
        assert protocols["cortecs"] == protocols["openrouter"] == "openai"

    async def test_the_listing_the_console_reads_carries_it(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        plugins = {p["name"]: p for p in (await client.get("/api/admin/provider-plugins")).json()}
        assert plugins["anthropic"]["protocol"] == "anthropic"
        assert plugins["generic"]["protocol"] == "openai"
