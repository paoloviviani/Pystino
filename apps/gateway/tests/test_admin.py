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
    GroupSource,
    LimitRule,
    Membership,
    MembershipSource,
    ModelPrice,
    UsageRecord,
    UsageStatus,
    User,
)
from gateway.types import utcnow
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload


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
        as_user(app, await make_admin(session_factory, seeded))
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


class TestAdminGroupMapping:
    """`GATEWAY_OIDC__ADMIN_GROUPS` maps an IdP group onto is_admin."""

    async def test_membership_grants_admin(self, session: AsyncSession) -> None:
        from gateway.config import OIDCSettings
        from gateway.oidc import provision_user

        user = await provision_user(
            session,
            issuer="https://idp.test",
            subject="s",
            email=None,
            display_name=None,
            group_names=["research", "platform-admins"],
            settings=OIDCSettings(admin_groups=["platform-admins"]),
        )
        await session.commit()
        assert user.is_admin is True

    async def test_losing_the_group_removes_admin(self, session: AsyncSession) -> None:
        """Authoritative in both directions, like group membership itself."""
        from gateway.config import OIDCSettings
        from gateway.oidc import provision_user

        settings = OIDCSettings(admin_groups=["platform-admins"])
        await provision_user(
            session,
            issuer="https://idp.test",
            subject="s",
            email=None,
            display_name=None,
            group_names=["platform-admins"],
            settings=settings,
        )
        await session.commit()

        user = await provision_user(
            session,
            issuer="https://idp.test",
            subject="s",
            email=None,
            display_name=None,
            group_names=["research"],
            settings=settings,
        )
        await session.commit()
        assert user.is_admin is False

    async def test_unconfigured_means_the_flag_is_never_touched(
        self, session: AsyncSession
    ) -> None:
        """So `gateway seed`'s local admin keeps working.

        `admin_groups` has to be emptied **explicitly** now. It used to default
        to nothing, which made "the directory does not decide admin" the
        accidental default; it now defaults to `platform-admins`, so a
        deployment that wants the flag left alone says so. The rule this test
        guards is unchanged — an empty setting means login never writes
        `is_admin` — only the way of reaching that state.
        """
        from gateway.config import OIDCSettings
        from gateway.oidc import provision_user

        user = await provision_user(
            session,
            issuer="https://idp.test",
            subject="s",
            email=None,
            display_name=None,
            group_names=["research"],
            settings=OIDCSettings(admin_groups=[]),
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
            settings=OIDCSettings(admin_groups=[]),
        )
        await session.commit()
        assert again.is_admin is True

    async def test_the_console_grants_admin_by_granting_the_group(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The console is authoritative for admin, and this is how.

        This test used to assert the opposite — that the change was **refused**
        when `admin_groups` was set, because writing the flag would be undone
        at the next login. The observation was right and the remedy was wrong:
        it made the console useless for the decision an administrator most
        wants to make there, and it assumed whoever runs the gateway also
        administers the directory its users come from.

        What happens now is that the grant lands on the thing `is_admin` is
        *derived* from — a **manual** membership of the admin group — which a
        directory sync leaves alone (ADR 0057). So it survives the next login
        rather than being reversed by it.
        """
        as_user(app, await make_admin(session_factory, seeded))
        app.state.settings.oidc.admin_groups = ["platform-admins"]  # type: ignore[attr-defined]

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
                (
                    await db.execute(
                        select(Membership)
                        .where(Membership.user_id == target_id)
                        .options(selectinload(Membership.group))
                    )
                )
                .scalars()
                .all()
            )
            admin_rows = [m for m in memberships if m.group.name == "platform-admins"]
            assert admin_rows, "no membership of the admin group was created"
            # The provenance is the whole point: `manual` is what makes the
            # directory's next sync leave it alone.
            assert admin_rows[0].source is MembershipSource.MANUAL

    async def test_admin_the_directory_granted_cannot_be_withdrawn_here(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The asymmetry, and it is honest rather than incidental.

        Granting always works. Revoking works only on a grant this console
        made: a membership the *directory* granted comes back at the next
        login, so removing it here would appear to work and then silently
        undo itself.
        """
        as_user(app, await make_admin(session_factory, seeded))
        app.state.settings.oidc.admin_groups = ["platform-admins"]  # type: ignore[attr-defined]

        async with session_factory() as db:
            group = Group(name="platform-admins", source=GroupSource.OIDC)
            db.add(group)
            await db.flush()
            target = User(
                issuer="https://idp.test", subject="theirs", email="theirs@example.org"
            )
            db.add(target)
            await db.flush()
            db.add(
                Membership(
                    user_id=target.id,
                    group_id=group.id,
                    source=MembershipSource.OIDC,
                )
            )
            target.is_admin = True
            await db.commit()
            target_id = target.id

        response = await client.patch(
            f"/api/admin/users/{target_id}", json={"is_admin": False}
        )
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "admin_granted_by_directory"
        # And it names where to go instead, because "refused" alone is useless.
        assert "platform-admins" in response.json()["error"]["message"]

