"""Bring your own key: private providers and models.

What this pins, in the order the failure would hurt:

* **Access.** A private model is usable by its owner only, only while the owner
  holds the capability, and is addressed ``my/<name>``. Another user's id is a
  404 everywhere; a deployment model never rides a private provider nor the
  reverse; an alias can never target one.
* **Accounting.** A BYOK request writes its usage row (flagged, priced at the
  owner's own rows) and is invisible to every quota, every group or
  administrator report and the billing totals; it appears only in its owner's
  own usage.
* **The network guard** at the API (create/update refuse, request time refuses)
  and its admin switch. The guard's mechanics are in ``test_netguard.py``.
* **Secrets.** The credential is encrypted at rest and never in any response,
  the administrators' view included.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
import pytest
import pytest_asyncio
from conftest import FakeUpstream, Seeded
from fastapi import FastAPI
from gateway.models import (
    AdminAuditEvent,
    ApiKey,
    Group,
    GroupModelAccess,
    LimitAppliesTo,
    LimitMetric,
    LimitRule,
    LimitScope,
    Membership,
    ModelDef,
    Provider,
    UsageRecord,
    User,
)
from gateway.netguard import NetworkGuardError
from gateway.secrets import SecretBox
from gateway.security import generate_api_key
from helpers import completion_body
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin

BOX = SecretBox(["test-encryption-key-not-for-production"])
USER_KEY = "sk-user-own-key-0123456789"
PUBLIC_HOST = "https://api.example.com/v1"
PUBLIC_IP = "93.184.216.34"

#: What the fake DNS answers. A test flips an entry to play a rebinding attacker.
DNS: dict[str, list[str]] = {}


@pytest.fixture(autouse=True)
def fake_dns(app: FastAPI) -> dict[str, list[str]]:
    DNS.clear()
    DNS.update({"api.example.com": [PUBLIC_IP], "other.example.org": [PUBLIC_IP]})

    async def resolver(host: str, port: int) -> list[str]:
        if host in DNS:
            return list(DNS[host])
        raise NetworkGuardError(f"The host {host!r} does not resolve.")

    app.state.providers._resolver = resolver
    return DNS


# --------------------------------------------------------------------------
# people and rows
# --------------------------------------------------------------------------


@dataclass
class Member:
    user_id: uuid.UUID
    group_id: uuid.UUID
    secret: str
    email: str

    @property
    def auth(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.secret}"}


async def add_member(
    session_factory: async_sessionmaker[AsyncSession], name: str, *, byok: bool = True
) -> Member:
    """A person in their own group, with a usable API key (the group carries the
    api_keys capability, otherwise the key would be refused before any model
    question) and the byok flag on or off."""
    async with session_factory() as session:
        group = Group(name=f"group-{name}", api_keys_allowed=True, byok_allowed=byok)
        user = User(issuer="https://idp.test", subject=name, email=f"{name}@example.org")
        user.display_name = name.title()
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
                name=f"{name} key",
                billing_group_id=group.id,
            )
        )
        await session.commit()
        return Member(user.id, group.id, generated.secret, user.email or "")


async def set_flag(
    session_factory: async_sessionmaker[AsyncSession], member: Member, *, byok: bool
) -> None:
    async with session_factory() as session:
        group = await session.get(Group, member.group_id)
        assert group is not None
        group.byok_allowed = byok
        await session.commit()


async def fresh(session_factory: async_sessionmaker[AsyncSession], member: Member) -> User:
    async with session_factory() as session:
        return (await session.execute(select(User).where(User.id == member.user_id))).scalar_one()


async def acting(app: FastAPI, session_factory: Any, member: Member) -> None:
    """Make the next management call this person's (the cookie is bypassed, as
    test_admin.as_user does; the BYOK scope reads capability from this user)."""
    as_user(app, await fresh(session_factory, member))


@pytest_asyncio.fixture
async def alice(session_factory: async_sessionmaker[AsyncSession], seeded: Seeded) -> Member:
    return await add_member(session_factory, "alice")


@pytest_asyncio.fixture
async def bob(session_factory: async_sessionmaker[AsyncSession], seeded: Seeded) -> Member:
    return await add_member(session_factory, "bob")


async def new_provider(
    app: FastAPI,
    client: httpx.AsyncClient,
    factory: Any,
    who: Member,
    name: str = "mine",
    **payload: Any,
) -> dict[str, Any]:
    await acting(app, factory, who)
    body = {"name": name, "base_url": PUBLIC_HOST, "api_key": USER_KEY, **payload}
    response = await client.post("/api/me/providers", json=body)
    assert response.status_code == 201, response.text
    return dict(response.json())


async def new_model(
    app: FastAPI,
    client: httpx.AsyncClient,
    factory: Any,
    who: Member,
    provider_id: str,
    name: str = "mine",
    *,
    price: bool = True,
    **payload: Any,
) -> dict[str, Any]:
    await acting(app, factory, who)
    body = {"name": name, "upstream_model": f"vendor/{name}", "provider_id": provider_id, **payload}
    response = await client.post("/api/me/models", json=body)
    assert response.status_code == 201, response.text
    model = dict(response.json())
    if price:
        priced = await client.post(
            f"/api/me/models/{model['id']}/prices",
            json={"input_per_mtok": "1", "output_per_mtok": "2", "currency": "EUR"},
        )
        assert priced.status_code == 201, priced.text
    return model


@dataclass
class Own:
    provider: dict[str, Any]
    model: dict[str, Any]


async def own(
    app: FastAPI, client: httpx.AsyncClient, factory: Any, who: Member, name: str = "mine"
) -> Own:
    provider = await new_provider(app, client, factory, who, name)
    return Own(provider, await new_model(app, client, factory, who, provider["id"], name))


async def chat(client: httpx.AsyncClient, who: Member, model: str, **extra: Any) -> httpx.Response:
    return await client.post(
        "/v1/chat/completions",
        headers=who.auth,
        json={"model": model, "messages": [{"role": "user", "content": "hi"}], **extra},
    )


# --------------------------------------------------------------------------
# the capability
# --------------------------------------------------------------------------


class TestCapability:
    async def test_without_it_every_route_answers_403_with_one_code(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        seeded: Seeded,
    ) -> None:
        nobody = await add_member(session_factory, "nobody", byok=False)
        await acting(app, session_factory, nobody)
        for method, url in [
            ("GET", "/api/me/providers"),
            ("POST", "/api/me/providers"),
            ("GET", "/api/me/models"),
            ("POST", "/api/me/models"),
            ("GET", f"/api/me/models/{uuid.uuid4()}"),
            ("GET", "/api/me/models/discover?provider_id=" + str(uuid.uuid4())),
            ("POST", f"/api/me/providers/{uuid.uuid4()}/test"),
            ("GET", "/api/me/provider-plugins"),
        ]:
            response = await client.request(method, url, json={} if method == "POST" else None)
            assert response.status_code == 403, (method, url, response.text)
            assert response.json()["error"]["code"] == "byok_not_allowed"
            assert "bring-your-own-key" in response.json()["error"]["message"].lower()

    async def test_it_is_reported_on_me_and_v1_me(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        alice: Member,
    ) -> None:
        await acting(app, session_factory, alice)
        assert (await client.get("/api/me")).json()["capabilities"]["byok"] is True
        assert (await client.get("/v1/me", headers=alice.auth)).json()["capabilities"][
            "byok"
        ] is True
        await set_flag(session_factory, alice, byok=False)
        await acting(app, session_factory, alice)
        assert (await client.get("/api/me")).json()["capabilities"]["byok"] is False
        assert (await client.get("/v1/me", headers=alice.auth)).json()["capabilities"][
            "byok"
        ] is False

    async def test_an_administrator_holds_it_like_the_other_capabilities(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        seeded: Seeded,
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        assert (await client.get("/api/me/providers")).status_code == 200

    async def test_losing_it_keeps_the_rows_and_regaining_restores_them(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        alice: Member,
        fake_upstream: FakeUpstream,
    ) -> None:
        await own(app, client, session_factory, alice)
        fake_upstream.set_json(completion_body("ok"))
        assert (await chat(client, alice, "my/mine")).status_code == 200
        assert "my/mine" in {
            m["id"] for m in (await client.get("/v1/models", headers=alice.auth)).json()["data"]
        }

        await set_flag(session_factory, alice, byok=False)
        lost = await chat(client, alice, "my/mine")
        assert lost.status_code == 403
        assert lost.json()["error"]["code"] == "byok_not_allowed"
        listed = await client.get("/v1/models", headers=alice.auth)
        assert "my/mine" not in {m["id"] for m in listed.json()["data"]}
        assert (await client.get("/v1/models/my/mine", headers=alice.auth)).status_code == 404
        # Kept, not deleted.
        async with session_factory() as session:
            assert (
                await session.scalar(
                    select(func.count()).select_from(ModelDef).where(ModelDef.owner_id.is_not(None))
                )
            ) == 1

        await set_flag(session_factory, alice, byok=True)
        assert (await chat(client, alice, "my/mine")).status_code == 200
        assert "my/mine" in {
            m["id"] for m in (await client.get("/v1/models", headers=alice.auth)).json()["data"]
        }

    async def test_a_deactivated_group_does_not_confer_it(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        alice: Member,
    ) -> None:
        async with session_factory() as session:
            group = await session.get(Group, alice.group_id)
            assert group is not None
            group.is_active = False
            await session.commit()
        await acting(app, session_factory, alice)
        assert (await client.get("/api/me/providers")).status_code == 403

    async def test_the_group_toggle_and_the_new_group_default(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        seeded: Seeded,
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        # Default off: a new group carries no byok.
        created = await client.post("/api/admin/groups", json={"name": "fresh"})
        assert created.status_code == 201, created.text
        assert created.json()["byok_allowed"] is False
        toggled = await client.patch(
            f"/api/admin/groups/{created.json()['id']}", json={"byok_allowed": True}
        )
        assert toggled.json()["byok_allowed"] is True
        # The deployment default for NEW groups.
        policy = await client.put("/api/admin/oidc/policy", json={"byok_mode": "on"})
        assert policy.status_code == 200, policy.text
        assert policy.json()["byok_mode"] == "on"
        later = await client.post("/api/admin/groups", json={"name": "later"})
        assert later.json()["byok_allowed"] is True
        # Existing groups are untouched by a default change.
        groups = (await client.get("/api/admin/groups")).json()["items"]
        assert {g["name"]: g["byok_allowed"] for g in groups}["research"] is False
        # The bulk action reaches every group.
        bulk = await client.post(
            "/api/admin/groups/capabilities", json={"capability": "byok", "value": False}
        )
        assert bulk.status_code == 200, bulk.text
        groups = (await client.get("/api/admin/groups")).json()["items"]
        assert not any(g["byok_allowed"] for g in groups)

    async def test_the_default_is_off_in_the_environment_too(self, app: FastAPI) -> None:
        assert app.state.settings.access.byok == "off"
        assert app.state.oidc_policy.policy.byok_mode == "off"
        assert app.state.oidc_policy.policy.byok_private_networks is False


# --------------------------------------------------------------------------
# owner isolation
# --------------------------------------------------------------------------


class TestIsolation:
    async def test_another_users_ids_are_404_on_every_route(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        alice: Member,
        bob: Member,
    ) -> None:
        theirs = await own(app, client, session_factory, alice)
        await acting(app, session_factory, bob)
        pid, mid = theirs.provider["id"], theirs.model["id"]
        probes = [
            ("PATCH", f"/api/me/providers/{pid}", {"description": "x"}),
            ("DELETE", f"/api/me/providers/{pid}", None),
            ("POST", f"/api/me/providers/{pid}/test", None),
            ("GET", f"/api/me/models/{mid}", None),
            ("PATCH", f"/api/me/models/{mid}", {"is_active": False}),
            ("DELETE", f"/api/me/models/{mid}", None),
            ("GET", f"/api/me/models/{mid}/prices", None),
            (
                "POST",
                f"/api/me/models/{mid}/prices",
                {"input_per_mtok": "1", "output_per_mtok": "1"},
            ),
            ("GET", f"/api/me/models/discover?provider_id={pid}", None),
            ("GET", f"/api/me/models/tags?provider_id={pid}", None),
            (
                "POST",
                f"/api/me/models/import?provider_id={pid}",
                {"models": [{"upstream_model": "x"}]},
            ),
        ]
        for method, url, body in probes:
            response = await client.request(method, url, json=body)
            assert response.status_code == 404, (method, url, response.status_code, response.text)
        # And nothing was changed by any of them.
        async with session_factory() as session:
            assert await session.get(Provider, uuid.UUID(pid)) is not None
            model = await session.get(ModelDef, uuid.UUID(mid))
            assert model is not None and model.is_active is True

    async def test_lists_show_only_the_callers_own_rows(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        alice: Member,
        bob: Member,
    ) -> None:
        await own(app, client, session_factory, alice, "alice-one")
        await own(app, client, session_factory, bob, "bob-one")
        await acting(app, session_factory, alice)
        providers = (await client.get("/api/me/providers")).json()
        models = (await client.get("/api/me/models")).json()
        assert [p["name"] for p in providers["items"]] == ["alice-one"]
        assert [m["name"] for m in models["items"]] == ["alice-one"]
        assert providers["total"] == 1

    async def test_v1_models_never_lists_someone_elses(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        seeded: Seeded,
        alice: Member,
        bob: Member,
    ) -> None:
        await own(app, client, session_factory, alice, "alice-one")
        await own(app, client, session_factory, bob, "bob-one")
        mine = (await client.get("/v1/models", headers=alice.auth)).json()["data"]
        assert "my/alice-one" in {m["id"] for m in mine}
        assert not any("bob-one" in m["id"] for m in mine)
        # The unauthenticated listing is deployment-only.
        public = (await client.get("/v1/models")).json()["data"]
        assert not any(m["id"].startswith("my/") or "one" in m["id"] for m in public)
        # Nor can she address his, by either spelling.
        assert (await client.get("/v1/models/my/bob-one", headers=alice.auth)).status_code == 404
        assert (await client.get("/v1/models/bob-one", headers=alice.auth)).status_code == 404

    async def test_another_users_model_cannot_be_used(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        alice: Member,
        bob: Member,
        fake_upstream: FakeUpstream,
    ) -> None:
        await own(app, client, session_factory, bob, "bob-one")
        fake_upstream.set_json(completion_body("ok"))
        for name in ("my/bob-one", "bob-one"):
            response = await chat(client, alice, name)
            assert response.status_code == 404, name
        assert fake_upstream.bodies == []

    async def test_a_model_cannot_ride_someone_elses_provider(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        alice: Member,
        bob: Member,
    ) -> None:
        theirs = await new_provider(app, client, session_factory, bob, "bobs")
        await acting(app, session_factory, alice)
        response = await client.post(
            "/api/me/models",
            json={"name": "x", "upstream_model": "u", "provider_id": theirs["id"]},
        )
        assert response.status_code == 404

    async def test_the_admin_catalogue_never_shows_or_touches_private_rows(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        seeded: Seeded,
        alice: Member,
    ) -> None:
        theirs = await own(app, client, session_factory, alice)
        as_user(app, await make_admin(session_factory, seeded))
        providers = {p["name"] for p in (await client.get("/api/admin/providers")).json()["items"]}
        models = {m["name"] for m in (await client.get("/api/admin/models")).json()["items"]}
        assert "mine" not in providers and "mine" not in models
        mid, pid = theirs.model["id"], theirs.provider["id"]
        for method, url, body in [
            ("GET", f"/api/admin/models/{mid}", None),
            ("PATCH", f"/api/admin/models/{mid}", {"is_active": False}),
            ("DELETE", f"/api/admin/models/{mid}", None),
            ("GET", f"/api/admin/models/{mid}/prices", None),
            ("PATCH", f"/api/admin/providers/{pid}", {"is_active": False}),
            ("DELETE", f"/api/admin/providers/{pid}", None),
            ("POST", f"/api/admin/providers/{pid}/test", None),
        ]:
            response = await client.request(method, url, json=body)
            assert response.status_code == 404, (method, url, response.text)
        # Grants cannot be made on a private model, by group or by person.
        group = await client.get("/api/admin/groups")
        gid = group.json()["items"][0]["id"]
        assert (await client.put(f"/api/admin/groups/{gid}/models/{mid}")).status_code == 404
        assert (
            await client.put(f"/api/admin/users/{seeded.user.id}/models/{mid}")
        ).status_code == 404

    async def test_a_grant_row_forced_in_does_not_widen_a_private_model(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        seeded: Seeded,
        alice: Member,
        fake_upstream: FakeUpstream,
    ) -> None:
        """Defence in depth: even a grant row inserted by hand (the routes
        refuse to write one) reaches nothing, and `is_public` on a private row
        is ignored by the predicate."""
        theirs = await own(app, client, session_factory, alice)
        async with session_factory() as session:
            model = await session.get(ModelDef, uuid.UUID(theirs.model["id"]))
            assert model is not None
            model.is_public = True
            session.add(GroupModelAccess(group_id=seeded.group.id, model_id=model.id))
            await session.commit()
        fake_upstream.set_json(completion_body("ok"))
        member = Member(seeded.user.id, seeded.group.id, seeded.secret, "")
        # The deployment's member sees nothing of it, with or without the
        # capability of her own.
        for holds in (False, True):
            await set_flag(session_factory, member, byok=holds)
            listed = await client.get("/v1/models", headers=seeded.auth)
            assert not any("mine" in m["id"] for m in listed.json()["data"])
            assert (await chat(client, member, "mine")).status_code == 404
            assert (await chat(client, member, "my/mine")).status_code == (404 if holds else 403)
        assert fake_upstream.bodies == []

    async def test_a_deployment_model_cannot_use_a_private_provider_nor_the_reverse(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        seeded: Seeded,
        alice: Member,
    ) -> None:
        theirs = await new_provider(app, client, session_factory, alice)
        as_user(app, await make_admin(session_factory, seeded))
        # Create and repoint a deployment model at a private provider: "no such provider".
        created = await client.post(
            "/api/admin/models",
            json={"name": "dep", "upstream_model": "u", "provider_id": theirs["id"]},
        )
        assert created.status_code == 404
        deployment = await client.get("/api/admin/models")
        existing = deployment.json()["items"][0]["id"]
        moved = await client.patch(
            f"/api/admin/models/{existing}", json={"provider_id": theirs["id"]}
        )
        assert moved.status_code == 404
        # A private model on a deployment provider.
        await acting(app, session_factory, alice)
        crossing = await client.post(
            "/api/me/models",
            json={"name": "x", "upstream_model": "u", "provider_id": str(seeded.provider.id)},
        )
        assert crossing.status_code == 404
        assert (await client.get("/api/me/providers")).json()["total"] == 1

    async def test_a_forced_cross_owner_row_is_unusable(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        seeded: Seeded,
        alice: Member,
        fake_upstream: FakeUpstream,
    ) -> None:
        """The access predicate re-checks what the write path enforces."""
        theirs = await own(app, client, session_factory, alice)
        async with session_factory() as session:
            model = await session.get(ModelDef, uuid.UUID(theirs.model["id"]))
            assert model is not None
            model.provider_id = seeded.provider.id  # a private model on a deployment provider
            await session.commit()
        fake_upstream.set_json(completion_body("ok"))
        assert (await chat(client, alice, "my/mine")).status_code == 404
        assert fake_upstream.bodies == []

    async def test_an_alias_cannot_target_a_private_model(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        seeded: Seeded,
        alice: Member,
    ) -> None:
        theirs = await own(app, client, session_factory, alice)
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/aliases",
            json={"name": "fast", "target_model_id": theirs.model["id"]},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "unknown_target"
        # And an alias row forced onto one reads as unavailable, never served.
        from gateway.access import AliasUnavailable, alias_unavailable_reason

        async with session_factory() as session:
            private = (
                await session.execute(select(ModelDef).where(ModelDef.owner_id.is_not(None)))
            ).scalar_one()
            alias = ModelDef(name="fast", is_alias=True)
            assert alias_unavailable_reason(alias, private) is AliasUnavailable.TARGET_DELETED


# --------------------------------------------------------------------------
# naming and resolution
# --------------------------------------------------------------------------


class TestNaming:
    async def test_the_listing_and_the_cards_carry_my_prefix_and_byok(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        seeded: Seeded,
        alice: Member,
    ) -> None:
        await own(app, client, session_factory, alice)
        await set_grants(session_factory, seeded, alice)
        cards = {
            m["id"]: m for m in (await client.get("/v1/models", headers=alice.auth)).json()["data"]
        }
        assert cards["my/mine"]["byok"] is True
        assert cards["my/mine"]["kind"] == "chat"
        assert cards["test-model"]["byok"] is False
        retrieved = await client.get("/v1/models/my/mine", headers=alice.auth)
        assert retrieved.status_code == 200
        assert retrieved.json()["id"] == "my/mine"
        assert retrieved.json()["byok"] is True

    async def test_my_name_resolves_to_the_callers_own_and_the_bare_name_to_the_deployment(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
    ) -> None:
        # A private model deliberately named like the deployment's.
        member = await add_member(session_factory, "carol")
        await set_grants(session_factory, seeded, member)
        provider = await new_provider(app, client, session_factory, member)
        await new_model(app, client, session_factory, member, provider["id"], "test-model")
        fake_upstream.set_json(completion_body("ok", model="vendor/test-model"))
        mine = await chat(client, member, "my/test-model")
        assert mine.status_code == 200
        assert mine.json()["model"] == "my/test-model"
        assert fake_upstream.last_body["model"] == "vendor/test-model"
        assert fake_upstream.urls[-1].startswith(PUBLIC_HOST)
        deployment = await chat(client, member, "test-model")
        assert deployment.status_code == 200
        assert deployment.json()["model"] == "test-model"
        assert fake_upstream.last_body["model"] == "upstream/test-model"
        assert fake_upstream.urls[-1].startswith("http://fake-upstream")

    async def test_the_stored_name_never_carries_the_prefix_and_may_not_start_with_it(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        seeded: Seeded,
        alice: Member,
    ) -> None:
        provider = await new_provider(app, client, session_factory, alice)
        response = await client.post(
            "/api/me/models",
            json={"name": "my/x", "upstream_model": "u", "provider_id": provider["id"]},
        )
        assert response.status_code == 400
        assert "my/" in response.text
        model = await new_model(app, client, session_factory, alice, provider["id"], "plain")
        assert model["name"] == "plain"
        as_user(app, await make_admin(session_factory, seeded))
        for url, body in [
            (
                "/api/admin/models",
                {"name": "my/x", "upstream_model": "u", "provider_id": str(seeded.provider.id)},
            ),
            ("/api/admin/aliases", {"name": "my/fast", "target_model_id": str(seeded.model.id)}),
        ]:
            assert (await client.post(url, json=body)).status_code == 400, url

    async def test_every_v1_surface_addresses_it_the_same_way(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        alice: Member,
        fake_upstream: FakeUpstream,
    ) -> None:
        await own(app, client, session_factory, alice)
        fake_upstream.set_json(
            {
                "id": "resp",
                "object": "response",
                "model": "vendor/mine",
                "output": [],
                "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
            }
        )
        responses = await client.post(
            "/v1/responses", headers=alice.auth, json={"model": "my/mine", "input": "hi"}
        )
        assert responses.status_code == 200, responses.text
        assert responses.json()["model"] == "my/mine"
        # A streamed chat names it too.
        from helpers import chunk, sse, usage_only_frame

        fake_upstream.set_stream(
            [
                sse(chunk("hel", model="vendor/mine")),
                sse(chunk("lo", finish_reason="stop", model="vendor/mine")),
                sse(usage_only_frame(10, 5, "vendor/mine")),
                sse("[DONE]"),
            ]
        )
        streamed = await client.post(
            "/v1/chat/completions",
            headers=alice.auth,
            json={
                "model": "my/mine",
                "stream": True,
                "stream_options": {"include_usage": True},
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert streamed.status_code == 200
        assert '"model": "my/mine"' in streamed.text or '"model":"my/mine"' in streamed.text


class TestOtherSurfaces:
    async def test_embeddings_are_addressed_and_recorded_the_same_way(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        alice: Member,
        fake_upstream: FakeUpstream,
    ) -> None:
        provider = await new_provider(app, client, session_factory, alice)
        await new_model(
            app, client, session_factory, alice, provider["id"], "emb", kind="embedding"
        )
        fake_upstream.set_json(
            {
                "object": "list",
                "model": "vendor/emb",
                "data": [{"index": 0, "object": "embedding", "embedding": [0.1, 0.2]}],
                "usage": {"prompt_tokens": 10, "total_tokens": 10},
            }
        )
        response = await client.post(
            "/v1/embeddings", headers=alice.auth, json={"model": "my/emb", "input": "x"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["model"] == "my/emb"
        async with session_factory() as session:
            row = (await session.execute(select(UsageRecord))).scalar_one()
        assert row.byok is True and row.model_name == "my/emb" and row.group_id is None
        # And its kind is still enforced on the other surfaces, naming the name sent.
        wrong = await chat(client, alice, "my/emb")
        assert wrong.status_code == 400
        assert "my/emb" in wrong.json()["error"]["message"]


async def set_grants(session_factory: Any, seeded: Seeded, member: Member) -> None:
    """Let a member use the deployment's test model too (a grant to their group)."""
    async with session_factory() as session:
        session.add(GroupModelAccess(group_id=member.group_id, model_id=seeded.model.id))
        await session.commit()


# --------------------------------------------------------------------------
# what a member may add
# --------------------------------------------------------------------------


class TestWhatMayBeAdded:
    async def test_only_language_model_providers(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        alice: Member,
    ) -> None:
        await acting(app, session_factory, alice)
        plugins = (await client.get("/api/me/provider-plugins")).json()
        assert plugins and {p["kind"] for p in plugins} <= {"provider", "router"}
        assert not any(
            p["name"] in ("extractor", "linkup", "exa", "jina", "duckduckgo") for p in plugins
        )
        for body in (
            {"name": "s", "plugin": "linkup", "kind": "search", "api_key": "k"},
            {"name": "s", "plugin": "extractor", "base_url": PUBLIC_HOST},
            {"name": "s", "plugin": "exa", "api_key": "k"},
            {"name": "s", "kind": "search", "base_url": PUBLIC_HOST},
            {"name": "s", "kind": "internal", "base_url": PUBLIC_HOST},
        ):
            response = await client.post(
                "/api/me/providers", json={"base_url": PUBLIC_HOST, **body}
            )
            assert response.status_code in (400, 422), (body, response.text)
        ok = await client.post(
            "/api/me/providers",
            json={
                "name": "a",
                "plugin": "anthropic",
                "api_key": "k",
                "base_url": "https://api.example.com",
            },
        )
        assert ok.status_code == 201, ok.text
        assert ok.json()["kind"] == "provider"

    async def test_headers_that_interfere_with_the_request_are_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        alice: Member,
    ) -> None:
        await acting(app, session_factory, alice)
        for header in ("Host", "content-length", "X-Forwarded-For", "transfer-encoding"):
            response = await client.post(
                "/api/me/providers",
                json={"name": "h", "base_url": PUBLIC_HOST, "extra_headers": {header: "x"}},
            )
            assert response.status_code == 400, header
        good = await client.post(
            "/api/me/providers",
            json={"name": "h", "base_url": PUBLIC_HOST, "extra_headers": {"x-team": "a"}},
        )
        assert good.status_code == 201

    async def test_chat_embedding_image_transcription_only_and_never_public(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        alice: Member,
    ) -> None:
        provider = await new_provider(app, client, session_factory, alice)
        for kind in ("chat", "embedding", "image", "transcription"):
            response = await client.post(
                "/api/me/models",
                json={
                    "name": f"m-{kind}",
                    "upstream_model": "u",
                    "provider_id": provider["id"],
                    "kind": kind,
                },
            )
            assert response.status_code == 201, (kind, response.text)
        for kind in ("ocr", "decision", "search"):
            response = await client.post(
                "/api/me/models",
                json={
                    "name": f"x-{kind}",
                    "upstream_model": "u",
                    "provider_id": provider["id"],
                    "kind": kind,
                },
            )
            assert response.status_code == 400, (kind, response.text)
            assert response.json()["error"]["code"] == "byok_kind_not_allowed"
        public = await client.post(
            "/api/me/models",
            json={
                "name": "pub",
                "upstream_model": "u",
                "provider_id": provider["id"],
                "is_public": True,
            },
        )
        assert public.status_code == 400
        model = (await client.get("/api/me/models")).json()["items"][0]
        patched = await client.patch(f"/api/me/models/{model['id']}", json={"is_public": True})
        assert patched.status_code == 400
        # A model's kind cannot be edited into a forbidden one either.
        patched = await client.patch(f"/api/me/models/{model['id']}", json={"kind": "ocr"})
        assert patched.status_code == 400

    async def test_names_are_unique_per_owner_not_globally(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        seeded: Seeded,
        alice: Member,
        bob: Member,
    ) -> None:
        await own(app, client, session_factory, alice, "shared-name")
        await own(app, client, session_factory, bob, "shared-name")  # a second owner: fine
        # A deployment row may share a private row's name in both tables.
        async with session_factory() as session:
            dupes = await session.scalar(
                select(func.count()).select_from(Provider).where(Provider.name == "shared-name")
            )
            assert dupes == 2
        # But one owner cannot repeat a name.
        await acting(app, session_factory, alice)
        provider = (await client.get("/api/me/providers")).json()["items"][0]
        again = await client.post(
            "/api/me/providers", json={"name": "shared-name", "base_url": PUBLIC_HOST}
        )
        assert again.status_code == 409
        again = await client.post(
            "/api/me/models",
            json={"name": "shared-name", "upstream_model": "u2", "provider_id": provider["id"]},
        )
        assert again.status_code == 409
        # And a deployment model may carry a private model's short name.
        as_user(app, await make_admin(session_factory, seeded))
        created = await client.post(
            "/api/admin/models",
            json={
                "name": "shared-name",
                "upstream_model": "u",
                "provider_id": str(seeded.provider.id),
            },
        )
        assert created.status_code == 201, created.text
        clash = await client.post(
            "/api/admin/models",
            json={
                "name": "shared-name",
                "upstream_model": "u",
                "provider_id": str(seeded.provider.id),
            },
        )
        assert clash.status_code == 409

    async def test_a_provider_with_models_cannot_be_deleted_then_can(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        alice: Member,
    ) -> None:
        theirs = await own(app, client, session_factory, alice)
        refused = await client.delete(f"/api/me/providers/{theirs.provider['id']}")
        assert refused.status_code == 409
        assert (await client.delete(f"/api/me/models/{theirs.model['id']}")).status_code == 204
        assert (
            await client.delete(f"/api/me/providers/{theirs.provider['id']}")
        ).status_code == 204
        assert (await client.get("/api/me/providers")).json()["total"] == 0

    async def test_there_is_a_ceiling_per_account(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        alice: Member,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("gateway.byok.MAX_PRIVATE_PROVIDERS", 2)
        await new_provider(app, client, session_factory, alice, "p1")
        await new_provider(app, client, session_factory, alice, "p2")
        response = await client.post(
            "/api/me/providers", json={"name": "p3", "base_url": PUBLIC_HOST}
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "byok_limit_reached"


# --------------------------------------------------------------------------
# the credential
# --------------------------------------------------------------------------


class TestCredential:
    async def test_it_is_encrypted_at_rest_and_in_no_response(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        alice: Member,
        fake_upstream: FakeUpstream,
    ) -> None:
        provider = await new_provider(app, client, session_factory, alice)
        texts = [
            (await client.get("/api/me/providers")).text,
            (
                await client.patch(
                    f"/api/me/providers/{provider['id']}", json={"description": "edited"}
                )
            ).text,
        ]
        for text in texts:
            assert USER_KEY not in text
        assert "api_key_encrypted" not in "".join(texts)
        async with session_factory() as session:
            row = await session.get(Provider, uuid.UUID(provider["id"]))
            assert row is not None
            assert row.api_key_encrypted and USER_KEY not in row.api_key_encrypted
            assert BOX.decrypt(row.api_key_encrypted) == USER_KEY
            assert row.owner_kind == "user"
        # And it is what the upstream receives.
        await new_model(app, client, session_factory, alice, provider["id"])
        fake_upstream.set_json(completion_body("ok"))
        assert (await chat(client, alice, "my/mine")).status_code == 200
        assert fake_upstream.headers[-1]["authorization"] == f"Bearer {USER_KEY}"

    async def test_rotating_and_clearing_use_the_same_three_way_convention(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        alice: Member,
    ) -> None:
        provider = await new_provider(app, client, session_factory, alice)
        assert provider["has_api_key"] is True
        cleared = await client.patch(f"/api/me/providers/{provider['id']}", json={"api_key": ""})
        assert cleared.json()["has_api_key"] is False
        rotated = await client.patch(
            f"/api/me/providers/{provider['id']}", json={"api_key": "new-key-9999"}
        )
        assert rotated.json()["has_api_key"] is True
        assert "new-key-9999" not in rotated.text

    async def test_private_edits_are_not_in_the_administrators_audit_log(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        alice: Member,
    ) -> None:
        theirs = await own(app, client, session_factory, alice)
        await client.patch(f"/api/me/providers/{theirs.provider['id']}", json={"description": "d"})
        await client.delete(f"/api/me/models/{theirs.model['id']}")
        async with session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(AdminAuditEvent)) == 0


# --------------------------------------------------------------------------
# prices, probe, discovery
# --------------------------------------------------------------------------


class TestPricesProbeDiscovery:
    async def test_price_rows_append_and_list(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        alice: Member,
    ) -> None:
        theirs = await own(app, client, session_factory, alice)
        mid = theirs.model["id"]
        second = await client.post(
            f"/api/me/models/{mid}/prices",
            json={"input_per_mtok": "3", "output_per_mtok": "4", "currency": "EUR"},
        )
        assert second.status_code == 201
        listed = (await client.get(f"/api/me/models/{mid}/prices")).json()
        assert listed["total"] == 2
        got = (await client.get(f"/api/me/models/{mid}")).json()
        assert got["current_price"]["input_per_mtok"] is not None

    async def test_probe_runs_the_providers_own_check(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        alice: Member,
        fake_upstream: FakeUpstream,
    ) -> None:
        provider = await new_provider(app, client, session_factory, alice)
        fake_upstream.set_json({"data": [{"id": "vendor/mine"}]})
        result = await client.post(f"/api/me/providers/{provider['id']}/test")
        assert result.status_code == 200
        assert result.json()["ok"] is True
        assert fake_upstream.headers[-1]["authorization"] == f"Bearer {USER_KEY}"

    async def test_discovery_and_import_read_the_providers_own_models_endpoint(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        alice: Member,
    ) -> None:
        provider = await new_provider(app, client, session_factory, alice)
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "vendor/alpha",
                            "pricing": {"input": 1, "output": 2},
                            "input_cost_per_mtok": 1.0,
                            "output_cost_per_mtok": 2.0,
                        },
                        {"id": "vendor/beta"},
                    ]
                },
            )

        app.state.byok_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        discovered = await client.get(f"/api/me/models/discover?provider_id={provider['id']}")
        assert discovered.status_code == 200, discovered.text
        assert seen == [f"{PUBLIC_HOST}/models"]
        body = discovered.json()
        assert {m["upstream_model"] for m in body["available"]} | {
            m["upstream_model"] for m in body["catalogued"]
        } >= {"vendor/beta"} or body["provider_model_count"] >= 1
        imported = await client.post(
            f"/api/me/models/import?provider_id={provider['id']}",
            json={
                "models": [
                    {
                        "upstream_model": "vendor/beta",
                        "manual_price": {"input_per_mtok": "1", "output_per_mtok": "2"},
                    }
                ]
            },
        )
        assert imported.status_code == 201, imported.text
        assert imported.json()["results"][0]["imported"] is True
        async with session_factory() as session:
            row = (
                await session.execute(
                    select(ModelDef).where(ModelDef.upstream_model == "vendor/beta")
                )
            ).scalar_one()
            assert row.owner_id == alice.user_id and row.owner_kind == "user"
            assert row.name == "beta"
        # The imported model is hers, addressed my/beta.
        assert (await client.get("/api/me/models")).json()["total"] == 1

    async def test_a_catalogue_url_override_is_refused_for_a_private_provider(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        alice: Member,
    ) -> None:
        provider = await new_provider(app, client, session_factory, alice)
        response = await client.get(
            f"/api/me/models/discover?provider_id={provider['id']}&url=http://169.254.169.254/x"
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "byok_catalogue_url"

    async def test_discovery_refuses_a_host_that_has_since_gone_private(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        alice: Member,
        fake_dns: dict[str, list[str]],
    ) -> None:
        provider = await new_provider(app, client, session_factory, alice)
        fake_dns["api.example.com"] = ["10.0.0.9"]  # rebinding after creation
        called: list[str] = []
        app.state.byok_http = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: called.append(str(r.url)) or httpx.Response(200, json={"data": []})
            )
        )
        response = await client.get(f"/api/me/models/discover?provider_id={provider['id']}")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "provider_url_blocked"
        assert called == []


# --------------------------------------------------------------------------
# the network guard at the API
# --------------------------------------------------------------------------


class TestNetworkGuardAtTheApi:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:8000/v1",
            "http://localhost:11434/v1",
            "http://[::1]/v1",
            "http://169.254.169.254/latest",
            "http://10.0.0.5/v1",
            "http://172.20.0.3:8000/v1",
            "http://192.168.1.9/v1",
            "http://100.64.1.1/v1",
            "http://gateway:8000/v1",
            "http://postgres:5432/v1",
            "http://valkey:6379/v1",
            "http://redaction:8001/v1",
            "https://user:pw@api.example.com/v1",
            "ftp://api.example.com/v1",
        ],
    )
    async def test_create_refuses_internal_targets(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        alice: Member,
        url: str,
    ) -> None:
        await acting(app, session_factory, alice)
        response = await client.post("/api/me/providers", json={"name": "bad", "base_url": url})
        assert response.status_code == 400, (url, response.text)
        assert response.json()["error"]["code"] == "provider_url_blocked"
        assert (await client.get("/api/me/providers")).json()["total"] == 0

    async def test_a_public_name_that_resolves_privately_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        alice: Member,
        fake_dns: dict[str, list[str]],
    ) -> None:
        fake_dns["sneaky.example.com"] = ["192.168.0.4"]
        await acting(app, session_factory, alice)
        response = await client.post(
            "/api/me/providers", json={"name": "bad", "base_url": "https://sneaky.example.com/v1"}
        )
        assert response.status_code == 400
        assert "192.168.0.4" in response.json()["error"]["message"]

    async def test_update_refuses_a_blocked_url_and_keeps_the_old_one(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        alice: Member,
    ) -> None:
        provider = await new_provider(app, client, session_factory, alice)
        response = await client.patch(
            f"/api/me/providers/{provider['id']}", json={"base_url": "http://10.0.0.5/v1"}
        )
        assert response.status_code == 400
        got = (await client.get("/api/me/providers")).json()["items"][0]
        assert got["base_url"] == PUBLIC_HOST

    async def test_request_time_refuses_a_name_that_started_resolving_privately(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        alice: Member,
        fake_upstream: FakeUpstream,
        fake_dns: dict[str, list[str]],
    ) -> None:
        await own(app, client, session_factory, alice)
        fake_upstream.set_json(completion_body("ok"))
        assert (await chat(client, alice, "my/mine")).status_code == 200
        calls = len(fake_upstream.bodies)
        fake_dns["api.example.com"] = ["169.254.169.254"]
        refused = await chat(client, alice, "my/mine")
        assert refused.status_code == 502
        assert len(fake_upstream.bodies) == calls  # nothing was sent anywhere
        # And no usage was recorded for a request that never left.
        async with session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(UsageRecord)) == 1

    async def test_the_admin_switch_lifts_the_guard_and_applies_at_once(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        seeded: Seeded,
        alice: Member,
        fake_upstream: FakeUpstream,
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        switched = await client.put("/api/admin/oidc/policy", json={"byok_private_networks": True})
        assert switched.status_code == 200, switched.text
        assert switched.json()["byok_private_networks"] is True
        provider = await new_provider(
            app, client, session_factory, alice, "lab", base_url="http://10.0.0.5:8000/v1"
        )
        await new_model(app, client, session_factory, alice, provider["id"], "lab")
        fake_upstream.set_json(completion_body("ok"))
        assert (await chat(client, alice, "my/lab")).status_code == 200
        # Switched back off: the very next request is refused (the cached client
        # was built while it was on, and is not reused).
        as_user(app, await make_admin(session_factory, seeded))
        await client.put("/api/admin/oidc/policy", json={"byok_private_networks": False})
        assert (await chat(client, alice, "my/lab")).status_code == 502

    async def test_deployment_providers_are_never_guarded(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
    ) -> None:
        """`http://fake-upstream` is a single-label host; it works because it is
        the deployment's, which is the line the guard draws."""
        fake_upstream.set_json(completion_body("ok"))
        assert (
            await chat(
                client, Member(seeded.user.id, seeded.group.id, seeded.secret, ""), "test-model"
            )
        ).status_code == 200
        as_user(app, await make_admin(session_factory, seeded))
        created = await client.post(
            "/api/admin/providers", json={"name": "internal", "base_url": "http://postgres:5432/v1"}
        )
        assert created.status_code == 201


# --------------------------------------------------------------------------
# accounting: the ledger row, quotas, reports, the user's own usage
# --------------------------------------------------------------------------


async def add_rule(
    session_factory: Any,
    *,
    scope: LimitScope,
    scope_id: uuid.UUID | None,
    metric: LimitMetric,
    value: str,
) -> None:
    async with session_factory() as session:
        session.add(
            LimitRule(
                name=f"{scope.value}-{metric.value}",
                scope=scope,
                scope_id=scope_id,
                metric=metric,
                window_seconds=86400,
                limit_value=Decimal(value),
                is_active=True,
                applies_to=LimitAppliesTo.AGGREGATE,
            )
        )
        await session.commit()


class TestLedger:
    async def test_the_row_is_flagged_priced_at_the_owners_rows_and_has_no_group(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        alice: Member,
        fake_upstream: FakeUpstream,
    ) -> None:
        await own(app, client, session_factory, alice)
        fake_upstream.set_json(completion_body("ok", prompt=1_000_000, completion=500_000))
        assert (await chat(client, alice, "my/mine")).status_code == 200
        async with session_factory() as session:
            row = (await session.execute(select(UsageRecord))).scalar_one()
        assert row.byok is True
        assert row.group_id is None
        assert row.user_id == alice.user_id
        assert row.model_name == "my/mine"
        assert row.requested_model is None
        # 1 EUR / Mtok in + 2 EUR / Mtok out, the owner's own price rows.
        assert row.cost == Decimal("2")
        assert row.total_tokens == 1_500_000

    async def test_an_unpriced_private_model_simply_has_no_cost(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        alice: Member,
        fake_upstream: FakeUpstream,
    ) -> None:
        provider = await new_provider(app, client, session_factory, alice)
        await new_model(app, client, session_factory, alice, provider["id"], price=False)
        fake_upstream.set_json(completion_body("ok", prompt=1_000_000, completion=500_000))
        assert (await chat(client, alice, "my/mine")).status_code == 200
        async with session_factory() as session:
            row = (await session.execute(select(UsageRecord))).scalar_one()
        assert row.byok is True and row.cost == 0 and row.total_tokens == 1_500_000

    async def test_the_deployments_rows_are_unflagged_and_keep_their_group(
        self,
        client: httpx.AsyncClient,
        session_factory: Any,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
    ) -> None:
        fake_upstream.set_json(completion_body("ok"))
        response = await client.post(
            "/v1/chat/completions",
            headers=seeded.auth,
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200
        async with session_factory() as session:
            row = (await session.execute(select(UsageRecord))).scalar_one()
        assert row.byok is False and row.group_id == seeded.group.id

    async def test_redaction_applies_to_byok_traffic_like_any_other(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        alice: Member,
        fake_upstream: FakeUpstream,
    ) -> None:
        from test_embeddings import _ShoutyRedactor

        await own(app, client, session_factory, alice)
        app.state.redactor = _ShoutyRedactor()
        fake_upstream.set_json(completion_body("ok"))
        response = await client.post(
            "/v1/chat/completions",
            headers=alice.auth,
            json={
                "model": "my/mine",
                "messages": [{"role": "user", "content": "my name is Mario Rossi"}],
            },
        )
        assert response.status_code == 200
        sent = fake_upstream.last_body["messages"][0]["content"]
        assert "Mario Rossi" not in sent and "<PERSON_X>" in sent
        async with session_factory() as session:
            row = (await session.execute(select(UsageRecord))).scalar_one()
        assert row.redaction_engine == "test" and row.redacted_entity_count == 1

    async def test_a_blocked_request_is_recorded_as_byok_too(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        alice: Member,
        fake_upstream: FakeUpstream,
    ) -> None:
        from gateway.errors import ContentBlockedError

        class Blocker:
            name = "blocker"

            async def redact_request(self, messages: Any, *, policy: Any = None) -> Any:
                raise ContentBlockedError("blocked by policy")

        await own(app, client, session_factory, alice)
        app.state.redactor = Blocker()
        response = await chat(client, alice, "my/mine")
        assert response.status_code == 403
        async with session_factory() as session:
            row = (await session.execute(select(UsageRecord))).scalar_one()
        assert row.byok is True and row.group_id is None and row.model_name == "my/mine"
        assert fake_upstream.bodies == []


class TestQuotasAreUntouched:
    async def test_a_zero_cap_on_every_scope_does_not_stop_a_byok_request(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        seeded: Seeded,
        alice: Member,
        fake_upstream: FakeUpstream,
    ) -> None:
        await own(app, client, session_factory, alice)
        await set_grants(session_factory, seeded, alice)
        for scope, scope_id in (
            (LimitScope.GLOBAL, None),
            (LimitScope.GROUP, alice.group_id),
            (LimitScope.USER, alice.user_id),
        ):
            for metric in (LimitMetric.REQUESTS, LimitMetric.TOKENS, LimitMetric.COST):
                await add_rule(
                    session_factory, scope=scope, scope_id=scope_id, metric=metric, value="0"
                )
        fake_upstream.set_json(completion_body("ok", prompt=1_000_000, completion=500_000))
        # The deployment's model is refused...
        refused = await chat(client, alice, "test-model")
        assert refused.status_code == 429
        # ...and her own key is not.
        for _ in range(3):
            allowed = await chat(client, alice, "my/mine")
            assert allowed.status_code == 200, allowed.text

    async def test_no_counter_moves_and_the_ledger_derived_counters_ignore_it(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        seeded: Seeded,
        alice: Member,
        fake_upstream: FakeUpstream,
    ) -> None:
        await own(app, client, session_factory, alice)
        await add_rule(
            session_factory,
            scope=LimitScope.USER,
            scope_id=alice.user_id,
            metric=LimitMetric.TOKENS,
            value="1000000000",
        )
        await add_rule(
            session_factory,
            scope=LimitScope.GROUP,
            scope_id=alice.group_id,
            metric=LimitMetric.COST,
            value="1000",
        )
        fake_upstream.set_json(completion_body("ok", prompt=1_000_000, completion=500_000))
        for _ in range(2):
            assert (await chat(client, alice, "my/mine")).status_code == 200

        usage = (await client.get("/v1/pystino/usage", headers=alice.auth)).json()
        assert [rule["current_value"] for rule in usage["limits"]] == ["0", "0"] or all(
            Decimal(str(rule["current_value"])) == 0 for rule in usage["limits"]
        )
        # The ledger-derived view (what a cold cache rebuilds from) agrees.
        as_user(app, await make_admin(session_factory, seeded))
        health = await client.get("/api/admin/quota/health")
        assert health.status_code == 200, health.text
        windows = health.json()["windows"]
        assert windows
        for window in windows:
            assert Decimal(str(window["ledger_total"])) == 0, window
            assert Decimal(str(window["counter_value"] or 0)) == 0, window


class TestReports:
    async def _make_traffic(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        seeded: Seeded,
        alice: Member,
        fake_upstream: FakeUpstream,
    ) -> None:
        await own(app, client, session_factory, alice)
        await set_grants(session_factory, seeded, alice)
        fake_upstream.set_json(completion_body("ok", prompt=1_000_000, completion=500_000))
        assert (await chat(client, alice, "my/mine")).status_code == 200  # 2 EUR, byok
        assert (await chat(client, alice, "test-model")).status_code == 200  # 2 EUR, group

    async def test_admin_and_group_reports_exclude_it(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        seeded: Seeded,
        alice: Member,
        fake_upstream: FakeUpstream,
    ) -> None:
        await self._make_traffic(app, client, session_factory, seeded, alice, fake_upstream)
        as_user(app, await make_admin(session_factory, seeded))
        for group_by in ("total", "group", "user", "model", "provider", "day"):
            report = (await client.get(f"/api/admin/reports/usage?group_by={group_by}")).json()
            assert report["totals"]["requests"] == 1, group_by
            assert Decimal(str(report["totals"]["cost"])) == 2, group_by
            assert not any("my/" in (row["label"] or "") for row in report["rows"])
        csv = (await client.get("/api/admin/reports/usage.csv?group_by=model")).text
        assert "my/mine" not in csv
        by_group = (await client.get("/api/admin/usage")).json()
        assert sum(row["requests"] for row in by_group) == 1
        assert all(Decimal(str(row["cost"])) <= 2 for row in by_group)
        # The request log never lists it either.
        log = (await client.get("/api/admin/requests")).json()
        assert log["total"] == 1
        assert "my/mine" not in (await client.get("/api/admin/requests.csv")).text

    async def test_the_users_own_usage_shows_it_separately_with_existing_fields_unchanged(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        seeded: Seeded,
        alice: Member,
        fake_upstream: FakeUpstream,
    ) -> None:
        await self._make_traffic(app, client, session_factory, seeded, alice, fake_upstream)
        usage = (await client.get("/v1/pystino/usage", headers=alice.auth)).json()
        assert usage["usage"]["requests"] == 1  # the group-charged request only
        assert Decimal(str(usage["usage"]["cost"])) == 2
        assert usage["usage"]["own_keys"]["requests"] == 1
        assert Decimal(str(usage["usage"]["own_keys"]["cost"])) == 2
        assert usage["usage"]["own_keys"]["total_tokens"] == 1_500_000
        assert all("own_keys" not in v for v in usage["groups"].values())
        group_usage = next(iter(usage["groups"].values()))
        assert group_usage["requests"] == 1
        await acting(app, session_factory, alice)
        mine = (await client.get("/api/me/usage")).json()
        assert mine["requests"] == 1 and mine["own_keys"]["requests"] == 1
        # Reports: the default is what quotas and groups see; `own_keys` is the other half.
        report = (await client.get("/api/me/reports/usage?group_by=model")).json()
        assert [row["label"] for row in report["rows"]] == ["test-model"]
        own_report = (await client.get("/api/me/reports/usage?group_by=model&own_keys=true")).json()
        assert [row["label"] for row in own_report["rows"]] == ["my/mine"]
        assert own_report["totals"]["requests"] == 1
        log = (await client.get("/api/me/requests")).json()
        assert [r["model"] for r in log["items"]] == ["test-model"]
        own_log = (await client.get("/api/me/requests?own_keys=true")).json()
        assert [r["model"] for r in own_log["items"]] == ["my/mine"]

    async def test_another_user_never_sees_it_in_their_usage(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        seeded: Seeded,
        alice: Member,
        bob: Member,
        fake_upstream: FakeUpstream,
    ) -> None:
        await self._make_traffic(app, client, session_factory, seeded, alice, fake_upstream)
        usage = (await client.get("/v1/pystino/usage", headers=bob.auth)).json()
        assert usage["usage"]["own_keys"]["requests"] == 0
        assert usage["usage"]["requests"] == 0


# --------------------------------------------------------------------------
# administrators
# --------------------------------------------------------------------------


class TestAdministratorsView:
    async def test_it_lists_owners_hosts_and_usage_and_never_the_credential(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        seeded: Seeded,
        alice: Member,
        fake_upstream: FakeUpstream,
    ) -> None:
        theirs = await own(app, client, session_factory, alice)
        fake_upstream.set_json(completion_body("ok", prompt=1_000_000, completion=500_000))
        assert (await chat(client, alice, "my/mine")).status_code == 200
        as_user(app, await make_admin(session_factory, seeded))
        providers = await client.get("/api/admin/byok/providers")
        models = await client.get("/api/admin/byok/models")
        assert providers.status_code == 200 and models.status_code == 200
        item = providers.json()["items"][0]
        assert item["owner"]["email"] == "alice@example.org"
        assert item["owner"]["name"] == "Alice"
        assert item["name"] == "mine"
        assert item["base_url_host"] == "api.example.com"
        assert item["has_api_key"] is True
        assert item["model_count"] == 1
        assert item["usage"]["requests"] == 1
        assert item["usage"]["total_tokens"] == 1_500_000
        assert item["usage"]["last_used_at"] is not None
        model = models.json()["items"][0]
        assert model["api_name"] == "my/mine" and model["name"] == "mine"
        assert model["provider_name"] == "mine" and model["upstream_model"] == "vendor/mine"
        assert model["usage"]["requests"] == 1
        # No credential, hint, ciphertext, header values or full URL anywhere.
        async with session_factory() as session:
            row = await session.get(Provider, uuid.UUID(theirs.provider["id"]))
            assert row is not None
            hint, cipher = row.api_key_hint, row.api_key_encrypted
        for text in (providers.text, models.text):
            assert USER_KEY not in text
            assert cipher not in text
            assert hint not in text or not hint
            assert "api_key" not in text.replace("has_api_key", "")
            assert PUBLIC_HOST not in text
        # Filters.
        assert (await client.get("/api/admin/byok/providers?q=alice")).json()["total"] == 1
        assert (await client.get("/api/admin/byok/providers?q=nobody")).json()["total"] == 0
        assert (await client.get("/api/admin/byok/models?q=vendor")).json()["total"] == 1

    async def test_disable_and_enable_a_provider_and_a_model(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        seeded: Seeded,
        alice: Member,
        fake_upstream: FakeUpstream,
    ) -> None:
        theirs = await own(app, client, session_factory, alice)
        fake_upstream.set_json(completion_body("ok"))
        assert (await chat(client, alice, "my/mine")).status_code == 200

        admin = await make_admin(session_factory, seeded)
        as_user(app, admin)
        held = await client.post(f"/api/admin/byok/providers/{theirs.provider['id']}/disable")
        assert held.status_code == 200, held.text
        assert held.json()["admin_disabled"] is True
        assert (await chat(client, alice, "my/mine")).status_code == 404
        # The owner cannot undo it: her own switch is a different flag.
        await acting(app, session_factory, alice)
        reactivated = await client.patch(
            f"/api/me/providers/{theirs.provider['id']}", json={"is_active": True}
        )
        assert reactivated.status_code == 200
        assert (await chat(client, alice, "my/mine")).status_code == 404
        probe = await client.post(f"/api/me/providers/{theirs.provider['id']}/test")
        assert probe.json()["ok"] is False and "administrator" in probe.json()["detail"]
        # Kept, and released on request.
        as_user(app, admin)
        released = await client.post(f"/api/admin/byok/providers/{theirs.provider['id']}/enable")
        assert released.json()["admin_disabled"] is False
        assert (await chat(client, alice, "my/mine")).status_code == 200

        held_model = await client.post(f"/api/admin/byok/models/{theirs.model['id']}/disable")
        assert held_model.json()["admin_disabled"] is True
        assert (await chat(client, alice, "my/mine")).status_code == 404
        await acting(app, session_factory, alice)
        await client.patch(f"/api/me/models/{theirs.model['id']}", json={"is_active": True})
        assert (await chat(client, alice, "my/mine")).status_code == 404
        as_user(app, admin)
        await client.post(f"/api/admin/byok/models/{theirs.model['id']}/enable")
        assert (await chat(client, alice, "my/mine")).status_code == 200

        async with session_factory() as session:
            actions = [
                row.action
                for row in (
                    await session.execute(
                        select(AdminAuditEvent).order_by(AdminAuditEvent.created_at)
                    )
                )
                .scalars()
                .all()
            ]
        assert actions == [
            "byok.provider.disable",
            "byok.provider.enable",
            "byok.model.disable",
            "byok.model.enable",
        ]

    async def test_only_administrators_and_only_private_rows(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        seeded: Seeded,
        alice: Member,
    ) -> None:
        theirs = await own(app, client, session_factory, alice)
        for url in ("/api/admin/byok/providers", "/api/admin/byok/models"):
            assert (await client.get(url)).status_code == 403  # alice is not an admin
        assert (
            await client.post(f"/api/admin/byok/providers/{theirs.provider['id']}/disable")
        ).status_code == 403
        as_user(app, await make_admin(session_factory, seeded))
        deployment_provider = str(seeded.provider.id)
        deployment_model = str(seeded.model.id)
        assert (
            await client.post(f"/api/admin/byok/providers/{deployment_provider}/disable")
        ).status_code == 404
        assert (
            await client.post(f"/api/admin/byok/models/{deployment_model}/disable")
        ).status_code == 404
        assert not any(
            item["name"] == "fake"
            for item in (await client.get("/api/admin/byok/providers")).json()["items"]
        )


# --------------------------------------------------------------------------
# erasure and merge
# --------------------------------------------------------------------------


class TestErasureAndMerge:
    async def test_deleting_a_user_removes_their_private_rows_and_keeps_the_ledger(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        seeded: Seeded,
        alice: Member,
        bob: Member,
        fake_upstream: FakeUpstream,
    ) -> None:
        from pydantic import SecretStr

        await own(app, client, session_factory, alice, "alice-one")
        await own(app, client, session_factory, bob, "bob-one")
        fake_upstream.set_json(completion_body("ok"))
        assert (await chat(client, alice, "my/alice-one")).status_code == 200

        def chat_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"counts": {}, "shared": []})

        app.state.control_http = httpx.AsyncClient(transport=httpx.MockTransport(chat_handler))
        app.state.settings.chat.erasure_url = "http://chat/internal/erasure"
        app.state.settings.chat.erasure_token = SecretStr("t")
        as_user(app, await make_admin(session_factory, seeded))
        preview = await client.get(f"/api/admin/users/{alice.user_id}/delete-preview")
        assert preview.json()["gateway_counts"]["models"] == 1
        assert preview.json()["gateway_counts"]["providers"] == 1
        deleted = await client.request(
            "DELETE", f"/api/admin/users/{alice.user_id}", json={"confirm_shared_loss": True}
        )
        assert deleted.status_code in (200, 202), deleted.text
        async with session_factory() as session:
            names = {row.name for row in (await session.execute(select(ModelDef))).scalars().all()}
            providers = {
                row.name for row in (await session.execute(select(Provider))).scalars().all()
            }
            assert "alice-one" not in names and "alice-one" not in providers
            assert "bob-one" in names and "bob-one" in providers
            ledger = (await session.execute(select(UsageRecord))).scalar_one()
        # The ledger row stays, anonymised like any other.
        assert ledger.user_id is None and ledger.model_id is None
        assert ledger.byok is True and ledger.model_name == "my/alice-one"

    async def test_merge_drops_the_source_accounts_private_rows(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        seeded: Seeded,
        alice: Member,
        bob: Member,
    ) -> None:
        await own(app, client, session_factory, alice, "alice-one")
        await own(app, client, session_factory, bob, "bob-one")
        as_user(app, await make_admin(session_factory, seeded))
        preview = await client.get(
            f"/api/admin/users/{alice.user_id}/merge-preview?into={bob.user_id}"
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["counts"]["models"] == 1
        assert preview.json()["counts"]["providers"] == 1
        merged = await client.post(
            f"/api/admin/users/{alice.user_id}/merge",
            json={"into": str(bob.user_id), "confirm": "alice@example.org"},
        )
        assert merged.status_code == 200, merged.text
        async with session_factory() as session:
            names = {row.name for row in (await session.execute(select(ModelDef))).scalars().all()}
            assert "alice-one" not in names and "bob-one" in names


# --------------------------------------------------------------------------
# a user with the capability and no billing group
# --------------------------------------------------------------------------


async def drop_default_group(session_factory: Any, member: Member) -> None:
    """No billing group that resolves: the default is cleared (the key pins one
    too, so it is cleared as well)."""
    async with session_factory() as session:
        user = await session.get(User, member.user_id)
        assert user is not None
        user.default_billing_group_id = None
        key = (
            await session.execute(select(ApiKey).where(ApiKey.user_id == member.user_id))
        ).scalar_one()
        key.billing_group_id = None
        await session.commit()


class TestNoBillingGroup:
    async def test_own_models_work_and_bill_no_group(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        seeded: Seeded,
        alice: Member,
        fake_upstream: FakeUpstream,
    ) -> None:
        await own(app, client, session_factory, alice)
        await set_grants(session_factory, seeded, alice)
        await drop_default_group(session_factory, alice)
        fake_upstream.set_json(completion_body("ok"))

        response = await chat(client, alice, "my/mine")
        assert response.status_code == 200, response.text
        async with session_factory() as session:
            row = (await session.execute(select(UsageRecord))).scalar_one()
        assert row.byok is True and row.group_id is None and row.user_id == alice.user_id

        # The listing and the retrieval show her own models, never the deployment's
        # (asking for one is refused, so offering it would be a lie).
        listed = (await client.get("/v1/models", headers=alice.auth)).json()["data"]
        assert [card["id"] for card in listed] == ["my/mine"]
        assert (await client.get("/v1/models/my/mine", headers=alice.auth)).status_code == 200
        assert (await client.get("/v1/models/test-model", headers=alice.auth)).status_code == 404

    async def test_a_deployment_model_still_needs_a_group_and_says_so_as_before(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: Any,
        seeded: Seeded,
        alice: Member,
    ) -> None:
        await own(app, client, session_factory, alice)
        await set_grants(session_factory, seeded, alice)
        await drop_default_group(session_factory, alice)

        response = await chat(client, alice, "test-model")
        assert response.status_code == 403
        assert "No billing group is set" in response.json()["error"]["message"]
        # And the routes that bill a group refuse the same way.
        assert (await client.get("/v1/billing/groups", headers=alice.auth)).status_code == 403

    async def test_without_the_capability_no_group_is_refused_at_the_door(
        self,
        client: httpx.AsyncClient,
        session_factory: Any,
        seeded: Seeded,
    ) -> None:
        plain = await add_member(session_factory, "plain", byok=False)
        await drop_default_group(session_factory, plain)

        for model in ("test-model", "my/anything"):
            response = await chat(client, plain, model)
            assert response.status_code == 403
        assert (await client.get("/v1/models", headers=plain.auth)).status_code == 403
