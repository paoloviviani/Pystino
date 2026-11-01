"""Image models priced from Cortecs's catalogue: per image plus per extra megapixel.

Fixtures are in Cortecs's own shape (``GET /v1/models?tag=Image-Gen`` and
``POST /v1/images/generations``, both checked live): token rates present and
zero, the real price in ``output_cost_per_image`` and, for one model,
``output_cost_per_additional_megapixel``.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
from conftest import FakeUpstream, Seeded
from gateway.accounting.cost import TokenCounts, compute_cost, image_rate, megapixels
from gateway.models import GroupModelAccess, ModelDef, ModelKind, ModelPrice
from gateway.pricing import parse_catalogue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin
from test_more_surfaces import latest_record


def _entry(model_id: str, **pricing: float) -> dict:
    return {
        "id": model_id,
        "object": "model",
        "tags": ["Image-Gen"],
        "input_modalities": ["text"],
        "output_modalities": ["image"],
        "context_size": 0,
        "max_output_tokens": 0,
        "pricing": {"currency": "EUR", "input_token": 0.0, "output_token": 0.0, **pricing},
    }


CORTECS_IMAGES = {
    "object": "list",
    "data": [
        _entry(
            "flux-2-klein-4b",
            output_cost_per_image=0.013,
            output_cost_per_additional_megapixel=0.001,
        ),
        _entry("z-image-turbo", output_cost_per_image=0.009),
        _entry("stable-diffusion-xl-base-1.0"),
    ],
}


class TestParse:
    def test_per_image_and_extra_megapixel_are_read(self) -> None:
        prices, _ = parse_catalogue(CORTECS_IMAGES)
        by_id = {p.model_id: p for p in prices}
        flux = by_id["flux-2-klein-4b"]
        assert flux.kind is ModelKind.IMAGE
        assert flux.per_image == Decimal("0.013")
        assert flux.per_extra_megapixel == Decimal("0.001")
        assert by_id["z-image-turbo"].per_image == Decimal("0.009")
        assert by_id["z-image-turbo"].per_extra_megapixel is None

    def test_an_image_model_with_no_price_is_unpriced_not_free(self) -> None:
        prices, unparsable = parse_catalogue(CORTECS_IMAGES)
        assert unparsable == ["stable-diffusion-xl-base-1.0"]
        assert "stable-diffusion-xl-base-1.0" not in {p.model_id for p in prices}

    def test_a_chat_model_never_takes_an_image_rate(self) -> None:
        entry = {
            "id": "chatty",
            "tags": ["Instruct"],
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "pricing": {
                "currency": "EUR",
                "input_token": 1.0,
                "output_token": 2.0,
                "output_cost_per_image": 5.0,
            },
        }
        (price,), _ = parse_catalogue({"data": [entry]})
        assert price.kind is ModelKind.CHAT
        assert price.per_image is None


def _price(**kwargs: object) -> ModelPrice:
    base: dict = {
        "input_per_mtok": Decimal(0),
        "output_per_mtok": Decimal(0),
        "per_image": Decimal("0.013"),
        "per_extra_megapixel": Decimal("0.001"),
        "currency": "EUR",
    }
    return ModelPrice(**{**base, **kwargs})


class TestMegapixelMath:
    def test_size_to_megapixels(self) -> None:
        assert megapixels("1536x1024") == Decimal("1.572864")
        assert megapixels("1024X1024") == Decimal("1.048576")
        assert megapixels("auto") is None
        assert megapixels(None) is None

    def test_at_or_below_one_megapixel_is_the_flat_rate(self) -> None:
        assert image_rate(_price(), "512x512") == Decimal("0.013")
        assert image_rate(_price(), "1000x1000") == Decimal("0.013")

    def test_larger_pays_for_each_megapixel_above_the_first(self) -> None:
        # 0.013 + (1.572864 - 1) * 0.001
        assert image_rate(_price(), "1536x1024") == Decimal("0.013572864")
        assert image_rate(_price(), "2048x2048") == Decimal("0.016194304")

    def test_a_listed_size_keeps_its_own_rate(self) -> None:
        price = _price(per_image_by_size={"1536x1024": "0.02"})
        assert image_rate(price, "1536x1024") == Decimal("0.02")
        assert image_rate(price, "2048x2048") == Decimal("0.016194304")

    def test_the_providers_measured_megapixels_outrank_the_size(self) -> None:
        assert image_rate(_price(), "2048x2048", Decimal(1)) == Decimal("0.013")
        assert image_rate(_price(), "1024x1024", Decimal(3)) == Decimal("0.015")

    def test_without_the_extra_rate_size_changes_nothing(self) -> None:
        price = _price(per_extra_megapixel=None)
        assert image_rate(price, "2048x2048") == Decimal("0.013")

    def test_an_unknown_size_takes_the_flat_rate(self) -> None:
        assert image_rate(_price(), None) == Decimal("0.013")
        assert image_rate(_price(), "auto") == Decimal("0.013")

    def test_cost_multiplies_by_the_image_count(self) -> None:
        cost = compute_cost(TokenCounts(images=2, image_size="1536x1024"), _price())
        assert cost.image_cost == Decimal("0.027145728")


class TestRoute:
    async def _flux(self, session: AsyncSession, seeded: Seeded) -> ModelDef:
        model = ModelDef(
            name="flux-2-klein-4b",
            upstream_model="flux-2-klein-4b",
            provider_id=seeded.provider.id,
            kind=ModelKind.IMAGE,
        )
        session.add(model)
        await session.flush()
        session.add(
            ModelPrice(
                model_id=model.id,
                input_per_mtok=Decimal(0),
                output_per_mtok=Decimal(0),
                per_image=Decimal("0.013"),
                per_extra_megapixel=Decimal("0.001"),
                currency="EUR",
            )
        )
        session.add(GroupModelAccess(group_id=seeded.group.id, model_id=model.id))
        await session.commit()
        return model

    @pytest.mark.parametrize(
        ("reply_usage", "expected"),
        [
            # Cortecs's real reply: it echoes the requested size but measured
            # one megapixel, and bills that.
            ({"out_images_mpx": 1}, Decimal("0.013")),
            (None, Decimal("0.013572864")),
        ],
    )
    async def test_the_ledger_follows_the_measured_megapixels(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session: AsyncSession,
        reply_usage: dict | None,
        expected: Decimal,
    ) -> None:
        model = await self._flux(session, seeded)
        reply: dict = {
            "created": 1,
            "size": "1536x1024",
            "data": [{"b64_json": "aaa", "url": None, "revised_prompt": None}],
        }
        if reply_usage:
            reply["usage"] = {"input_tokens": 0, "output_tokens": 0, **reply_usage}
        fake_upstream.set_json(reply)
        response = await client.post(
            "/v1/images/generations",
            json={"model": model.name, "prompt": "a boat", "size": "1536x1024"},
            headers=seeded.auth,
        )
        assert response.status_code == 200, response.text
        record = await latest_record(session)
        assert record.image_size == "1536x1024"
        assert record.cost == expected

    async def test_a_url_result_is_passed_through_untouched(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session: AsyncSession,
    ) -> None:
        model = await self._flux(session, seeded)
        fake_upstream.set_json(
            {"created": 1, "data": [{"url": "https://cdn.example/x.png", "b64_json": None}]}
        )
        response = await client.post(
            "/v1/images/generations",
            json={"model": model.name, "prompt": "a boat", "response_format": "url"},
            headers=seeded.auth,
        )
        assert response.status_code == 200
        assert response.json()["data"][0]["url"] == "https://cdn.example/x.png"
        # The gateway forwarded the caller's format and fetched nothing.
        assert fake_upstream.last_body["response_format"] == "url"
        assert (await latest_record(session)).image_count == 1


class TestImport:
    async def test_discover_and_import_carry_the_image_prices(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        session: AsyncSession,
    ) -> None:
        app.state.control_http = httpx.AsyncClient(  # type: ignore[attr-defined]
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=CORTECS_IMAGES))
        )
        as_user(app, await make_admin(session_factory, seeded))
        base = "provider_id=" + str(seeded.provider.id)
        found = (await client.get(f"/api/admin/models/discover?{base}&tag=Image-Gen")).json()
        rows = {row["upstream_model"]: row for row in found["available"]}
        assert rows["flux-2-klein-4b"]["per_image"] == "0.013"
        assert rows["flux-2-klein-4b"]["per_extra_megapixel"] == "0.001"
        assert rows["flux-2-klein-4b"]["kind"] == "image"

        imported = await client.post(
            f"/api/admin/models/import?{base}&tag=Image-Gen",
            json={"models": [{"upstream_model": "flux-2-klein-4b"}]},
        )
        assert imported.status_code == 201, imported.text
        assert imported.json()["results"][0]["priced"] is True
        model = (
            await session.execute(select(ModelDef).where(ModelDef.name == "flux-2-klein-4b"))
        ).scalar_one()
        assert model.kind is ModelKind.IMAGE
        price = (
            await session.execute(select(ModelPrice).where(ModelPrice.model_id == model.id))
        ).scalar_one()
        assert price.per_image == Decimal("0.013")
        assert price.per_extra_megapixel == Decimal("0.001")

        # The price API shows the field.
        shown = (await client.get(f"/api/admin/models/{model.id}")).json()
        assert shown["current_price"]["per_extra_megapixel"] is not None

    async def test_an_image_model_can_be_priced_per_image_by_hand(self) -> None:
        from gateway.config import Settings
        from gateway.routers.admin import _manual_catalogue_price
        from gateway.schemas import ManualImportPrice

        price, reason = await _manual_catalogue_price(
            ManualImportPrice(per_image=Decimal("0.02"), per_extra_megapixel=Decimal("0.001")),
            ModelKind.IMAGE,
            Settings(),
            None,
        )
        assert reason is None and price is not None
        assert price.per_image == Decimal("0.02")
        assert price.per_extra_megapixel == Decimal("0.001")
