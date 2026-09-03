"""Catalogue discovery and import.

The catalogue is an allowlist an administrator curates. These endpoints remove the
tedium of finding out what a provider offers without moving the decision away from a
person — so most of what is asserted here is what the endpoints refuse to do.
"""

from __future__ import annotations

import httpx
import pytest
from conftest import Seeded
from gateway.models import GroupModelAccess, ModelDef, ModelPrice
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


@pytest.fixture
def catalogue(app: object) -> None:
    """Point the control-plane client at a canned provider catalogue."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=CATALOGUE)

    app.state.control_http = httpx.AsyncClient(  # type: ignore[attr-defined]
        transport=httpx.MockTransport(handler)
    )


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
