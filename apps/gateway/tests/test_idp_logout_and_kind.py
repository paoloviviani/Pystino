"""Signing out of the bundled Authelia, and its provider row being an Authelia one.

Two live findings (2026-09-24). Authelia 4.39 publishes no
`end_session_endpoint`, so `redirect_to` was null, the console signed in again
and Authelia's live SSO session let the person straight back in. And the
environment seed created the bundled Authelia's row as `kind=generic` with no
sync adapter, hiding the users-file sync and the console's user management.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from conftest import BEARER_ISSUER, seed_identity_provider
from fastapi import FastAPI
from gateway.config import OIDCSettings, Settings
from gateway.identity_policy import default_logout_url, logout_redirect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

LANDING = "https://llm.example.org/console"
AUTHELIA = "https://llm.example.org/authelia"


def _select(**overrides: Any) -> str | None:
    arguments: dict[str, Any] = {
        "override": "",
        "end_session_endpoint": None,
        "kind": "generic",
        "issuer": AUTHELIA,
        "landing": LANDING,
        "id_token_hint": None,
        "client_id": "pystino-console",
    }
    return logout_redirect(**{**arguments, **overrides})


class TestTheRedirectSelection:
    def test_authelia_without_discovery_uses_its_portal_logout(self) -> None:
        url = _select(kind="authelia")
        assert url is not None, "the SSO session would survive signing out"
        assert url.startswith(f"{AUTHELIA}/logout?rd=")
        assert parse_qs(urlparse(url).query)["rd"] == [LANDING]

    def test_the_spec_endpoint_is_used_when_published(self) -> None:
        url = _select(kind="authelia", end_session_endpoint=f"{AUTHELIA}/end", id_token_hint="t")
        assert url is not None and url.startswith(f"{AUTHELIA}/end?")
        query = parse_qs(urlparse(url).query)
        assert query["post_logout_redirect_uri"] == [LANDING]
        assert query["id_token_hint"] == ["t"] and "client_id" not in query

    def test_an_override_wins_and_gets_the_landing_filled_in(self) -> None:
        url = _select(
            override="https://idp.example.org/bye?next={redirect}",
            end_session_endpoint=f"{AUTHELIA}/end",
        )
        assert url == "https://idp.example.org/bye?next=https%3A%2F%2Fllm.example.org%2Fconsole"

    def test_an_override_without_a_placeholder_is_used_as_written(self) -> None:
        assert _select(override="https://idp.example.org/logout") == (
            "https://idp.example.org/logout"
        )

    def test_a_generic_provider_with_nothing_published_has_no_answer(self) -> None:
        assert _select() is None

    def test_the_default_is_offered_to_the_console_only_for_authelia(self) -> None:
        expected = f"{AUTHELIA}/logout?rd={{redirect}}"
        assert default_logout_url("authelia", f"{AUTHELIA}/") == expected
        assert default_logout_url("keycloak", AUTHELIA) == ""


class TestTheRoute:
    @pytest.mark.asyncio
    async def test_the_provider_s_logout_url_is_used_and_the_chat_is_signed_out_too(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        signing_key: Any,
    ) -> None:
        from gateway.models import IdentityProvider

        row = await seed_identity_provider(app, session_factory, signing_key)
        async with session_factory() as session:
            stored = await session.get(IdentityProvider, row.id)
            assert stored is not None
            stored.logout_url = f"{BEARER_ISSUER}/portal-logout?rd={{redirect}}"
            await session.commit()
        app.state.settings.logout_also_clear_cookies = ["hf-chat", "other:/x"]

        response = await client.post("/auth/logout")
        redirect_to = response.json()["redirect_to"]
        assert redirect_to.startswith(f"{BEARER_ISSUER}/portal-logout?rd=http%3A%2F%2Fgateway")
        cookies = response.headers.get_list("set-cookie")
        assert any(c.startswith("hf-chat=") and "Path=/" in c for c in cookies), cookies
        assert any(c.startswith("other=") and "Path=/x" in c for c in cookies), cookies
        assert any(c.startswith("gw_session=") for c in cookies)

    @pytest.mark.asyncio
    async def test_the_console_is_shown_the_default_it_would_use(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        seeded: Any,
        session_factory: async_sessionmaker[AsyncSession],
        signing_key: Any,
    ) -> None:
        from gateway.models import IdentityProvider
        from test_admin import as_user, make_admin

        as_user(app, await make_admin(session_factory, seeded))
        admin_client = client

        row = await seed_identity_provider(app, session_factory, signing_key)
        async with session_factory() as session:
            stored = await session.get(IdentityProvider, row.id)
            assert stored is not None
            stored.kind = "authelia"
            await session.commit()
        listed = (await admin_client.get("/api/admin/identity-providers")).json()
        mine = next(p for p in listed if p["id"] == str(row.id))
        assert mine["logout_url"] == ""
        assert mine["default_logout_url"] == f"{BEARER_ISSUER}/logout?rd={{redirect}}"

        # The row is a projection of the environment now (ADR 0093 §14): there
        # is no `PUT` to edit `logout_url` through any more, only what a
        # re-seed from `GATEWAY_OIDC__LOGOUT_URL` writes onto the row
        # directly — which is what this simulates, to prove the listing
        # still prefers an explicit value over the computed default.
        async with session_factory() as session:
            stored = await session.get(IdentityProvider, row.id)
            assert stored is not None
            stored.logout_url = "https://idp.example.org/out?to={redirect}"
            await session.commit()
        relisted = (await admin_client.get("/api/admin/identity-providers")).json()
        mine_again = next(p for p in relisted if p["id"] == str(row.id))
        assert mine_again["logout_url"] == "https://idp.example.org/out?to={redirect}"


def _settings(kind: str) -> Settings:
    return Settings(
        oidc=OIDCSettings(
            enabled=True,
            issuer=AUTHELIA,
            client_id="pystino-console",
            internal_base_url="http://authelia:9091/authelia",
            kind=kind,
        )
    )


class TestTheBundledAutheliaRow:
    @pytest.mark.asyncio
    async def test_a_fresh_seed_is_an_authelia_row_with_an_unconfirmed_sync(
        self, session: AsyncSession
    ) -> None:
        from gateway.identity_registry import reseed_from_env
        from gateway.models import IdentityProvider
        from gateway.secrets import SecretBox
        from sqlalchemy import select

        box = SecretBox(["test-encryption-key-not-for-production"])
        await reseed_from_env(session, _settings("authelia"), box)  # type: ignore[arg-type]
        row = (await session.execute(select(IdentityProvider))).scalar_one()
        assert (row.kind, row.sync_adapter, row.sync_confirmed) == (
            "authelia",
            "authelia_file",
            False,
        )

    @pytest.mark.asyncio
    async def test_re_seeding_never_touches_an_administrator_s_sync_choice(
        self, session: AsyncSession
    ) -> None:
        """``sync_adapter`` is console-owned once the row exists (ADR 0093
        §2): re-seeding an *existing* matching-issuer row never re-derives it
        from ``kind`` — unlike the old ``_fill_kind``, which corrected a row
        still at its old defaults. That correction was itself a one-time
        historical fix for a pre-0093 deployment (migration 0046); once a row
        exists under this design, only its first creation sets the adapter
        from the kind, and an administrator's later choice is theirs from
        then on.
        """
        from gateway.identity_registry import reseed_from_env
        from gateway.models import IdentityProvider
        from gateway.secrets import SecretBox
        from sqlalchemy import select

        box = SecretBox(["test-encryption-key-not-for-production"])
        await reseed_from_env(session, _settings("authelia"), box)  # type: ignore[arg-type]
        row = (await session.execute(select(IdentityProvider))).scalar_one()
        row.sync_adapter = "none"
        await session.commit()
        await reseed_from_env(session, _settings("authelia"), box)  # type: ignore[arg-type]
        await session.refresh(row)
        assert row.sync_adapter == "none"
        assert row.kind == "authelia"  # still environment-owned, still overwritten

    def test_an_unknown_kind_in_the_environment_is_refused(self) -> None:
        with pytest.raises(ValueError, match="GATEWAY_OIDC__KIND"):
            OIDCSettings(kind="ldap")
