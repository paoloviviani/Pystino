"""The local door (ADR 0046): sign in with the gateway's own accounts.

What is specific to this feature and earns tests: the session row a local
sign-in creates is the *same* shape the OIDC callback creates, keyed
``(issuer="local", subject=casefolded email)``; the access token comes from
the gateway's exchange rather than an IdP refresh; and the failure modes the
gateway can produce — refuse the credential, be unreachable — have different
honesties. A 401 from the gateway ends the session; a 502 must not log
someone out over a network blip.
"""

from __future__ import annotations

import httpx
import pytest
from chat_api.deps import SESSION_COOKIE
from chat_api.models import Session
from chat_api.routers import MOUNT_PATH as MOUNT
from chat_api.secrets import SecretBox
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import ENCRYPTION_KEY, FakeGateway

pytestmark = [pytest.mark.asyncio]


async def _sign_in(client: httpx.AsyncClient, **overrides: str) -> httpx.Response:
    body = {"email": "person@example.org", "password": "good password"} | overrides
    return await client.post(f"{MOUNT}/api/auth/local", json=body)


class TestLocalSignIn:
    async def test_good_credentials_create_a_session_and_set_the_cookie(
        self,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        fake_gateway: FakeGateway,
    ) -> None:
        response = await _sign_in(client)
        assert response.status_code == 204
        assert SESSION_COOKIE in response.cookies

        async with session_factory() as db:
            session = (await db.execute(select(Session))).scalar_one()
        assert session.issuer == "local"
        # The casefolded email — the exact key the gateway logs in by. A
        # different case here would create a transcript owner the gateway's
        # ledger never heard of.
        assert session.subject == "person@example.org"
        assert session.groups == ["research"]
        assert session.is_admin is False
        # The refresh credential is stored encrypted, not in the clear.
        box = SecretBox([ENCRYPTION_KEY])
        assert box.decrypt(session.refresh_token_encrypted).startswith("gwr_")

    async def test_uppercase_email_lands_on_the_casefolded_subject(
        self, client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The gateway casefolds at login; the session row must key the same."""
        response = await _sign_in(client, email="Person@Example.ORG")
        assert response.status_code == 204
        async with session_factory() as db:
            session = (await db.execute(select(Session))).scalar_one()
        assert session.subject == "person@example.org"

    async def test_wrong_password_is_the_gateways_own_answer(
        self, client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        response = await _sign_in(client, password="wrong password")
        assert response.status_code == 401
        assert response.json()["detail"] == "Incorrect email or password."
        async with session_factory() as db:
            assert (await db.execute(select(Session))).scalars().all() == []

    async def test_local_auth_disabled_is_503(
        self, client: httpx.AsyncClient, fake_gateway: FakeGateway
    ) -> None:
        fake_gateway.local_login_response = (
            503,
            {"error": {"message": "Local sign-in is not enabled.", "type": "api_error"}},
        )
        response = await _sign_in(client)
        assert response.status_code == 503


class TestLocalSessionsCallTheGateway:
    async def test_a_local_session_exchanges_at_the_gateway(
        self, client: httpx.AsyncClient, fake_gateway: FakeGateway
    ) -> None:
        """get_caller branches on the issuer: local means /auth/token."""
        login = await _sign_in(client)
        client.cookies.set(SESSION_COOKIE, login.cookies[SESSION_COOKIE])

        me = await client.get(f"{MOUNT}/api/me")
        assert me.status_code == 200
        assert me.json()["email"] == "person@example.org"
        # The session holds a credential the gateway issued and accepted: the
        # exchange minted one, which is what a /v1 turn would carry.
        assert fake_gateway.access_credentials

    async def test_gateway_refusing_the_credential_ends_the_session(
        self,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        fake_gateway: FakeGateway,
    ) -> None:
        login = await _sign_in(client)
        client.cookies.set(SESSION_COOKIE, login.cookies[SESSION_COOKIE])
        fake_gateway.refresh_credentials.clear()  # revoked upstream

        me = await client.get(f"{MOUNT}/api/me")
        assert me.status_code == 401
        async with session_factory() as db:
            assert (await db.execute(select(Session))).scalars().all() == []

    async def test_gateway_unreachable_keeps_the_session(
        self,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        fake_gateway: FakeGateway,
    ) -> None:
        """A transient outage must not log people out."""
        login = await _sign_in(client)
        client.cookies.set(SESSION_COOKIE, login.cookies[SESSION_COOKIE])
        fake_gateway.down = True

        me = await client.get(f"{MOUNT}/api/me")
        assert me.status_code == 502
        async with session_factory() as db:
            assert (await db.execute(select(Session))).scalar_one() is not None


class TestLocalLogout:
    async def test_logout_revokes_the_family_then_deletes_the_row(
        self,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        fake_gateway: FakeGateway,
    ) -> None:
        login = await _sign_in(client)
        client.cookies.set(SESSION_COOKIE, login.cookies[SESSION_COOKIE])

        response = await client.post(f"{MOUNT}/api/auth/logout")
        assert response.status_code == 204
        # The gateway's family is gone: an exchange with it now refuses.
        assert fake_gateway.refresh_credentials == {}
        async with session_factory() as db:
            assert (await db.execute(select(Session))).scalars().all() == []

    async def test_logout_is_real_even_when_the_gateway_cannot_be_asked(
        self,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        fake_gateway: FakeGateway,
    ) -> None:
        """The session row dies regardless; the cost is named in the ADR."""
        login = await _sign_in(client)
        client.cookies.set(SESSION_COOKIE, login.cookies[SESSION_COOKIE])
        fake_gateway.down = True

        response = await client.post(f"{MOUNT}/api/auth/logout")
        assert response.status_code == 204
        async with session_factory() as db:
            assert (await db.execute(select(Session))).scalars().all() == []


class TestAuthMethods:
    async def test_methods_are_the_gateways_answer(
        self, client: httpx.AsyncClient, fake_gateway: FakeGateway
    ) -> None:
        response = await client.get(f"{MOUNT}/api/auth/methods")
        assert response.status_code == 200
        assert response.json() == {"local": True, "oidc": False}

        fake_gateway.methods = {"local": False, "oidc": True}
        response = await client.get(f"{MOUNT}/api/auth/methods")
        assert response.json() == {"local": False, "oidc": True}
