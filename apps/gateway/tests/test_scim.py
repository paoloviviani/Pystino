"""Inbound SCIM 2.0: a directory pushes, the same engine applies (ADR 0088 draft)."""

from __future__ import annotations

import httpx
from conftest import Seeded
from gateway.models import GroupSync, IdentityProvider, User
from gateway.secrets import SecretBox
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin

ISS = "https://login.microsoftonline.com/tenant/v2.0"
SCIM = "application/scim+json"


async def _setup(
    app: object, client: httpx.AsyncClient, seeded: Seeded, session_factory
) -> tuple[str, dict]:
    as_user(app, await make_admin(session_factory, seeded))
    # The row is a projection of the environment now (ADR 0093 §14): the
    # `POST`/`PUT /admin/identity-providers` this used to go through are
    # removed, so the fixture writes the row directly, exactly as a re-seed
    # from `OIDC_KIND=entra`, `OIDC_SYNC_ADAPTER=scim` etc. would.
    box: SecretBox = app.state.secrets  # type: ignore[attr-defined]
    async with session_factory() as session:
        row = IdentityProvider(
            name="entra",
            issuer=ISS,
            client_id="c",
            client_secret_encrypted=box.encrypt("s"),
            scopes=["openid", "profile", "email"],
            kind="entra",
            subject_claim="oid",
            # The directory is authoritative for groups: every push applies.
            sync_adapter="scim",
            group_source="directory",
            group_sync=GroupSync.EVERY_LOGIN,
            is_enabled=True,
        )
        session.add(row)
        await session.commit()
        pid = str(row.id)
    minted = (await client.post(f"/api/admin/identity-providers/{pid}/scim-token")).json()
    assert minted["endpoint"].endswith("/scim/v2/entra")
    return pid, {"authorization": f"Bearer {minted['token']}", "content-type": SCIM}


async def test_a_pushed_user_exists_before_login_and_is_deprovisioned_not_deleted(
    app: object,
    client: httpx.AsyncClient,
    seeded: Seeded,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, headers = await _setup(app, client, seeded, session_factory)
    created = await client.post(
        "/scim/v2/entra/Users",
        headers=headers,
        json={
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "userName": "alice@corp.example",
            "externalId": "oid-1",
            "displayName": "Alice",
            "active": True,
            "emails": [{"value": "alice@corp.example", "primary": True}],
        },
    )
    assert created.status_code == 201, created.text
    assert created.headers["content-type"].startswith(SCIM)
    uid = created.json()["id"]
    async with session_factory() as session:
        user = (await session.execute(select(User).where(User.subject == "oid-1"))).scalar_one()
        assert user.issuer == ISS and user.is_active

    found = await client.get(
        '/scim/v2/entra/Users?filter=userName eq "alice@corp.example"', headers=headers
    )
    assert found.json()["totalResults"] == 1
    assert (
        await client.post(
            "/scim/v2/entra/Users",
            headers=headers,
            json={"userName": "alice@corp.example", "externalId": "oid-1"},
        )
    ).status_code == 409

    patched = await client.patch(
        f"/scim/v2/entra/Users/{uid}",
        headers=headers,
        json={
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "replace", "value": {"active": False}}],
        },
    )
    assert patched.json()["active"] is False
    async with session_factory() as session:
        user = (await session.execute(select(User).where(User.subject == "oid-1"))).scalar_one()
        assert not user.is_active and user.deactivated_by == "directory"

    assert (await client.delete(f"/scim/v2/entra/Users/{uid}", headers=headers)).status_code == 204
    assert (await client.get(f"/scim/v2/entra/Users/{uid}", headers=headers)).status_code == 404
    async with session_factory() as session:
        assert (await session.execute(select(User).where(User.subject == "oid-1"))).scalar_one()


async def test_pushed_group_membership_becomes_directory_groups(
    app: object,
    client: httpx.AsyncClient,
    seeded: Seeded,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, headers = await _setup(app, client, seeded, session_factory)
    # A pushed group name is a directory's name like a claimed one: under
    # group_import=manual (the default) it is listed for import, not created.
    # This test is about the push mechanics, so it runs in auto mode.
    app.state.settings.oidc.group_import = "auto"  # type: ignore[attr-defined]
    uid = (
        await client.post(
            "/scim/v2/entra/Users",
            headers=headers,
            json={"userName": "bob@corp.example", "externalId": "oid-2"},
        )
    ).json()["id"]
    group = await client.post(
        "/scim/v2/entra/Groups",
        headers=headers,
        json={"displayName": "engineering", "members": [{"value": uid}]},
    )
    assert group.status_code == 201, group.text
    gid = group.json()["id"]
    async with session_factory() as session:
        user = (await session.execute(select(User).where(User.subject == "oid-2"))).scalar_one()
        await session.refresh(user, attribute_names=["memberships"])
        assert {m.group.name for m in user.memberships} == {"engineering"}
    removed = await client.patch(
        f"/scim/v2/entra/Groups/{gid}",
        headers=headers,
        json={"Operations": [{"op": "remove", "path": f'members[value eq "{uid}"]'}]},
    )
    assert removed.json()["members"] == []
    async with session_factory() as session:
        user = (await session.execute(select(User).where(User.subject == "oid-2"))).scalar_one()
        await session.refresh(user, attribute_names=["memberships"])
        assert user.memberships == []


async def test_wrong_or_missing_tokens_are_one_answer(
    app: object,
    client: httpx.AsyncClient,
    seeded: Seeded,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _setup(app, client, seeded, session_factory)
    for headers in ({}, {"authorization": "Bearer scim_wrong"}):
        response = await client.get("/scim/v2/entra/Users", headers=headers)
        assert response.status_code == 401
    assert (
        await client.get("/scim/v2/nobody/Users", headers={"authorization": "Bearer x"})
    ).status_code == 401


async def test_discovery_documents(
    app: object,
    client: httpx.AsyncClient,
    seeded: Seeded,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, headers = await _setup(app, client, seeded, session_factory)
    config = (await client.get("/scim/v2/entra/ServiceProviderConfig", headers=headers)).json()
    assert config["patch"]["supported"] is True
    types = (await client.get("/scim/v2/entra/ResourceTypes", headers=headers)).json()
    assert {t["id"] for t in types["Resources"]} == {"User", "Group"}
