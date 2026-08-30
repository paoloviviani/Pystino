"""Public model access (ADR 0045).

The rule this pins: ``is_public`` widens *access only* — any authenticated
caller may use the model, billed to their own default billing group. The tests
that matter are the ones where intuition and the code could part ways: a caller
whose groups have no grant, billing through the caller's own group, listing
visibility, and that the default stays closed.
"""

from __future__ import annotations

import httpx
import pytest_asyncio
from conftest import FakeUpstream
from gateway.models import ApiKey, Group, GroupModelAccess, Membership, ModelDef, User
from gateway.security import generate_api_key
from helpers import completion_body
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest_asyncio.fixture
async def public_model(seeded, session: AsyncSession) -> ModelDef:
    """The seeded model, marked public, with its group grant removed.

    Starting from the seeded model and stripping the grant is the sharper
    arrangement: whatever the tests then prove can only be the flag's doing,
    not the grant's.
    """
    model = (
        await session.execute(select(ModelDef).where(ModelDef.name == "test-model"))
    ).scalar_one()
    await session.execute(update(ModelDef).where(ModelDef.id == model.id).values(is_public=True))
    await session.execute(delete(GroupModelAccess).where(GroupModelAccess.model_id == model.id))
    await session.commit()
    await session.refresh(model)
    return model


@pytest_asyncio.fixture
async def outsider(
    seeded, session: AsyncSession
) -> tuple[User, str]:
    """A user in a group that has no grants, plus their usable key.

    Deliberately *not* a groupless user: a groupless caller cannot bill anything,
    which is a separate refusal (billing, not access). This caller has somewhere
    for the spend to land and still no grant — so a success here can only come
    from the public flag.
    """
    group = Group(name="outsiders", description="no grants on purpose")
    user = User(issuer="https://idp.test", subject="outsider", email="outsider@example.org")
    session.add_all([group, user])
    await session.flush()
    session.add(Membership(user_id=user.id, group_id=group.id))
    user.default_billing_group_id = group.id
    generated = generate_api_key()
    session.add(
        ApiKey(
            user_id=user.id,
            prefix=generated.prefix,
            key_hash=generated.key_hash,
            name="outsider key",
            billing_group_id=group.id,
        )
    )
    await session.commit()
    await session.refresh(user)
    return user, generated.secret


class TestPublicAccess:
    async def test_public_model_is_listed_without_any_grant(
        self,
        client: httpx.AsyncClient,
        public_model,
        outsider: tuple[User, str],
    ) -> None:
        _, secret = outsider
        listing = await client.get("/v1/models", headers={"authorization": f"Bearer {secret}"})
        assert listing.status_code == 200
        assert [m["id"] for m in listing.json()["data"]] == ["test-model"]

    async def test_a_public_model_serves_a_caller_with_no_grant(
        self,
        client: httpx.AsyncClient,
        public_model,
        outsider: tuple[User, str],
        fake_upstream: FakeUpstream,
    ) -> None:
        fake_upstream.set_json(completion_body())
        _, secret = outsider
        response = await client.post(
            "/v1/chat/completions",
            headers={"authorization": f"Bearer {secret}"},
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["choices"][0]["message"]["role"] == "assistant"

    async def test_private_models_stay_hidden_without_a_grant(
        self, client: httpx.AsyncClient, seeded, outsider: tuple[User, str]
    ) -> None:
        """The default is unchanged: no flag, no grant, no access."""
        _, secret = outsider
        listing = await client.get("/v1/models", headers={"authorization": f"Bearer {secret}"})
        assert listing.status_code == 200
        assert listing.json()["data"] == []

    async def test_a_groupless_caller_is_still_refused_even_a_public_model(
        self,
        client: httpx.AsyncClient,
        seeded,
        public_model,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Access is granted; billing is not. Public is not free."""
        async with session_factory() as db:
            groupless = User(issuer="https://idp.test", subject="groupless")
            db.add(groupless)
            await db.flush()
            generated = generate_api_key()
            db.add(
                ApiKey(
                    user_id=groupless.id,
                    prefix=generated.prefix,
                    key_hash=generated.key_hash,
                    name="groupless key",
                    billing_group_id=None,
                )
            )
            await db.commit()
            secret = generated.secret

        response = await client.post(
            "/v1/chat/completions",
            headers={"authorization": f"Bearer {secret}"},
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
        )
        # Not 401 — the caller is authenticated. The refusal is billing's.
        assert response.status_code == 403
        assert "billing group" in response.json()["error"]["message"].lower()


class TestAdminToggle:
    async def test_flag_round_trips_through_the_admin_api(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from test_admin import as_user, make_admin

        admin = await make_admin(session_factory=session_factory, seeded=seeded)
        as_user(app, admin)

        created = await client.post(
            "/api/admin/models",
            json={
                "name": "open-to-all",
                "upstream_model": "upstream/open",
                "provider_id": str(seeded.provider.id),
                "is_public": True,
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["is_public"] is True

        model_id = created.json()["id"]
        restricted = await client.patch(
            f"/api/admin/models/{model_id}", json={"is_public": False}
        )
        assert restricted.status_code == 200
        assert restricted.json()["is_public"] is False

        # And the listing agrees — the console renders from it.
        listing = await client.get("/api/admin/models?q=open-to-all")
        assert listing.json()["items"][0]["is_public"] is False
