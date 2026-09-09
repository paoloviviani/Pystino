"""Signing out ends the session *here* and at the identity provider.

`POST /auth/logout` had no test at all, on either side of the split, and that is
why it silently stopped working: ADR 0051 moved providers into rows, the route
kept reading `app.state.oidc_client` — which is set nowhere — and so
`redirect_to` was always null. Our cookie went, Keycloak's SSO session stayed,
and the next visit to `/auth/login` returned the reader to the console as the
same person without a password. On a shared machine that is the whole point of
signing out.

The shape of the bug is worth keeping in mind while reading these: a null
`redirect_to` is also the *correct* answer for a provider that publishes no
`end_session_endpoint`, so "no crash" proved nothing. Every test here asserts on
the URL rather than on the request succeeding.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from conftest import BEARER_ISSUER, seed_identity_provider
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query)


class TestEndsTheProvidersSession:
    @pytest.mark.asyncio
    async def test_it_hands_back_the_providers_logout_url(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        signing_key: Any,
    ) -> None:
        """The test that fails without the fix.

        Before it, this returned `{"redirect_to": null}` for a deployment with a
        working provider, and nothing anywhere said so.
        """
        await seed_identity_provider(app, session_factory, signing_key)

        response = await client.post("/auth/logout")
        assert response.status_code == 200, response.text
        redirect_to = response.json()["redirect_to"]

        assert redirect_to is not None, "the provider's session would have survived"
        assert redirect_to.startswith(f"{BEARER_ISSUER}/logout")

    @pytest.mark.asyncio
    async def test_it_asks_the_provider_to_come_back_here(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        signing_key: Any,
    ) -> None:
        """`post_logout_redirect_uri` is built from the origin of this request.

        Hard-coding it breaks the moment the stack is reached on the overlay
        address instead of localhost, which is the arrangement the proxy overlay
        exists to serve.
        """
        await seed_identity_provider(app, session_factory, signing_key)

        response = await client.post("/auth/logout")
        landing = _query(response.json()["redirect_to"])["post_logout_redirect_uri"][0]
        assert landing.startswith("http://gateway"), landing

    @pytest.mark.asyncio
    async def test_without_a_hint_it_names_the_provider_s_own_client(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        signing_key: Any,
    ) -> None:
        """The row's client id, not the environment's.

        `GATEWAY_OIDC__CLIENT_ID` seeded the first row and can name a different
        provider than the one being signed out of — or nothing at all, where
        every provider was added through the console.
        """
        record = await seed_identity_provider(app, session_factory, signing_key)

        response = await client.post("/auth/logout")
        parameters = _query(response.json()["redirect_to"])
        assert parameters["client_id"] == [record.client_id]
        assert "id_token_hint" not in parameters

    @pytest.mark.asyncio
    async def test_a_hint_is_preferred_to_a_client_id(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        signing_key: Any,
    ) -> None:
        """With the hint the provider ends the session without stopping to ask.

        Without one Keycloak prompts — correctly, since otherwise any page able
        to navigate a browser here could sign people out.
        """
        await seed_identity_provider(app, session_factory, signing_key)
        from conftest import make_token

        token = make_token(signing_key)
        # Sent as a header rather than through httpx's cookie jar: the jar wants
        # a domain and path match against a transport that has no real host, and
        # what is under test is the route reading `request.cookies`, not httpx's
        # matching rules.
        response = await client.post(
            "/auth/logout", headers={"cookie": f"gw_idt={token}"}
        )
        parameters = _query(response.json()["redirect_to"])
        assert parameters["id_token_hint"] == [token]
        assert "client_id" not in parameters


class TestAlwaysDropsOurOwnSession:
    @pytest.mark.asyncio
    async def test_the_cookies_go_even_with_no_provider(
        self, client: httpx.AsyncClient
    ) -> None:
        """A deployment with no directory still has to be able to sign out.

        `redirect_to` is null here and that is the right answer: there is no
        second session to end.
        """
        response = await client.post("/auth/logout")
        assert response.status_code == 200
        assert response.json()["redirect_to"] is None
        assert "gw_session" in response.headers.get("set-cookie", "")

    @pytest.mark.asyncio
    async def test_an_unreachable_provider_does_not_block_signing_out(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        signing_key: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Failing to read discovery must not leave someone stuck signed in."""
        await seed_identity_provider(app, session_factory, signing_key)

        from gateway.oidc import OIDCError

        registry = app.state.oidc_providers
        for cached in list(getattr(registry, "_clients", {}).values()):

            async def _broken(*_a: object, **_k: object) -> None:
                raise OIDCError("provider is down")

            monkeypatch.setattr(cached, "metadata", _broken, raising=False)

        response = await client.post("/auth/logout")
        assert response.status_code == 200
        assert response.json()["redirect_to"] is None
        assert "gw_session" in response.headers.get("set-cookie", "")
