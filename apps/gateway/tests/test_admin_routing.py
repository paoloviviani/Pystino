"""The admin API for routed aliases (issue P1): several targets, a rule, stickiness.

The single-target spelling (`target_model_id`) keeps working and is covered in
`test_admin_aliases.py`; this is the route as a list.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from conftest import Seeded
from gateway.models import (
    AdminAuditEvent,
    ModelDef,
    ModelKind,
    ModelRouteTarget,
    RouteSession,
)
from gateway.types import utcnow
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


async def add_models(
    session_factory: async_sessionmaker[AsyncSession],
    seeded: Seeded,
    *names: str,
    kind: ModelKind = ModelKind.CHAT,
) -> list[str]:
    ids = []
    async with session_factory() as session:
        for name in names:
            model = ModelDef(
                name=name,
                upstream_model=f"up/{name}",
                provider_id=seeded.provider.id,
                kind=kind,
            )
            session.add(model)
            await session.flush()
            ids.append(str(model.id))
        await session.commit()
    return ids


async def create(client: httpx.AsyncClient, **body: Any) -> httpx.Response:
    return await client.post("/api/admin/aliases", json={"name": "routed", **body})


class TestCreate:
    async def test_a_route_with_order_weights_rule_and_ttl(
        self,
        admin: None,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        a, b, c = await add_models(session_factory, seeded, "m-a", "m-b", "m-c")
        response = await create(
            client,
            targets=[
                {"model_id": b, "weight": 3},
                {"model_id": a},
                {"model_id": c, "weight": 10},
            ],
            route_rule="weighted",
            route_sticky_ttl_seconds=600,
        )
        assert response.status_code == 201, response.text
        alias = response.json()
        assert [(t["name"], t["position"], t["weight"]) for t in alias["targets"]] == [
            ("m-b", 0, 3),
            ("m-a", 1, 1),
            ("m-c", 2, 10),
        ]
        assert alias["target"]["name"] == "m-b"
        assert alias["route_rule"] == "weighted"
        assert alias["route_sticky_ttl_seconds"] == 600
        assert alias["status"] == "available"

        listed = (await client.get("/api/admin/aliases")).json()
        assert [t["name"] for t in listed[0]["targets"]] == ["m-b", "m-a", "m-c"]

    @pytest.mark.parametrize(
        ("body", "code"),
        [
            ({"route_rule": "telepathy"}, "unknown_route_rule"),
        ],
    )
    async def test_an_unknown_rule_is_a_400(
        self,
        body: dict[str, Any],
        code: str,
        admin: None,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        (a,) = await add_models(session_factory, seeded, "m-a")
        response = await create(client, targets=[{"model_id": a}], **body)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == code

    async def test_the_same_model_twice_is_a_400(
        self,
        admin: None,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        (a,) = await add_models(session_factory, seeded, "m-a")
        response = await create(client, targets=[{"model_id": a}, {"model_id": a}])
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "alias_duplicate_target"

    async def test_targets_of_different_kinds_are_refused(
        self,
        admin: None,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        (chat_id,) = await add_models(session_factory, seeded, "m-chat")
        (embed_id,) = await add_models(session_factory, seeded, "m-embed", kind=ModelKind.EMBEDDING)
        response = await create(client, targets=[{"model_id": chat_id}, {"model_id": embed_id}])
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "alias_kind_mismatch"

    async def test_an_alias_cannot_be_a_target(
        self,
        admin: None,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        (a,) = await add_models(session_factory, seeded, "m-a")
        first = (await create(client, name="inner", targets=[{"model_id": a}])).json()
        response = await create(client, targets=[{"model_id": a}, {"model_id": first["id"]}])
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "alias_target_is_alias"

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"targets": []},
            {"targets": [{"model_id": str(uuid.uuid4()), "weight": 0}]},
            {"targets": [{"model_id": str(uuid.uuid4()), "weight": 1001}]},
            {"targets": [{"model_id": str(uuid.uuid4())}], "target_model_id": str(uuid.uuid4())},
            {"targets": [{"model_id": str(uuid.uuid4())}], "route_sticky_ttl_seconds": -1},
            {"targets": [{"model_id": str(uuid.uuid4())}], "route_sticky_ttl_seconds": 99_999_999},
            {"targets": [{"model_id": str(uuid.uuid4())} for _ in range(17)]},
        ],
        ids=[
            "no targets at all",
            "empty list",
            "weight zero",
            "weight over the bound",
            "both spellings",
            "negative ttl",
            "ttl over the bound",
            "too many targets",
        ],
    )
    async def test_malformed_routes_are_a_400(
        self, body: dict[str, Any], admin: None, client: httpx.AsyncClient
    ) -> None:
        assert (await create(client, **body)).status_code == 400

    async def test_an_unknown_target_is_a_400_naming_it(
        self,
        admin: None,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        (a,) = await add_models(session_factory, seeded, "m-a")
        response = await create(client, targets=[{"model_id": a}, {"model_id": str(uuid.uuid4())}])
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "unknown_target"


class TestUpdate:
    async def _alias(
        self, client: httpx.AsyncClient, seeded: Seeded, factory: Any
    ) -> tuple[dict[str, Any], list[str]]:
        ids = await add_models(factory, seeded, "m-a", "m-b", "m-c")
        created = await create(client, targets=[{"model_id": ids[0]}, {"model_id": ids[1]}])
        assert created.status_code == 201, created.text
        return created.json(), ids

    async def test_targets_replace_the_whole_route_in_order(
        self,
        admin: None,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        alias, ids = await self._alias(client, seeded, session_factory)
        patched = await client.patch(
            f"/api/admin/aliases/{alias['id']}",
            json={"targets": [{"model_id": ids[2], "weight": 5}, {"model_id": ids[0]}]},
        )
        assert patched.status_code == 200, patched.text
        assert [(t["name"], t["position"], t["weight"]) for t in patched.json()["targets"]] == [
            ("m-c", 0, 5),
            ("m-a", 1, 1),
        ]
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
        assert len(rows) == 2

    async def test_rule_and_ttl_change_without_touching_the_route(
        self,
        admin: None,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        alias, _ = await self._alias(client, seeded, session_factory)
        patched = await client.patch(
            f"/api/admin/aliases/{alias['id']}",
            json={"route_rule": "cheapest", "route_sticky_ttl_seconds": 0},
        )
        assert patched.status_code == 200, patched.text
        body = patched.json()
        assert body["route_rule"] == "cheapest"
        assert body["route_sticky_ttl_seconds"] == 0
        assert [t["name"] for t in body["targets"]] == ["m-a", "m-b"]

    async def test_the_single_target_spelling_collapses_the_route_to_that_model(
        self,
        admin: None,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        alias, ids = await self._alias(client, seeded, session_factory)
        patched = await client.patch(
            f"/api/admin/aliases/{alias['id']}", json={"target_model_id": ids[2]}
        )
        assert patched.status_code == 200, patched.text
        assert [t["name"] for t in patched.json()["targets"]] == ["m-c"]

    async def test_both_spellings_at_once_are_refused(
        self,
        admin: None,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        alias, ids = await self._alias(client, seeded, session_factory)
        response = await client.patch(
            f"/api/admin/aliases/{alias['id']}",
            json={"target_model_id": ids[2], "targets": [{"model_id": ids[0]}]},
        )
        assert response.status_code == 400

    async def test_an_unknown_rule_is_refused_and_changes_nothing(
        self,
        admin: None,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        alias, _ = await self._alias(client, seeded, session_factory)
        response = await client.patch(
            f"/api/admin/aliases/{alias['id']}", json={"route_rule": "nonsense"}
        )
        assert response.status_code == 400
        after = (await client.get("/api/admin/aliases")).json()[0]
        assert after["route_rule"] == "first_available"

    async def test_a_cross_kind_route_is_refused_on_update(
        self,
        admin: None,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        alias, ids = await self._alias(client, seeded, session_factory)
        (embed,) = await add_models(session_factory, seeded, "m-embed", kind=ModelKind.EMBEDDING)
        response = await client.patch(
            f"/api/admin/aliases/{alias['id']}",
            json={"targets": [{"model_id": ids[0]}, {"model_id": embed}]},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "alias_kind_mismatch"

    async def test_the_audit_trail_names_the_route_and_the_policy(
        self,
        admin: None,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        alias, ids = await self._alias(client, seeded, session_factory)
        await client.patch(
            f"/api/admin/aliases/{alias['id']}",
            json={
                "targets": [{"model_id": ids[2]}, {"model_id": ids[0]}],
                "route_rule": "cheapest",
            },
        )
        async with session_factory() as session:
            events = (
                (
                    await session.execute(
                        select(AdminAuditEvent)
                        .where(AdminAuditEvent.action == "alias.update")
                        .order_by(AdminAuditEvent.created_at)
                    )
                )
                .scalars()
                .all()
            )
        (event,) = events
        assert event.changes["target"] == {"before": "m-a, m-b", "after": "m-c, m-a"}
        assert event.changes["route_rule"] == {"before": "first_available", "after": "cheapest"}


class TestStatus:
    async def test_one_target_down_is_still_available_and_says_which(
        self,
        admin: None,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ids = await add_models(session_factory, seeded, "m-a", "m-b")
        await create(client, targets=[{"model_id": ids[0]}, {"model_id": ids[1]}])
        async with session_factory() as session:
            a = await session.get(ModelDef, uuid.UUID(ids[0]))
            assert a is not None
            a.is_active = False
            await session.commit()
        alias = (await client.get("/api/admin/aliases")).json()[0]
        assert alias["status"] == "available"
        assert alias["unavailable_reason"] is None
        by_name = {t["name"]: t for t in alias["targets"]}
        assert by_name["m-a"]["available"] is False
        assert by_name["m-a"]["unavailable_reason"] == "target_inactive"
        assert by_name["m-b"]["available"] is True

    async def test_every_target_down_is_unavailable_with_the_first_reason(
        self,
        admin: None,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ids = await add_models(session_factory, seeded, "m-a", "m-b")
        await create(client, targets=[{"model_id": ids[0]}, {"model_id": ids[1]}])
        async with session_factory() as session:
            for model_id in ids:
                model = await session.get(ModelDef, uuid.UUID(model_id))
                assert model is not None
                model.is_active = False
            await session.commit()
        alias = (await client.get("/api/admin/aliases")).json()[0]
        assert alias["status"] == "unavailable"
        assert alias["unavailable_reason"] == "target_inactive"


class TestDelete:
    async def test_deleting_the_alias_forgets_its_sessions(
        self,
        admin: None,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        ids = await add_models(session_factory, seeded, "m-a", "m-b")
        alias = (await create(client, targets=[{"model_id": ids[0]}, {"model_id": ids[1]}])).json()
        async with session_factory() as session:
            session.add(
                RouteSession(
                    alias_id=uuid.UUID(alias["id"]),
                    key_hash="h" * 64,
                    target_model_id=uuid.UUID(ids[0]),
                    expires_at=utcnow(),
                )
            )
            await session.commit()
        assert (await client.delete(f"/api/admin/aliases/{alias['id']}")).status_code == 204
        async with session_factory() as session:
            assert (await session.execute(select(RouteSession))).first() is None
