"""Signing out of the bundled Authelia, and its provider row being an Authelia one.

Two live findings (2026-09-24). Authelia 4.39 publishes no
`end_session_endpoint`, so `redirect_to` was null, the console signed in again
and Authelia's live SSO session let the person straight back in. And the
environment seed created the bundled Authelia's row as `kind=generic` with no
sync adapter, hiding the users-file sync and the console's user management.
"""

from __future__ import annotations

from pathlib import Path
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

        updated = await admin_client.put(
            f"/api/admin/identity-providers/{row.id}",
            json={"logout_url": "https://idp.example.org/out?to={redirect}"},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["logout_url"] == "https://idp.example.org/out?to={redirect}"
        refused = await admin_client.put(
            f"/api/admin/identity-providers/{row.id}", json={"logout_url": "javascript:alert(1)"}
        )
        assert refused.status_code in (400, 422)  # the app reports validation as 400


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
        from gateway.identity_registry import seed_from_env
        from gateway.models import IdentityProvider
        from gateway.secrets import SecretBox
        from sqlalchemy import select

        box = SecretBox(["test-encryption-key-not-for-production"])
        await seed_from_env(session, _settings("authelia"), box)  # type: ignore[arg-type]
        row = (await session.execute(select(IdentityProvider))).scalar_one()
        assert (row.kind, row.sync_adapter, row.sync_confirmed) == (
            "authelia",
            "authelia_file",
            False,
        )

    @pytest.mark.asyncio
    async def test_an_old_generic_row_is_corrected_once_and_a_choice_is_kept(
        self, session: AsyncSession
    ) -> None:
        from gateway.identity_registry import seed_from_env
        from gateway.models import GroupSync, IdentityProvider
        from gateway.secrets import SecretBox

        box = SecretBox(["test-encryption-key-not-for-production"])
        row = IdentityProvider(
            name="default",
            issuer=AUTHELIA,
            client_id="pystino-console",
            client_secret_encrypted=box.encrypt("s"),
            scopes=["openid"],
            groups_claim="groups",
            fetch_userinfo=True,
            group_mappings=[],
            link_local_by_email=False,
            group_sync=GroupSync.FIRST_LOGIN,
            internal_base_url="http://authelia:9091/authelia",
            is_enabled=True,
        )
        session.add(row)
        await session.commit()
        await seed_from_env(session, _settings("authelia"), box)  # type: ignore[arg-type]
        await session.refresh(row)
        assert (row.kind, row.sync_adapter, row.sync_confirmed) == (
            "authelia",
            "authelia_file",
            False,
        )
        # An administrator's later choice is theirs.
        row.sync_adapter = "none"
        await session.commit()
        await seed_from_env(session, _settings("authelia"), box)  # type: ignore[arg-type]
        await session.refresh(row)
        assert row.sync_adapter == "none"

    def test_an_unknown_kind_in_the_environment_is_refused(self) -> None:
        with pytest.raises(ValueError, match="GATEWAY_OIDC__KIND"):
            OIDCSettings(kind="ldap")


def test_init_writes_the_kind_and_the_chat_logout_url_for_the_bundled_authelia(
    tmp_path: Path,
) -> None:
    from gateway.deploy import doctor, envfile
    from gateway.deploy.cli import main

    argv = ["init", "--dir", str(tmp_path), "--origin", "https://llm.example.org"]
    assert main([*argv, "--admin-email", "ops@example.org", "--preset", "team"]) == 0
    env = envfile.read(tmp_path / ".env")
    assert env["OIDC_KIND"] == "authelia"
    assert env["OIDC_LOGOUT_URL"] == "https://llm.example.org/authelia/logout?rd={redirect}"
    first = doctor.check(tmp_path, probe_docker=False).warnings
    assert not any("OIDC_LOGOUT_URL" in w for w in first)

    envfile.update(tmp_path / ".env", {"OIDC_LOGOUT_URL": ""})
    warnings = doctor.check(tmp_path, probe_docker=False).warnings
    assert any("OIDC_LOGOUT_URL" in w and "/authelia/logout?rd={redirect}" in w for w in warnings)
