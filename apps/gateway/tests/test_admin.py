"""The administration surface.

Two things get most of the attention here, because they are the ones that would
quietly corrupt the ledger if they were wrong: prices must stay append-only, and
deleting a model must leave every usage row in place — readable by its
denormalised name, still attributed to its user and group.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import httpx
from conftest import Seeded
from fastapi import FastAPI
from gateway.deps import get_management_user
from gateway.models import (
    Group,
    GroupModelAccess,
    IdentityEvent,
    IdentityEventAction,
    LimitRule,
    Membership,
    ModelPrice,
    UsageRecord,
    UsageStatus,
    User,
)
from gateway.types import utcnow
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def as_user(app: object, user: User) -> None:
    """Bypass the OIDC session cookie by overriding the dependency.

    The cookie path itself is covered end to end by scripts/test_oidc_flow.py
    against a real Keycloak; repeating it here would test Keycloak, not the admin
    routes.
    """
    app.dependency_overrides[get_management_user] = lambda: user  # type: ignore[attr-defined]


async def make_admin(
    session_factory: async_sessionmaker[AsyncSession], seeded: Seeded, *, admin: bool = True
) -> User:
    async with session_factory() as session:
        user = (await session.execute(select(User).where(User.id == seeded.user.id))).scalar_one()
        user.is_admin = admin
        await session.commit()
        await session.refresh(user, attribute_names=["memberships"])
        return user


class TestAccessControl:
    async def test_no_session_is_401(self, client: httpx.AsyncClient, seeded: Seeded) -> None:
        assert (await client.get("/api/admin/models")).status_code == 401

    async def test_a_non_admin_is_403(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded, admin=False))
        response = await client.get("/api/admin/models")
        assert response.status_code == 403
        assert "administrator" in response.json()["error"]["message"].lower()

    async def test_an_admin_gets_through(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        assert (await client.get("/api/admin/models")).status_code == 200


class TestModels:
    async def test_create_list_and_deactivate(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))

        created = await client.post(
            "/api/admin/models",
            json={
                "name": "new-model",
                "upstream_model": "vendor/new",
                "provider_id": str(seeded.provider.id),
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["name"] == "new-model"
        assert body["current_price"] is None
        assert body["granted_to"] == []

        listing = (await client.get("/api/admin/models")).json()["items"]
        assert {m["name"] for m in listing} == {"test-model", "new-model"}

        patched = await client.patch(f"/api/admin/models/{body['id']}", json={"is_active": False})
        assert patched.status_code == 200
        assert patched.json()["is_active"] is False

    async def test_duplicate_name_is_409_not_500(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/models",
            json={
                "name": "test-model",
                "upstream_model": "other/thing",
                "provider_id": str(seeded.provider.id),
            },
        )
        assert response.status_code == 409
        assert "already exists" in response.json()["error"]["message"]

    async def test_delete_removes_the_model_but_not_the_ledger(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The row goes; every usage record stays, attributed and readable.

        The ledger keeps the request's own ``model_name`` precisely so the
        model behind it can one day be deleted. Prices and access grants are
        meaningless without the model and go with it.
        """
        as_user(app, await make_admin(session_factory, seeded))
        async with session_factory() as db:
            db.add(
                UsageRecord(
                    request_id="req-delete-1",
                    status=UsageStatus.COMPLETED,
                    user_id=seeded.user.id,
                    group_id=seeded.group.id,
                    api_key_id=seeded.api_key.id,
                    model_id=seeded.model.id,
                    model_name=seeded.model.name,
                    currency="EUR",
                )
            )
            db.add(
                ModelPrice(
                    model_id=seeded.model.id,
                    input_per_mtok=Decimal("1"),
                    output_per_mtok=Decimal("2"),
                    currency="EUR",
                )
            )
            # The seeded grant is already in place; a second row for the same
            # pair would violate the composite primary key.
            await db.commit()

        response = await client.delete(f"/api/admin/models/{seeded.model.id}")
        assert response.status_code == 204

        assert (await client.get(f"/api/admin/models/{seeded.model.id}")).status_code == 404
        listing = (await client.get("/api/admin/models")).json()["items"]
        assert all(m["id"] != str(seeded.model.id) for m in listing)

        async with session_factory() as db:
            usage = (
                await db.execute(
                    select(UsageRecord).where(UsageRecord.request_id == "req-delete-1")
                )
            ).scalar_one()
            assert usage.model_id is None
            assert usage.model_name == seeded.model.name
            assert usage.user_id == seeded.user.id
            assert usage.group_id == seeded.group.id
            assert (await db.execute(select(func.count(ModelPrice.id)))).scalar_one() == 0
            # The seeded grant dies with the model.
            assert (
                await db.execute(select(func.count(GroupModelAccess.group_id)))
            ).scalar_one() == 0

    async def test_delete_unknown_model_is_404(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.delete(f"/api/admin/models/{uuid.uuid4()}")
        assert response.status_code == 404

    async def test_unknown_model_is_404(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.patch(f"/api/admin/models/{uuid.uuid4()}", json={})
        assert response.status_code == 404

    async def test_one_model_can_be_fetched_by_id(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """What a deep link to the console's model page needs.

        The same shape as a row of the listing, grants and current price
        included, so the page renders identically however it was reached.
        """
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.get(f"/api/admin/models/{seeded.model.id}")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["id"] == str(seeded.model.id)
        assert body["name"] == seeded.model.name
        assert "granted_to" in body and "current_price" in body

        listing = (await client.get("/api/admin/models")).json()["items"]
        assert body in listing

        missing = await client.get(f"/api/admin/models/{uuid.uuid4()}")
        assert missing.status_code == 404

    async def test_discover_is_not_shadowed_by_the_by_id_route(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """`/models/discover` is a literal path and `{model_id}` would eat it.

        FastAPI matches in declaration order, so this passes or fails on where
        the by-id route is declared — and the failure is a 422 about a malformed
        uuid from an endpoint nobody asked for, which reads like a client bug.
        """
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.get(
            "/api/admin/models/discover", params={"provider_id": str(seeded.provider.id)}
        )
        assert response.status_code != 422, response.text


class TestPrices:
    async def test_a_new_price_is_appended_never_edited(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))

        response = await client.post(
            f"/api/admin/models/{seeded.model.id}/prices",
            json={"input_per_mtok": "5", "output_per_mtok": "10"},
        )
        assert response.status_code == 201, response.text

        async with session_factory() as session:
            prices = (
                (
                    await session.execute(
                        select(ModelPrice).where(ModelPrice.model_id == seeded.model.id)
                    )
                )
                .scalars()
                .all()
            )
        # The seeded price is still there, untouched.
        assert len(prices) == 2
        assert sorted(str(p.input_per_mtok) for p in prices) == ["1.000000000000", "5.000000000000"]

    async def test_history_is_returned_newest_first(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        await client.post(
            f"/api/admin/models/{seeded.model.id}/prices",
            json={"input_per_mtok": "9", "output_per_mtok": "9"},
        )
        history = (await client.get(f"/api/admin/models/{seeded.model.id}/prices")).json()["items"]
        assert len(history) == 2
        assert Decimal(history[0]["input_per_mtok"]) == Decimal(9)

    async def test_a_future_price_does_not_take_effect_yet(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Scheduling a change must not reprice today's requests."""
        as_user(app, await make_admin(session_factory, seeded))
        future = (utcnow() + timedelta(days=7)).isoformat()
        await client.post(
            f"/api/admin/models/{seeded.model.id}/prices",
            json={"input_per_mtok": "99", "output_per_mtok": "99", "effective_from": future},
        )
        current = (await client.get("/api/admin/models")).json()["items"]
        model = next(m for m in current if m["name"] == "test-model")
        assert Decimal(model["current_price"]["input_per_mtok"]) == Decimal(1)

    async def test_wrong_currency_is_refused_with_a_reason(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            f"/api/admin/models/{seeded.model.id}/prices",
            json={"input_per_mtok": "1", "output_per_mtok": "2", "currency": "USD"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "price_currency_mismatch"

    async def test_negative_prices_are_rejected(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            f"/api/admin/models/{seeded.model.id}/prices",
            json={"input_per_mtok": "-1", "output_per_mtok": "2"},
        )
        assert response.status_code == 400


class TestGroupAccess:
    async def test_grant_and_revoke_change_what_v1_shows(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The point of the endpoint: it must actually change model visibility."""
        as_user(app, await make_admin(session_factory, seeded))

        # A second model, granted to nobody, is invisible on /v1.
        created = (
            await client.post(
                "/api/admin/models",
                json={
                    "name": "extra-model",
                    "upstream_model": "vendor/extra",
                    "provider_id": str(seeded.provider.id),
                },
            )
        ).json()
        visible = (await client.get("/v1/models", headers=seeded.auth)).json()["data"]
        assert {m["id"] for m in visible} == {"test-model"}

        granted = await client.put(f"/api/admin/groups/{seeded.group.id}/models/{created['id']}")
        assert granted.status_code == 204
        visible = (await client.get("/v1/models", headers=seeded.auth)).json()["data"]
        assert {m["id"] for m in visible} == {"test-model", "extra-model"}

        revoked = await client.delete(f"/api/admin/groups/{seeded.group.id}/models/{created['id']}")
        assert revoked.status_code == 204
        visible = (await client.get("/v1/models", headers=seeded.auth)).json()["data"]
        assert {m["id"] for m in visible} == {"test-model"}

    async def test_granting_twice_is_idempotent(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        path = f"/api/admin/groups/{seeded.group.id}/models/{seeded.model.id}"
        assert (await client.put(path)).status_code == 204
        assert (await client.put(path)).status_code == 204

        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(GroupModelAccess).where(
                            GroupModelAccess.group_id == seeded.group.id,
                            GroupModelAccess.model_id == seeded.model.id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1

    async def test_revoking_something_never_granted_is_not_an_error(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.delete(f"/api/admin/groups/{seeded.group.id}/models/{uuid.uuid4()}")
        assert response.status_code == 204

    async def test_granting_to_an_unknown_group_is_404(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.put(f"/api/admin/groups/{uuid.uuid4()}/models/{seeded.model.id}")
        assert response.status_code == 404

    async def test_groups_listing_reports_members_and_models(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        groups = (await client.get("/api/admin/groups")).json()["items"]
        research = next(g for g in groups if g["name"] == "research")
        assert research["member_count"] == 1
        assert research["models"] == ["test-model"]


class TestLimits:
    async def test_create_a_rule_and_watch_it_bite(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: object,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """An admin-created rule must actually enforce, not just persist."""
        as_user(app, await make_admin(session_factory, seeded))
        created = await client.post(
            "/api/admin/limits",
            json={
                "name": "no spending",
                "scope": "group",
                "scope_id": str(seeded.group.id),
                "metric": "cost",
                "window_seconds": 3600,
                "limit_value": "0",
            },
        )
        assert created.status_code == 201, created.text

        response = await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
            headers=seeded.auth,
        )
        assert response.status_code == 429

    async def test_deactivating_a_rule_stops_it_enforcing(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: object,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        created = (
            await client.post(
                "/api/admin/limits",
                json={
                    "scope": "group",
                    "scope_id": str(seeded.group.id),
                    "metric": "requests",
                    "window_seconds": 3600,
                    "limit_value": "0",
                },
            )
        ).json()

        patched = await client.patch(
            f"/api/admin/limits/{created['id']}", json={"is_active": False}
        )
        assert patched.status_code == 200
        assert patched.json()["is_active"] is False

        from helpers import completion_body

        fake_upstream.set_json(completion_body())  # type: ignore[attr-defined]
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
            headers=seeded.auth,
        )
        assert response.status_code == 200

    async def test_a_global_rule_must_not_carry_a_scope_id(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/limits",
            json={
                "scope": "global",
                "scope_id": str(seeded.group.id),
                "metric": "cost",
                "window_seconds": 60,
                "limit_value": "1",
            },
        )
        assert response.status_code == 400

    async def test_a_scoped_rule_needs_a_scope_id(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/limits",
            json={"scope": "group", "metric": "cost", "window_seconds": 60, "limit_value": "1"},
        )
        assert response.status_code == 400

    async def test_a_rule_for_a_nonexistent_group_is_refused_up_front(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Otherwise it persists happily and silently matches nothing."""
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/limits",
            json={
                "scope": "group",
                "scope_id": str(uuid.uuid4()),
                "metric": "cost",
                "window_seconds": 60,
                "limit_value": "1",
            },
        )
        assert response.status_code == 404

    async def test_duplicate_rules_are_409(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        body = {
            "scope": "group",
            "scope_id": str(seeded.group.id),
            "metric": "cost",
            "window_seconds": 3600,
            "limit_value": "5",
        }
        assert (await client.post("/api/admin/limits", json=body)).status_code == 201
        assert (await client.post("/api/admin/limits", json=body)).status_code == 409

    async def test_delete_removes_the_rule(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        created = (
            await client.post(
                "/api/admin/limits",
                json={
                    "scope": "global",
                    "metric": "tokens",
                    "window_seconds": 60,
                    "limit_value": "1",
                },
            )
        ).json()
        assert (await client.delete(f"/api/admin/limits/{created['id']}")).status_code == 204
        async with session_factory() as session:
            remaining = (await session.execute(select(LimitRule))).scalars().all()
        assert remaining == []


class TestUsersAndUsage:
    async def test_user_listing_shows_groups_and_keys(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        users = (await client.get("/api/admin/users")).json()["items"]
        entry = next(u for u in users if u["email"] == "member@example.org")
        assert entry["groups"] == ["research"]
        assert entry["active_key_count"] == 1
        assert entry["default_billing_group"] == "research"

    async def test_deactivating_a_user_blocks_their_key(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # A second admin, so this is not the last-admin guard's case — that one
        # is covered by TestLastAdminGuard below.
        admin = await make_admin(session_factory, seeded)
        async with session_factory() as session:
            session.add(User(issuer="https://idp.test", subject="other-admin", is_admin=True))
            await session.commit()
        as_user(app, admin)
        response = await client.patch(
            f"/api/admin/users/{seeded.user.id}", json={"is_active": False}
        )
        assert response.status_code == 200
        assert (await client.get("/v1/models", headers=seeded.auth)).status_code == 401

    async def test_usage_is_grouped_and_sorted_by_spend(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as session:
            other = Group(name="other-group")
            session.add(other)
            await session.flush()
            session.add_all(
                [
                    UsageRecord(
                        request_id="a",
                        status=UsageStatus.COMPLETED,
                        group_id=seeded.group.id,
                        model_name="m",
                        currency="EUR",
                        total_tokens=100,
                        cost=Decimal("0.25"),
                    ),
                    UsageRecord(
                        request_id="b",
                        status=UsageStatus.COMPLETED,
                        group_id=other.id,
                        model_name="m",
                        currency="EUR",
                        total_tokens=400,
                        cost=Decimal("2.5"),
                    ),
                ]
            )
            await session.commit()

        as_user(app, await make_admin(session_factory, seeded))
        rows = (await client.get("/api/admin/usage")).json()
        assert [r["group_name"] for r in rows] == ["other-group", "research"]
        assert Decimal(rows[0]["cost"]) == Decimal("2.5")

    async def test_an_absurd_window_is_rejected(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        assert (await client.get("/api/admin/usage?window_seconds=5")).status_code == 400


class TestLastAdminGuard:
    """ADR 0093 §5.5: PATCH and DELETE refuse to leave zero active admins."""

    async def _lone_admin(self, app: object, session_factory, seeded: Seeded) -> User:
        return await make_admin(session_factory, seeded)

    async def test_self_deactivation_of_the_last_admin_is_refused(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await self._lone_admin(app, session_factory, seeded)
        as_user(app, admin)
        response = await client.patch(f"/api/admin/users/{admin.id}", json={"is_active": False})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "last_admin"

        async with session_factory() as db:
            user = (await db.execute(select(User).where(User.id == admin.id))).scalar_one()
            assert user.is_active

    async def test_self_demotion_of_the_last_admin_is_refused(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await self._lone_admin(app, session_factory, seeded)
        as_user(app, admin)
        response = await client.patch(f"/api/admin/users/{admin.id}", json={"is_admin": False})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "last_admin"

    async def test_a_refusal_is_audited(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await self._lone_admin(app, session_factory, seeded)
        as_user(app, admin)
        await client.patch(f"/api/admin/users/{admin.id}", json={"is_active": False})

        async with session_factory() as db:
            row = (
                await db.execute(select(IdentityEvent).order_by(IdentityEvent.at.desc()))
            ).scalars().first()
            assert row is not None
            assert row.action == IdentityEventAction.ADMIN_REFUSED_LAST
            assert row.target_user_id == admin.id

    async def test_demoting_one_of_two_admins_succeeds_and_is_audited(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await self._lone_admin(app, session_factory, seeded)
        async with session_factory() as session:
            other = User(issuer="https://idp.test", subject="other-admin", is_admin=True)
            session.add(other)
            await session.commit()
            other_id = other.id
        as_user(app, admin)
        response = await client.patch(f"/api/admin/users/{other_id}", json={"is_admin": False})
        assert response.status_code == 200

        async with session_factory() as db:
            row = (
                await db.execute(
                    select(IdentityEvent)
                    .where(IdentityEvent.target_user_id == other_id)
                    .order_by(IdentityEvent.at.desc())
                )
            ).scalars().first()
            assert row is not None
            assert row.action == IdentityEventAction.ADMIN_REVOKE

    async def test_deleting_a_non_admin_still_works(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await self._lone_admin(app, session_factory, seeded)
        async with session_factory() as session:
            victim = User(issuer="https://idp.test", subject="victim", email="victim@example.org")
            session.add(victim)
            await session.commit()
            victim_id = victim.id
        as_user(app, admin)
        response = await client.delete(f"/api/admin/users/{victim_id}")
        assert response.status_code == 204


class TestUserProfileEdits:
    """The console edits what the account says, not just what it may do.

    Two shape facts pin the design. Identity is ``(issuer, subject)`` and is
    not on the request model at all, so a client that sends it gets its other
    fields edited and its identity untouched — the route never re-keys a
    person out from under their ledger. And the schema's lengths mirror the
    columns (email 320, the names 255) while the *shape* check on email stays
    in the route, so the console gets the same sentence ``create_user``
    produces rather than a pydantic field list.
    """

    async def _admin(self, app: object, session_factory, seeded: Seeded) -> None:
        as_user(app, await make_admin(session_factory, seeded))

    async def test_profile_fields_update_and_are_recorded_as_edited(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await self._admin(app, session_factory, seeded)
        response = await client.patch(
            f"/api/admin/users/{seeded.user.id}",
            json={
                "email": "renamed@example.org",
                "display_name": "Renamed Member",
                "username": "renamed@local",
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["email"] == "renamed@example.org"
        assert body["display_name"] == "Renamed Member"
        assert body["username"] == "renamed@local"

        async with session_factory() as db:
            user = (
                await db.execute(select(User).where(User.id == seeded.user.id))
            ).scalar_one()
            assert user.email == "renamed@example.org"
            # The record is what makes the edit durable: provisioning reads it
            # and leaves a listed field alone (test_oidc.py pins that half).
            assert sorted(user.admin_edited_fields) == ["display_name", "email", "username"]

    async def test_a_field_left_out_stays_and_is_not_recorded(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """PATCH semantics: absent means untouched, here and in the ledger.

        Recording an unedited field as administrator-edited would quietly
        detach it from the directory — the opposite of what the operator asked,
        which was to change nothing about it.
        """
        await self._admin(app, session_factory, seeded)
        response = await client.patch(
            f"/api/admin/users/{seeded.user.id}", json={"display_name": "Only A Name"}
        )
        assert response.status_code == 200
        assert response.json()["email"] == "member@example.org"

        async with session_factory() as db:
            user = (
                await db.execute(select(User).where(User.id == seeded.user.id))
            ).scalar_one()
            assert user.admin_edited_fields == ["display_name"]

    async def test_an_explicit_null_clears_the_field(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await self._admin(app, session_factory, seeded)
        response = await client.patch(
            f"/api/admin/users/{seeded.user.id}", json={"username": None}
        )
        assert response.status_code == 200
        assert response.json()["username"] is None

    async def test_a_blank_value_clears_rather_than_storing_whitespace(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await self._admin(app, session_factory, seeded)
        response = await client.patch(
            f"/api/admin/users/{seeded.user.id}", json={"display_name": "   "}
        )
        assert response.status_code == 200
        assert response.json()["display_name"] is None

    async def test_a_malformed_email_is_refused_with_the_create_route_sentence(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await self._admin(app, session_factory, seeded)
        response = await client.patch(
            f"/api/admin/users/{seeded.user.id}", json={"email": "not-an-address"}
        )
        assert response.status_code == 400
        assert "valid email" in response.json()["error"]["message"]

    async def test_an_overlong_value_is_refused_at_the_column_width(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """255 is what the column holds; the schema is where the API says so.

        A refusal rather than a silent truncation: an account whose stored
        name is not the name anybody typed is a bug that surfaces weeks later,
        from a screenshot, without the request that caused it. The app rewrites
        FastAPI's 422s into its error envelope (main.py), so the status the
        client sees is 400 — the same as every other refusal on this surface.
        """
        await self._admin(app, session_factory, seeded)
        response = await client.patch(
            f"/api/admin/users/{seeded.user.id}", json={"display_name": "x" * 256}
        )
        assert response.status_code == 400
        assert "display_name" in response.json()["error"]["message"]

    async def test_identity_fields_are_not_editable(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """``issuer`` + ``subject`` are the login identity and stay out of reach.

        The pair is UNIQUE together and *is* the account's key everywhere —
        the session cookie, every key's owner, every ledger row. A PATCH that
        moved it would not rename the person; it would strand the old row's
        memberships and keys on a stranger. Sent, they are ignored rather than
        refused: the client's other fields still edit, and identity was never
        on offer to begin with.
        """
        await self._admin(app, session_factory, seeded)
        response = await client.patch(
            f"/api/admin/users/{seeded.user.id}",
            json={"issuer": "https://evil.test", "subject": "taken-over", "is_active": True},
        )
        assert response.status_code == 200, response.text

        async with session_factory() as db:
            user = (
                await db.execute(select(User).where(User.id == seeded.user.id))
            ).scalar_one()
            assert user.issuer == "https://idp.test"
            assert user.subject == "subject-1"


class TestAdminIsAConsoleFact:
    """Authorisation is a gateway fact: no claim, group or setting moves the flag.

    ADR 0069 removed the last automatic admin path. Three things follow, and
    each is a test: the removed setting refuses to start rather than doing
    nothing; a directory group named `platform-admins` confers nothing; and
    the console's own grant is durable, because provisioning no longer
    derives the flag from anything.
    """

    async def test_the_removed_setting_is_refused_at_startup(self) -> None:
        """A setting that would silently do nothing is worse than no setting.

        Removed, not deprecated (ADR 0069): an operator who still sets
        GATEWAY_OIDC__ADMIN_GROUPS is told so at startup, in terms of what to
        do instead, rather than owning a knob that turns nothing.
        """
        import pytest
        from gateway.config import OIDCSettings

        with pytest.raises(ValueError, match="ADR 0069"):
            OIDCSettings(admin_groups=["platform-admins"])

    async def test_a_group_claim_never_confers_admin(self, session: AsyncSession) -> None:
        """The directory authenticates; it never administers."""
        from gateway.config import OIDCSettings
        from gateway.oidc import provision_user

        user = await provision_user(
            session,
            issuer="https://idp.test",
            subject="s",
            email=None,
            display_name=None,
            group_names=["research", "platform-admins"],
            settings=OIDCSettings(),
        )
        await session.commit()
        assert user.is_admin is False

    async def test_the_console_sets_the_flag_directly(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """PATCH writes the flag, and nothing else.

        The old machinery granted a manual membership of the admin group so a
        login would not undo the flag — a workaround for a derivation that no
        longer exists. What is asserted now is the absence of the workaround
        as much as the presence of the flag: no membership is created, because
        no group is what made this person an administrator.
        """
        as_user(app, await make_admin(session_factory, seeded))

        async with session_factory() as db:
            target = User(issuer="https://idp.test", subject="promote-me")
            db.add(target)
            await db.commit()
            target_id = target.id

        response = await client.patch(
            f"/api/admin/users/{target_id}", json={"is_admin": True}
        )
        assert response.status_code == 200, response.text
        assert response.json()["is_admin"] is True

        async with session_factory() as db:
            memberships = (
                (await db.execute(select(Membership).where(Membership.user_id == target_id)))
                .scalars()
                .all()
            )
            assert memberships == [], "a group grant is no longer how admin is conferred"

    async def test_the_flag_survives_every_login(self, session: AsyncSession) -> None:
        """The old failure this replaces: login derived the flag and undid the grant.

        `test_the_console_grants_admin_by_granting_the_group` worked around
        that by making the grant land on what the flag was derived *from*.
        With the derivation gone (ADR 0069), the plain flag is durable and the
        workaround is gone with it.
        """
        from gateway.config import OIDCSettings
        from gateway.oidc import provision_user

        settings = OIDCSettings()
        user = await provision_user(
            session,
            issuer="https://idp.test",
            subject="s",
            email=None,
            display_name=None,
            group_names=["research"],
            settings=settings,
        )
        user.is_admin = True
        await session.commit()

        again = await provision_user(
            session,
            issuer="https://idp.test",
            subject="s",
            email=None,
            display_name=None,
            group_names=["research", "anything-else"],
            settings=settings,
        )
        await session.commit()
        assert again.is_admin is True

    async def test_unconfigured_means_the_flag_is_never_touched(
        self, session: AsyncSession
    ) -> None:
        """The rule this test has always guarded, now the only behaviour."""
        from gateway.config import OIDCSettings
        from gateway.oidc import provision_user

        user = await provision_user(
            session,
            issuer="https://idp.test",
            subject="s",
            email=None,
            display_name=None,
            group_names=["research"],
            settings=OIDCSettings(),
        )
        user.is_admin = True
        await session.commit()

        again = await provision_user(
            session,
            issuer="https://idp.test",
            subject="s",
            email=None,
            display_name=None,
            group_names=["research"],
            settings=OIDCSettings(),
        )
        await session.commit()
        assert again.is_admin is True
