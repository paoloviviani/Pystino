"""The admin surface for aliases (ADR 0101): the Routing API, and the model
endpoints staying about served models.

Two things are asserted here that are easy to believe work and easy to get
wrong: that the save path refuses the states the database cannot (one target,
no chains, no crossing kinds), and that every existing model endpoint is
unambiguous about an alias id rather than half-working on one.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from conftest import Seeded
from gateway.models import ModelDef, ModelKind, ModelRouteTarget
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin


@pytest.fixture
async def admin(
    app: object,
    client: httpx.AsyncClient,
    seeded: Seeded,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    as_user(app, await make_admin(session_factory, seeded))


async def target_id(session_factory: async_sessionmaker[AsyncSession]) -> str:
    async with session_factory() as session:
        model = (
            await session.execute(select(ModelDef).where(ModelDef.name == "test-model"))
        ).scalar_one()
        return str(model.id)


async def make_embedder(session_factory: async_sessionmaker[AsyncSession], seeded: Seeded) -> str:
    async with session_factory() as session:
        embedder = ModelDef(
            name="embedder",
            upstream_model="upstream/embedder",
            provider_id=seeded.provider.id,
            kind=ModelKind.EMBEDDING,
            context_window=8192,
        )
        session.add(embedder)
        await session.commit()
        return str(embedder.id)


# --------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------


class TestAliasCrud:
    async def test_create_read_update_delete(
        self,
        admin: None,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        target = await target_id(session_factory)
        created = await client.post(
            "/api/admin/aliases",
            json={
                "name": "fast",
                "display_name": "Fast",
                "description": "the fast one",
                "target_model_id": target,
                "is_public": True,
            },
        )
        assert created.status_code == 201, created.text
        alias = created.json()
        assert alias["name"] == "fast"
        assert alias["target"] == {
            "id": target,
            "name": "test-model",
            "kind": "chat",
            "is_active": True,
        }
        assert alias["status"] == "available"
        assert alias["unavailable_reason"] is None
        # The kind is the target's, set on save.
        assert alias["kind"] == "chat"

        listed = await client.get("/api/admin/aliases")
        assert listed.status_code == 200
        assert [row["name"] for row in listed.json()] == ["fast"]

        renamed = await client.patch(
            f"/api/admin/aliases/{alias['id']}", json={"name": "quick", "display_name": "Quick"}
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["name"] == "quick"
        assert renamed.json()["display_name"] == "Quick"

        assert (await client.delete(f"/api/admin/aliases/{alias['id']}")).status_code == 204
        assert (await client.get("/api/admin/aliases")).json() == []

    async def test_the_alias_row_carries_no_provider_or_upstream_model(
        self,
        admin: None,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The shape the whole design rests on — asserted on the stored row, not
        on the response, because the response could hide it and the database
        still has to agree."""
        target = await target_id(session_factory)
        alias = (
            await client.post(
                "/api/admin/aliases", json={"name": "fast", "target_model_id": target}
            )
        ).json()
        async with session_factory() as session:
            row = await session.get(ModelDef, uuid.UUID(alias["id"]))
            assert row is not None
            assert row.is_alias is True
            assert row.provider_id is None
            assert row.upstream_model is None
            assert row.kind == ModelKind.CHAT
            routes = (
                (
                    await session.execute(
                        select(ModelRouteTarget).where(ModelRouteTarget.alias_id == row.id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(routes) == 1
            assert routes[0].position == 0

    async def test_a_retarget_leaves_exactly_one_route_row(
        self,
        admin: None,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The rule the table cannot express. A second row would make the
        answer to "what serves this name?" depend on a position column this
        step never uses."""
        target = await target_id(session_factory)
        async with session_factory() as session:
            other = ModelDef(
                name="second-model",
                upstream_model="upstream/second-model",
                provider_id=seeded.provider.id,
                kind=ModelKind.CHAT,
                context_window=8192,
            )
            session.add(other)
            await session.commit()
            other_id = str(other.id)

        alias = (
            await client.post(
                "/api/admin/aliases", json={"name": "fast", "target_model_id": target}
            )
        ).json()
        patched = await client.patch(
            f"/api/admin/aliases/{alias['id']}", json={"target_model_id": other_id}
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["target"]["name"] == "second-model"

        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ModelRouteTarget).where(
                            ModelRouteTarget.alias_id == uuid.UUID(alias["id"])
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) == 1
            assert str(rows[0].target_model_id) == other_id

    async def test_toggling_active_and_public(
        self,
        admin: None,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        target = await target_id(session_factory)
        alias = (
            await client.post(
                "/api/admin/aliases", json={"name": "fast", "target_model_id": target}
            )
        ).json()
        assert alias["is_active"] is True
        assert alias["is_public"] is False
        off = await client.patch(
            f"/api/admin/aliases/{alias['id']}", json={"is_active": False, "is_public": True}
        )
        assert off.status_code == 200
        assert off.json()["is_active"] is False
        assert off.json()["is_public"] is True


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


class TestValidation:
    async def test_a_name_clashing_with_a_model_is_a_clean_4xx(
        self,
        admin: None,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        target = await target_id(session_factory)
        response = await client.post(
            "/api/admin/aliases", json={"name": "test-model", "target_model_id": target}
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "already_exists"

    async def test_renaming_onto_an_existing_name_is_a_clean_4xx(
        self,
        admin: None,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        target = await target_id(session_factory)
        alias = (
            await client.post(
                "/api/admin/aliases", json={"name": "fast", "target_model_id": target}
            )
        ).json()
        response = await client.patch(
            f"/api/admin/aliases/{alias['id']}", json={"name": "test-model"}
        )
        assert response.status_code == 409, response.text

    async def test_an_unknown_target_is_a_400(
        self,
        admin: None,
        client: httpx.AsyncClient,
    ) -> None:
        response = await client.post(
            "/api/admin/aliases",
            json={"name": "fast", "target_model_id": str(uuid.uuid4())},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "unknown_target"

    async def test_a_chain_is_refused_on_create(
        self,
        admin: None,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        target = await target_id(session_factory)
        inner = (
            await client.post(
                "/api/admin/aliases", json={"name": "inner", "target_model_id": target}
            )
        ).json()
        response = await client.post(
            "/api/admin/aliases", json={"name": "outer", "target_model_id": inner["id"]}
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "alias_target_is_alias"
        assert "inner" in response.json()["error"]["message"]

    async def test_a_chain_is_refused_on_retarget(
        self,
        admin: None,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        target = await target_id(session_factory)
        inner = (
            await client.post(
                "/api/admin/aliases", json={"name": "inner", "target_model_id": target}
            )
        ).json()
        outer = (
            await client.post(
                "/api/admin/aliases", json={"name": "outer", "target_model_id": target}
            )
        ).json()
        response = await client.patch(
            f"/api/admin/aliases/{outer['id']}", json={"target_model_id": inner["id"]}
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "alias_target_is_alias"

    async def test_an_inactive_target_is_refused(
        self,
        admin: None,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        target = await target_id(session_factory)
        async with session_factory() as session:
            model = (
                await session.execute(select(ModelDef).where(ModelDef.name == "test-model"))
            ).scalar_one()
            model.is_active = False
            await session.commit()
        response = await client.post(
            "/api/admin/aliases", json={"name": "fast", "target_model_id": target}
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "alias_target_inactive"

    async def test_an_inactive_target_provider_is_refused(
        self,
        admin: None,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        target = await target_id(session_factory)
        async with session_factory() as session:
            provider = await session.get(type(seeded.provider), seeded.provider.id)
            provider.is_active = False  # type: ignore[union-attr]
            await session.commit()
        response = await client.post(
            "/api/admin/aliases", json={"name": "fast", "target_model_id": target}
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "alias_target_provider_inactive"

    async def test_retargeting_across_kinds_is_refused(
        self,
        admin: None,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Interchangeability starts now (ADR 0101, "Designed for routing")."""
        target = await target_id(session_factory)
        embedder = await make_embedder(session_factory, seeded)
        alias = (
            await client.post(
                "/api/admin/aliases", json={"name": "fast", "target_model_id": target}
            )
        ).json()
        response = await client.patch(
            f"/api/admin/aliases/{alias['id']}", json={"target_model_id": embedder}
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "alias_kind_mismatch"
        # And the alias was not moved.
        still = (await client.get("/api/admin/aliases")).json()
        assert still[0]["target"]["id"] == target

    async def test_a_model_id_on_the_alias_routes_is_a_404(
        self,
        admin: None,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        target = await target_id(session_factory)
        assert (
            await client.patch(f"/api/admin/aliases/{target}", json={"is_active": False})
        ).status_code == 404
        assert (await client.delete(f"/api/admin/aliases/{target}")).status_code == 404


# --------------------------------------------------------------------------
# the model screens stay about served models (ADR 0101, decision 8)
# --------------------------------------------------------------------------


class TestTheModelEndpointsIgnoreAliases:
    @pytest.fixture
    async def alias(self, session_factory: async_sessionmaker[AsyncSession]) -> str:
        async with session_factory() as session:
            model = (
                await session.execute(select(ModelDef).where(ModelDef.name == "test-model"))
            ).scalar_one()
            alias = ModelDef(
                name="fast",
                provider_id=None,
                upstream_model=None,
                is_alias=True,
                kind=model.kind,
                is_active=True,
                is_public=True,
            )
            session.add(alias)
            await session.flush()
            session.add(ModelRouteTarget(alias_id=alias.id, target_model_id=model.id, position=0))
            await session.commit()
            return str(alias.id)

    async def test_the_models_list_excludes_aliases(
        self,
        admin: None,
        client: httpx.AsyncClient,
        alias: str,
    ) -> None:
        listed = await client.get("/api/admin/models")
        assert listed.status_code == 200
        assert "fast" not in {row["name"] for row in listed.json()["items"]}

    async def test_the_model_detail_lists_aliases_pointing_here(
        self,
        admin: None,
        client: httpx.AsyncClient,
        alias: str,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        target = await target_id(session_factory)
        detail = await client.get(f"/api/admin/models/{target}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["aliases_pointing_here"] == [
            {"id": alias, "name": "fast", "is_active": True}
        ]

    async def test_the_listing_does_not_carry_aliases_pointing_here(
        self,
        admin: None,
        client: httpx.AsyncClient,
        alias: str,
    ) -> None:
        """One query per row is not a thing to spend on a page of forty."""
        listed = await client.get("/api/admin/models")
        assert all(row["aliases_pointing_here"] == [] for row in listed.json()["items"])

    async def test_the_detail_of_an_alias_id_is_a_clear_400(
        self,
        admin: None,
        client: httpx.AsyncClient,
        alias: str,
    ) -> None:
        response = await client.get(f"/api/admin/models/{alias}")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "model_is_alias"
        assert "Routing" in response.json()["error"]["message"]

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("patch", "/api/admin/models/{alias}"),
            ("delete", "/api/admin/models/{alias}"),
            ("get", "/api/admin/models/{alias}/prices"),
        ],
    )
    async def test_no_model_endpoint_half_works_on_an_alias(
        self,
        admin: None,
        client: httpx.AsyncClient,
        alias: str,
        method: str,
        path: str,
    ) -> None:
        url = path.format(alias=alias)
        if method == "patch":
            response = await client.patch(url, json={"is_active": False})
        else:
            response = await getattr(client, method)(url)
        assert response.status_code == 400, f"{method} {url}: {response.text}"
        assert response.json()["error"]["code"] == "model_is_alias"

    async def test_posting_a_price_for_an_alias_id_is_refused(
        self,
        admin: None,
        client: httpx.AsyncClient,
        alias: str,
    ) -> None:
        response = await client.post(
            f"/api/admin/models/{alias}/prices",
            json={"input_per_mtok": "1", "output_per_mtok": "2", "currency": "EUR"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "model_is_alias"

    async def test_deleting_a_model_leaves_its_alias_unavailable_not_deleted(
        self,
        admin: None,
        client: httpx.AsyncClient,
        alias: str,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The cascade is the mechanism, and the alias survives it on purpose:
        it still holds grants and a name callers are sending, so the useful
        state is "unavailable, fix me" rather than "gone"."""
        target = await target_id(session_factory)
        assert (await client.delete(f"/api/admin/models/{target}")).status_code == 204

        rows = (await client.get("/api/admin/aliases")).json()
        assert len(rows) == 1
        assert rows[0]["name"] == "fast"
        assert rows[0]["target"] is None
        assert rows[0]["status"] == "unavailable"
        assert rows[0]["unavailable_reason"] == "target_deleted"


# --------------------------------------------------------------------------
# availability is reported, with its reason
# --------------------------------------------------------------------------


class TestReportedAvailability:
    async def _alias_row(
        self, client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
    ) -> dict:
        target = await target_id(session_factory)
        return (
            await client.post(
                "/api/admin/aliases", json={"name": "fast", "target_model_id": target}
            )
        ).json()

    async def test_an_inactive_target_reads_as_target_inactive(
        self,
        admin: None,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        alias = await self._alias_row(client, session_factory)
        async with session_factory() as session:
            model = (
                await session.execute(select(ModelDef).where(ModelDef.name == "test-model"))
            ).scalar_one()
            model.is_active = False
            await session.commit()
        row = (await client.get("/api/admin/aliases")).json()[0]
        assert row["id"] == alias["id"]
        assert row["status"] == "unavailable"
        assert row["unavailable_reason"] == "target_inactive"

    async def test_an_inactive_target_provider_reads_as_such(
        self,
        admin: None,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await self._alias_row(client, session_factory)
        async with session_factory() as session:
            provider = await session.get(type(seeded.provider), seeded.provider.id)
            provider.is_active = False  # type: ignore[union-attr]
            await session.commit()
        row = (await client.get("/api/admin/aliases")).json()[0]
        assert row["unavailable_reason"] == "target_provider_inactive"

    async def test_visibility_is_reported_in_the_models_grant_shape(
        self,
        admin: None,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Same tables and the same labels as a model, so the console's
        visibility controls are reused rather than rebuilt."""
        alias = await self._alias_row(client, session_factory)
        async with session_factory() as session:
            from gateway.models import GroupModelAccess

            session.add(GroupModelAccess(group_id=seeded.group.id, model_id=uuid.UUID(alias["id"])))
            await session.commit()
        row = (await client.get("/api/admin/aliases")).json()[0]
        assert row["granted_to"] == ["research"]
        assert row["granted_to_users"] == []
