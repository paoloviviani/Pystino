"""Self-service password change (ADR 0049's other half) and the /api/me
identity fields the console needs to know which offers to make.

A wrong answer here is a lockout or a hijack: the current password is what
stands between a stolen cookie and a permanently stolen account, and the
boundary between local and directory accounts is what stops the platform
minting credentials its IdP will simply overwrite.
"""

from __future__ import annotations

from typing import Any

import httpx
from gateway.models import LocalCredential, User
from gateway.passwords import hash_password
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_my_limits import session_cookie


async def make_local_user(
    session_factory: async_sessionmaker[AsyncSession],
    password: str = "the-current-password",  # noqa: S107 — a fixture value, not a secret
) -> User:
    async with session_factory() as session:
        user = User(issuer="local", subject="self@example.org", email="self@example.org")
        session.add(user)
        await session.flush()
        session.add(LocalCredential(user_id=user.id, password_hash=hash_password(password)))
        await session.commit()
        return user


class TestMeIdentityFields:
    async def test_a_local_account_says_so_and_counts_its_password(
        self,
        client: httpx.AsyncClient,
        seeded: Any,
        app: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        user = await make_local_user(session_factory)
        response = await client.get("/api/me", headers=session_cookie(user.id, app))
        body = response.json()
        assert body["issuer"] == "local"
        assert body["has_password"] is True

    async def test_a_directory_account_has_no_password(
        self,
        client: httpx.AsyncClient,
        seeded: Any,
        app: Any,
    ) -> None:
        response = await client.get("/api/me", headers=session_cookie(seeded.user.id, app))
        body = response.json()
        assert body["issuer"] == "https://idp.test"
        assert body["has_password"] is False


class TestChangeMyPassword:
    async def test_the_current_password_proves_the_person(
        self,
        client: httpx.AsyncClient,
        app: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        user = await make_local_user(session_factory)
        response = await client.put(
            "/api/me/password",
            json={
                "current_password": "the-current-password",
                "new_password": "the-brand-new-password",
            },
            headers=session_cookie(user.id, app),
        )
        assert response.status_code == 204
        async with session_factory() as session:
            credential = await session.get(LocalCredential, user.id)
            assert credential is not None
            # The hash changed; the verifier is the proof, not a byte compare.
            from gateway.passwords import verify_password

            assert verify_password("the-brand-new-password", credential.password_hash)

    async def test_a_wrong_current_password_is_refused(
        self,
        client: httpx.AsyncClient,
        app: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        user = await make_local_user(session_factory)
        response = await client.put(
            "/api/me/password",
            json={
                "current_password": "not-the-password",
                "new_password": "the-brand-new-password",
            },
            headers=session_cookie(user.id, app),
        )
        assert response.status_code == 400
        assert "current password" in response.json()["error"]["message"]

    async def test_a_directory_account_cannot_mint_local_credentials(
        self,
        client: httpx.AsyncClient,
        seeded: Any,
        app: Any,
    ) -> None:
        response = await client.put(
            "/api/me/password",
            json={
                "current_password": "whatever",
                "new_password": "the-brand-new-password",
            },
            headers=session_cookie(seeded.user.id, app),
        )
        assert response.status_code == 400
        assert "identity provider" in response.json()["error"]["message"]

    async def test_a_short_password_is_refused(
        self,
        client: httpx.AsyncClient,
        app: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        user = await make_local_user(session_factory)
        response = await client.put(
            "/api/me/password",
            json={
                "current_password": "the-current-password",
                "new_password": "short",
            },
            headers=session_cookie(user.id, app),
        )
        assert response.status_code == 400

    async def test_a_local_account_without_a_password_cannot_set_one_this_way(
        self,
        client: httpx.AsyncClient,
        app: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # ADR 0043 made "local, no password" a real state. Setting a password
        # without proving the current one would hand a hijacked session the
        # account outright; an administrator sets the first password instead.
        async with session_factory() as session:
            user = User(issuer="local", subject="bare@example.org", email="bare@example.org")
            session.add(user)
            await session.commit()
            user_id = user.id

        response = await client.put(
            "/api/me/password",
            json={
                "current_password": "anything",
                "new_password": "the-brand-new-password",
            },
            headers=session_cookie(user_id, app),
        )
        assert response.status_code == 400
        assert "no password set" in response.json()["error"]["message"]
