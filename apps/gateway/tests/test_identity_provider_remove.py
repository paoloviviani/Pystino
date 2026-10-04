"""Removing a spent previous identity provider from the console.

`reseed_from_env` keeps the old provider's row, disabled and renamed
`previous-…`, so people who signed in there can be linked back. When nobody
ever did, that row is only clutter, and this is the one way to clear it. The
rules under test: only a disabled row, never one anything depends on, checked
and deleted in one transaction, and audited.
"""

from __future__ import annotations

import httpx
import pytest
from conftest import Seeded
from fastapi import FastAPI
from gateway.identity_registry import removal_blocker
from gateway.models import (
    DirectoryEntry,
    DirectorySyncRun,
    IdentityEvent,
    IdentityEventAction,
    IdentityProvider,
    User,
    UserIdentity,
)
from gateway.oidc import ProvisioningRefused, sign_in
from gateway.secrets import SecretBox
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin

OLD_ISSUER = "https://old-idp.example.org"
LIVE_ISSUER = "https://live-idp.example.org"


async def add_provider(
    session_factory: async_sessionmaker[AsyncSession],
    app: FastAPI,
    *,
    name: str,
    issuer: str,
    enabled: bool,
) -> IdentityProvider:
    box: SecretBox = app.state.secrets
    row = IdentityProvider(
        name=name,
        issuer=issuer,
        client_id="pystino",
        client_secret_encrypted=box.encrypt("s3cret"),
        scopes=["openid"],
        is_enabled=enabled,
    )
    async with session_factory() as session:
        session.add(row)
        await session.commit()
    return row


async def exists(session_factory: async_sessionmaker[AsyncSession], provider_id: object) -> bool:
    async with session_factory() as session:
        return await session.get(IdentityProvider, provider_id) is not None


@pytest.fixture
async def previous(
    app: FastAPI, session_factory: async_sessionmaker[AsyncSession]
) -> IdentityProvider:
    return await add_provider(
        session_factory, app, name="previous-20261004-ab12", issuer=OLD_ISSUER, enabled=False
    )


@pytest.fixture
async def admin(
    app: FastAPI, session_factory: async_sessionmaker[AsyncSession], seeded: Seeded
) -> User:
    user = await make_admin(session_factory, seeded)
    as_user(app, user)
    return user


class TestRemoval:
    async def test_a_disabled_unused_provider_is_removed_with_its_directory_rows(
        self,
        client: httpx.AsyncClient,
        admin: User,
        previous: IdentityProvider,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as session:
            session.add_all(
                [
                    DirectoryEntry(provider_id=previous.id, external_id="a"),
                    DirectoryEntry(provider_id=previous.id, external_id="b"),
                    DirectorySyncRun(provider_id=previous.id, trigger="manual"),
                ]
            )
            await session.commit()

        response = await client.delete(f"/api/admin/identity-providers/{previous.id}")

        assert response.status_code == 204
        assert response.content == b""
        assert not await exists(session_factory, previous.id)
        async with session_factory() as session:
            for model in (DirectoryEntry, DirectorySyncRun):
                assert (
                    await session.execute(select(func.count()).select_from(model))
                ).scalar_one() == 0
            event = (
                await session.execute(
                    select(IdentityEvent).where(
                        IdentityEvent.action == IdentityEventAction.IDP_REMOVE
                    )
                )
            ).scalar_one()
        assert event.actor_user_id == admin.id
        assert event.issuer == OLD_ISSUER
        assert event.detail == {"name": previous.name, "directory_entries": 2}

    async def test_removal_leaves_the_audit_history_of_that_issuer_alone(
        self,
        client: httpx.AsyncClient,
        admin: User,
        previous: IdentityProvider,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as session:
            session.add(
                IdentityEvent(
                    actor_type="system",
                    actor_label="system",
                    action=IdentityEventAction.IDP_RESEED,
                    issuer=OLD_ISSUER,
                )
            )
            await session.commit()
        assert (
            await client.delete(f"/api/admin/identity-providers/{previous.id}")
        ).status_code == 204
        async with session_factory() as session:
            kept = (
                await session.execute(
                    select(func.count())
                    .select_from(IdentityEvent)
                    .where(IdentityEvent.action == IdentityEventAction.IDP_RESEED)
                )
            ).scalar_one()
        assert kept == 1

    async def test_an_enabled_provider_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        admin: User,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        live = await add_provider(
            session_factory, app, name="default", issuer=LIVE_ISSUER, enabled=True
        )
        response = await client.delete(f"/api/admin/identity-providers/{live.id}")
        assert response.status_code == 409
        assert "environment" in response.json()["error"]["message"]
        assert await exists(session_factory, live.id)

    async def test_a_provider_with_a_user_at_its_issuer_is_kept(
        self,
        client: httpx.AsyncClient,
        admin: User,
        previous: IdentityProvider,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as session:
            session.add(User(issuer=OLD_ISSUER, subject="sub-1", email="a@example.org"))
            await session.commit()
        response = await client.delete(f"/api/admin/identity-providers/{previous.id}")
        assert response.status_code == 409
        assert "1 person signed in" in response.json()["error"]["message"]
        assert await exists(session_factory, previous.id)

    async def test_a_user_keyed_on_the_other_trailing_slash_spelling_still_blocks(
        self,
        client: httpx.AsyncClient,
        admin: User,
        previous: IdentityProvider,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as session:
            session.add(User(issuer=OLD_ISSUER + "/", subject="sub-1", email="a@example.org"))
            await session.commit()
        response = await client.delete(f"/api/admin/identity-providers/{previous.id}")
        assert response.status_code == 409

    async def test_a_linked_identity_at_its_issuer_is_kept(
        self,
        client: httpx.AsyncClient,
        admin: User,
        previous: IdentityProvider,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # The person's own account is at another issuer; only the linked
        # identity names this one, which `users.issuer` alone would not see.
        async with session_factory() as session:
            person = User(issuer="local", subject="p@example.org", email="p@example.org")
            session.add(person)
            await session.flush()
            session.add(UserIdentity(user_id=person.id, issuer=OLD_ISSUER, subject="linked"))
            await session.commit()
        response = await client.delete(f"/api/admin/identity-providers/{previous.id}")
        assert response.status_code == 409
        assert "1 person signed in" in response.json()["error"]["message"]
        assert await exists(session_factory, previous.id)

    async def test_a_person_with_both_rows_counts_once(
        self,
        session: AsyncSession,
        previous: IdentityProvider,
    ) -> None:
        person = User(issuer=OLD_ISSUER, subject="sub-1", email="a@example.org")
        session.add(person)
        await session.flush()
        session.add(UserIdentity(user_id=person.id, issuer=OLD_ISSUER, subject="sub-1b"))
        await session.commit()
        blocker = await removal_blocker(
            session, provider_id=previous.id, issuer=OLD_ISSUER, is_enabled=False
        )
        assert "1 person" in blocker

    async def test_a_directory_login_bound_to_an_account_is_kept(
        self,
        client: httpx.AsyncClient,
        admin: User,
        previous: IdentityProvider,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # A pending account has no issuer of this provider's, but its login
        # binding lives on the row the cascade would delete.
        async with session_factory() as session:
            session.add(DirectoryEntry(provider_id=previous.id, external_id="x", user_id=admin.id))
            await session.commit()
        response = await client.delete(f"/api/admin/identity-providers/{previous.id}")
        assert response.status_code == 409
        assert "bound" in response.json()["error"]["message"]
        assert await exists(session_factory, previous.id)

    async def test_an_unknown_provider_is_404(self, client: httpx.AsyncClient, admin: User) -> None:
        response = await client.delete(
            "/api/admin/identity-providers/00000000-0000-0000-0000-000000000000"
        )
        assert response.status_code == 404

    async def test_a_non_admin_is_403(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        seeded: Seeded,
        previous: IdentityProvider,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded, admin=False))
        response = await client.delete(f"/api/admin/identity-providers/{previous.id}")
        assert response.status_code == 403
        assert await exists(session_factory, previous.id)

    async def test_without_a_session_it_is_401(
        self, client: httpx.AsyncClient, previous: IdentityProvider
    ) -> None:
        response = await client.delete(f"/api/admin/identity-providers/{previous.id}")
        assert response.status_code == 401


class TestListing:
    async def test_the_listing_says_up_front_what_can_go_and_why_not(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        admin: User,
        previous: IdentityProvider,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await add_provider(session_factory, app, name="default", issuer=LIVE_ISSUER, enabled=True)
        used = await add_provider(
            session_factory,
            app,
            name="previous-20260901-cd34",
            issuer="https://used-idp.example.org",
            enabled=False,
        )
        async with session_factory() as session:
            session.add(User(issuer=used.issuer, subject="s", email="s@example.org"))
            await session.commit()

        listed = {p["name"]: p for p in (await client.get("/api/admin/identity-providers")).json()}

        assert listed["default"]["removable"] is False
        assert listed["default"]["kept_reason"] == ""
        assert listed[previous.name]["removable"] is True
        assert listed[previous.name]["kept_reason"] == ""
        assert listed[used.name]["removable"] is False
        assert listed[used.name]["kept_reason"].startswith("Kept: 1 person signed in")


class TestSignInAgainstARemovedRow:
    async def test_a_sign_in_that_resolved_the_provider_before_removal_is_refused(
        self,
        app: FastAPI,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        previous: IdentityProvider,
    ) -> None:
        """The window the share lock closes, observed from the sign-in side:
        the provider was resolved while it existed, and is gone by the time
        the sign-in's transaction asks. Nobody is provisioned at the issuer."""
        from gateway.identity_registry import record_from_row

        record = record_from_row(previous, app.state.secrets)
        async with session_factory() as other:
            await other.delete(await other.get(IdentityProvider, previous.id))
            await other.commit()

        with pytest.raises(ProvisioningRefused):
            await sign_in(
                session,
                record,  # type: ignore[arg-type]
                issuer=OLD_ISSUER,
                subject="late",
                claims={"sub": "late"},
                settings=app.state.settings,
            )
        users = (
            await session.execute(select(func.count(User.id)).where(User.issuer == OLD_ISSUER))
        ).scalar_one()
        assert users == 0
