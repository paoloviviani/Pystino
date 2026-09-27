"""`POST /admin/users`, `/sign-in` and `/reset-password` (ADR 0093 §8.1/§8.2).

Route-level: these are the surface an administrator's browser actually
calls, and the interesting behaviour — the creation order, the compensating
delete, the stable login — is as much about what happens across the Authelia
file and the gateway rows together as about either one alone.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
import yaml
from conftest import Seeded
from fastapi import FastAPI
from gateway.deps import get_management_user
from gateway.models import (
    DirectoryEntry,
    Group,
    IdentityEvent,
    IdentityEventAction,
    IdentityProvider,
    Membership,
    User,
)
from gateway.oidc import PENDING_USER_ISSUER
from gateway.secrets import SecretBox
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

BOX = SecretBox(["test-encryption-key-not-for-production"])
ISS = "https://gw.example.org/authelia"

SEED = """users:
  admin:
    disabled: false
    displayname: Admin
    password: '$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA'
    email: admin@example.org
    groups: [users]
"""


def as_user(app: object, user: User) -> None:
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


async def _bundled_provider(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> tuple[IdentityProvider, Path]:
    path = tmp_path / "users_database.yml"
    path.write_text(SEED)
    async with session_factory() as session:
        row = IdentityProvider(
            name="authelia",
            issuer=ISS,
            client_id="pystino-console",
            client_secret_encrypted=BOX.encrypt("s"),
            scopes=["openid"],
            groups_claim="groups",
            fetch_userinfo=False,
            group_mappings=[],
            link_by_email=False,
            kind="authelia",
            group_source="none",
            group_sync="never",
            sync_adapter="none",
            admin_source="console",
            admin_values=[],
            sync_deprovision="disable",
            sync_create_users=True,
            sync_confirmed=False,
            sync_interval_minutes=60,
            sync_config_encrypted=BOX.encrypt(json.dumps({"path": str(path)})),
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row, path


class TestCreateUser:
    async def test_creates_in_order_and_returns_the_password_once(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        provider, path = await _bundled_provider(session_factory, tmp_path)

        response = await client.post(
            "/api/admin/users",
            json={
                "login": "frank",
                "display_name": "Frank",
                "email": "frank@example.org",
                "groups": [seeded.group.name],
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["password"]
        assert body["email"] == "frank@example.org"
        assert body["is_active"] is True
        assert body["is_admin"] is False
        # Stated outright (§8): the login bound to this person at the bundled
        # provider — which the console reads to offer Reset password rather
        # than Create sign-in, since a pending user's issuer and linked
        # identities answer "no login" and would offer the wrong action.
        assert body["bundled_login"] == "frank"

        stored = yaml.safe_load(path.read_text())["users"]["frank"]
        assert stored["groups"] == ["users"]
        from argon2 import PasswordHasher

        assert PasswordHasher().verify(stored["password"], body["password"])

        async with session_factory() as session:
            user = (
                await session.execute(select(User).where(User.email == "frank@example.org"))
            ).scalar_one()
            assert user.issuer == PENDING_USER_ISSUER
            assert user.admin_edited_fields == ["email"]
            memberships = (
                (await session.execute(select(Membership).where(Membership.user_id == user.id)))
                .scalars()
                .all()
            )
            assert {m.group_id for m in memberships} == {seeded.group.id}

            entry = (
                await session.execute(
                    select(DirectoryEntry).where(DirectoryEntry.provider_id == provider.id)
                )
            ).scalar_one()
            assert entry.external_id == "frank"
            assert entry.user_id == user.id

            events = (
                (
                    await session.execute(
                        select(IdentityEvent).where(IdentityEvent.target_user_id == user.id)
                    )
                )
                .scalars()
                .all()
            )
            assert {e.action for e in events} == {
                IdentityEventAction.USER_CREATE,
                IdentityEventAction.LOGIN_CREATE,
            }

    async def test_no_groups_picked_still_gets_a_default_billing_group(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """ADR 0093 to-do item 1: an operator who ticks no boxes still creates
        someone who can bill something, the same as Authelia's own `users`
        claim used to grant automatically before `group_source=none`."""
        as_user(app, await make_admin(session_factory, seeded))
        await _bundled_provider(session_factory, tmp_path)

        response = await client.post(
            "/api/admin/users",
            json={
                "login": "gina",
                "display_name": "Gina",
                "email": "gina@example.org",
                "groups": [],
            },
        )
        assert response.status_code == 201, response.text
        user_id = uuid.UUID(response.json()["id"])

        async with session_factory() as session:
            user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
            memberships = (
                (await session.execute(select(Membership).where(Membership.user_id == user.id)))
                .scalars()
                .all()
            )
            assert len(memberships) == 1
            group = (
                await session.execute(select(Group).where(Group.id == memberships[0].group_id))
            ).scalar_one()
            assert group.name == "users"
            assert user.default_billing_group_id == group.id

    async def test_a_login_that_already_exists_writes_nothing_gateway_side(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        await _bundled_provider(session_factory, tmp_path)

        response = await client.post(
            "/api/admin/users",
            json={"login": "admin", "display_name": "", "email": "new@example.org", "groups": []},
        )
        assert response.status_code == 400
        async with session_factory() as session:
            assert (
                await session.execute(select(User).where(User.email == "new@example.org"))
            ).scalar_one_or_none() is None

    async def test_a_failed_gateway_write_removes_the_authelia_entry_again(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Forces the gateway-side failure with a pre-existing bound
        DirectoryEntry for the same (provider, login) — the unique
        constraint the route's own insert then violates — rather than
        mocking anything: a real IntegrityError is what a second admin
        racing the same login name would actually produce."""
        as_user(app, await make_admin(session_factory, seeded))
        provider, path = await _bundled_provider(session_factory, tmp_path)
        async with session_factory() as session:
            other = User(issuer=PENDING_USER_ISSUER, subject="x", email="x@example.org")
            session.add(other)
            await session.flush()
            session.add(
                DirectoryEntry(
                    provider_id=provider.id, external_id="grace", username="grace", user_id=other.id
                )
            )
            await session.commit()

        response = await client.post(
            "/api/admin/users",
            json={"login": "grace", "display_name": "", "email": "grace@example.org", "groups": []},
        )
        assert response.status_code >= 400
        assert "grace" not in yaml.safe_load(path.read_text())["users"]


class TestCreateSignIn:
    async def test_binds_a_login_to_an_existing_user(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        provider, path = await _bundled_provider(session_factory, tmp_path)
        async with session_factory() as session:
            target = User(
                issuer="https://old.example.org", subject="old-sub", email="henry@example.org"
            )
            session.add(target)
            await session.commit()
            target_id = target.id

        response = await client.post(
            f"/api/admin/users/{target_id}/sign-in", json={"login": "henry"}
        )
        assert response.status_code == 201, response.text
        assert response.json()["password"]
        assert "henry" in yaml.safe_load(path.read_text())["users"]

        async with session_factory() as session:
            entry = (
                await session.execute(
                    select(DirectoryEntry).where(
                        DirectoryEntry.provider_id == provider.id,
                        DirectoryEntry.external_id == "henry",
                    )
                )
            ).scalar_one()
            assert entry.user_id == target_id
            # The existing identity is untouched: this only adds a door in.
            refreshed = (
                await session.execute(select(User).where(User.id == target_id))
            ).scalar_one()
            assert refreshed.issuer == "https://old.example.org"

    async def test_refuses_a_user_who_already_has_a_bundled_login(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """The Add-user case, accepted at the wrong door: the person was
        created with a login already bound to them (§8.2 step 4), but their
        pending state made the console offer Create sign-in. Creating a
        second one here would leave them two working passwords and the
        single-entry admin queries (reset, the disable sync) with an
        impossible answer."""
        as_user(app, await make_admin(session_factory, seeded))
        provider, path = await _bundled_provider(session_factory, tmp_path)
        created = await client.post(
            "/api/admin/users",
            json={"login": "lena", "display_name": "", "email": "lena@example.org", "groups": []},
        )
        assert created.status_code == 201

        response = await client.post(
            f"/api/admin/users/{created.json()['id']}/sign-in", json={"login": "lena-2"}
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["message"] == (
            "They already sign in as 'lena'. Use Reset password."
        )

        # Nothing was written anywhere: no second entry, no second login.
        async with session_factory() as session:
            entries = (
                (
                    await session.execute(
                        select(DirectoryEntry).where(
                            DirectoryEntry.provider_id == provider.id,
                            DirectoryEntry.user_id
                            == uuid.UUID(created.json()["id"]),
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert [entry.external_id for entry in entries] == ["lena"]
        assert set(yaml.safe_load(path.read_text())["users"]) == {"admin", "lena"}


class TestResetPassword:
    async def test_mints_a_fresh_password(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        _, path = await _bundled_provider(session_factory, tmp_path)
        created = await client.post(
            "/api/admin/users",
            json={"login": "ivan", "display_name": "", "email": "ivan@example.org", "groups": []},
        )
        user_id = created.json()["id"]

        response = await client.post(f"/api/admin/users/{user_id}/reset-password")
        assert response.status_code == 200, response.text
        new_password = response.json()["password"]

        from argon2 import PasswordHasher

        stored = yaml.safe_load(path.read_text())["users"]["ivan"]
        assert PasswordHasher().verify(stored["password"], new_password)

    async def test_works_for_a_signed_in_user_with_a_bound_login(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Disable and reset go through the one entry bound to the person —
        the queries these routes answer with `scalar_one_or_none`, which a
        duplicate binding (the old Create sign-in bug) turned into
        MultipleResultsFound. With the partial unique index in place, one
        person has one login and both routes keep working."""
        as_user(app, await make_admin(session_factory, seeded))
        _, path = await _bundled_provider(session_factory, tmp_path)
        created = await client.post(
            "/api/admin/users",
            json={"login": "mona", "display_name": "", "email": "mona@example.org", "groups": []},
        )
        user_id = created.json()["id"]

        # Their first sign-in: the pending pair is replaced by the real one
        # (§8.2), exactly what `bind_bundled_login` does.
        async with session_factory() as session:
            user = (
                await session.execute(select(User).where(User.id == uuid.UUID(user_id)))
            ).scalar_one()
            user.issuer = ISS
            user.subject = "mona-sub"
            await session.commit()

        disabled = await client.patch(f"/api/admin/users/{user_id}", json={"is_active": False})
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["authelia_sync"] is None
        assert yaml.safe_load(path.read_text())["users"]["mona"]["disabled"] is True

        response = await client.post(f"/api/admin/users/{user_id}/reset-password")
        assert response.status_code == 200, response.text
        assert response.json()["password"]

        # And the listing states the login outright.
        listing = await client.get("/api/admin/users", params={"q": "mona"})
        assert listing.json()["items"][0]["bundled_login"] == "mona"

    async def test_an_admin_edited_username_does_not_redirect_the_reset(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """The stable-login rule: resolution goes through
        `directory_entries.external_id`, never `users.username` — which an
        administrator can edit freely without it ever meaning "reset a
        different Authelia login"."""
        as_user(app, await make_admin(session_factory, seeded))
        _, path = await _bundled_provider(session_factory, tmp_path)
        created = await client.post(
            "/api/admin/users",
            json={"login": "judy", "display_name": "", "email": "judy@example.org", "groups": []},
        )
        user_id = created.json()["id"]

        # An administrator edits the profile's username field — a display
        # convenience, not the login.
        patched = await client.patch(f"/api/admin/users/{user_id}", json={"username": "renamed"})
        assert patched.status_code == 200, patched.text

        response = await client.post(f"/api/admin/users/{user_id}/reset-password")
        assert response.status_code == 200, response.text
        new_password = response.json()["password"]

        data = yaml.safe_load(path.read_text())["users"]
        assert "renamed" not in data
        from argon2 import PasswordHasher

        assert PasswordHasher().verify(data["judy"]["password"], new_password)


class TestPendingUserCannotBeAdmin:
    async def test_patch_refuses_is_admin_true_before_first_sign_in(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        await _bundled_provider(session_factory, tmp_path)
        created = await client.post(
            "/api/admin/users",
            json={"login": "karl", "display_name": "", "email": "karl@example.org", "groups": []},
        )
        user_id = created.json()["id"]

        response = await client.patch(f"/api/admin/users/{user_id}", json={"is_admin": True})
        assert response.status_code == 400
        assert "sign" in response.json()["error"]["message"].lower()

        async with session_factory() as session:
            row = (
                await session.execute(select(User).where(User.id == uuid.UUID(user_id)))
            ).scalar_one()
            assert row.is_admin is False
