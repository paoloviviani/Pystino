"""Server-to-server OIDC over an internal URL (the deployment re-architecture, §2).

The bundled Authelia is published at ``https://<origin>/authelia`` for browsers
and reached by the gateway at ``http://authelia:9091/authelia``. These tests pin
the three things that make that work without a CA trust bundle: discovery and
every back-channel call go to the internal URL, they carry the forwarded headers
the IdP derives its issuer from, and the browser-facing endpoints stay public.
"""

from __future__ import annotations

import httpx
import pytest
from gateway.config import OIDCSettings
from gateway.oidc import OIDCClient, OIDCError

PUBLIC = "https://llm.example.org/authelia"
INTERNAL = "http://authelia:9091/authelia"


def _discovery(issuer: str) -> dict[str, str]:
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/api/oidc/authorization",
        "token_endpoint": f"{issuer}/api/oidc/token",
        "jwks_uri": f"{issuer}/jwks.json",
        "userinfo_endpoint": f"{issuer}/api/oidc/userinfo",
        "end_session_endpoint": f"{issuer}/logout",
    }


class _FakeIdp:
    """Answers like Authelia: the issuer is whatever the forwarded headers say."""

    def __init__(self, *, honour_forwarded: bool = True) -> None:
        self.requests: list[httpx.Request] = []
        self.honour_forwarded = honour_forwarded

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/.well-known/openid-configuration"):
            proto = request.headers.get("x-forwarded-proto")
            host = request.headers.get("x-forwarded-host")
            if self.honour_forwarded and proto and host:
                issuer = f"{proto}://{host}/authelia"
            else:
                issuer = INTERNAL
            return httpx.Response(200, json=_discovery(issuer))
        if request.url.path.endswith("/api/oidc/token"):
            return httpx.Response(200, json={"access_token": "a", "id_token": "i"})
        if request.url.path.endswith("/api/oidc/userinfo"):
            return httpx.Response(200, json={"sub": "s", "groups": ["users"]})
        return httpx.Response(404)


def _client(idp: _FakeIdp, *, internal: str = INTERNAL) -> OIDCClient:
    settings = OIDCSettings(
        enabled=True,
        issuer=PUBLIC,
        client_id="pystino-console",
        client_secret="secret",
        redirect_uri="https://llm.example.org/auth/callback/default",
        internal_base_url=internal,
    )
    return OIDCClient(settings, httpx.AsyncClient(transport=httpx.MockTransport(idp)))


async def test_discovery_goes_to_the_internal_url_with_forwarded_headers() -> None:
    idp = _FakeIdp()
    metadata = await _client(idp).metadata()

    (request,) = idp.requests
    assert str(request.url) == f"{INTERNAL}/.well-known/openid-configuration"
    assert request.headers["x-forwarded-proto"] == "https"
    assert request.headers["x-forwarded-host"] == "llm.example.org"
    assert metadata.issuer == PUBLIC


async def test_back_channel_endpoints_move_and_browser_endpoints_stay_public() -> None:
    metadata = await _client(_FakeIdp()).metadata()

    assert metadata.token_endpoint == f"{INTERNAL}/api/oidc/token"
    assert metadata.jwks_uri == f"{INTERNAL}/jwks.json"
    assert metadata.userinfo_endpoint == f"{INTERNAL}/api/oidc/userinfo"
    # What the browser is sent to must be reachable from the browser.
    assert metadata.authorization_endpoint == f"{PUBLIC}/api/oidc/authorization"
    assert metadata.end_session_endpoint == f"{PUBLIC}/logout"


async def test_token_and_userinfo_calls_carry_the_forwarded_headers() -> None:
    idp = _FakeIdp()
    client = _client(idp)
    await client.exchange_code("code", "verifier")
    await client.fetch_userinfo("access")

    token, userinfo = idp.requests[1], idp.requests[2]
    assert str(token.url) == f"{INTERNAL}/api/oidc/token"
    assert token.headers["x-forwarded-host"] == "llm.example.org"
    assert str(userinfo.url) == f"{INTERNAL}/api/oidc/userinfo"
    assert userinfo.headers["authorization"] == "Bearer access"
    assert userinfo.headers["x-forwarded-proto"] == "https"


async def test_an_idp_that_ignores_the_headers_is_named_as_the_cause() -> None:
    with pytest.raises(OIDCError, match="did not honour X-Forwarded"):
        await _client(_FakeIdp(honour_forwarded=False)).metadata()


async def test_a_non_default_port_is_kept_in_the_forwarded_host() -> None:
    idp = _FakeIdp()
    settings = OIDCSettings(
        enabled=True,
        issuer="https://10.0.0.5:18443/authelia",
        client_id="c",
        internal_base_url=INTERNAL,
    )
    client = OIDCClient(settings, httpx.AsyncClient(transport=httpx.MockTransport(idp)))
    metadata = await client.metadata()
    assert idp.requests[0].headers["x-forwarded-host"] == "10.0.0.5:18443"
    assert metadata.issuer == "https://10.0.0.5:18443/authelia"


async def test_without_an_internal_url_nothing_changes() -> None:
    requests: list[httpx.Request] = []

    def idp(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_discovery(PUBLIC))

    settings = OIDCSettings(enabled=True, issuer=PUBLIC, client_id="c")
    client = OIDCClient(settings, httpx.AsyncClient(transport=httpx.MockTransport(idp)))
    metadata = await client.metadata()
    assert str(requests[0].url) == f"{PUBLIC}/.well-known/openid-configuration"
    assert "x-forwarded-host" not in requests[0].headers
    assert metadata.token_endpoint == f"{PUBLIC}/api/oidc/token"


async def test_an_adopted_row_gets_the_environments_internal_url_once(session) -> None:
    """The provider row an old install seeded has no internal URL; the env fills it, once."""
    from gateway.config import Settings
    from gateway.identity_registry import seed_from_env
    from gateway.models import GroupSync, IdentityProvider
    from gateway.secrets import SecretBox

    box = SecretBox(["test-encryption-key-not-for-production"])
    row = IdentityProvider(
        name="default",
        issuer=PUBLIC,
        client_id="pystino-console",
        client_secret_encrypted=box.encrypt("s"),
        scopes=["openid"],
        groups_claim="groups",
        fetch_userinfo=True,
        group_mappings=[],
        link_local_by_email=False,
        group_sync=GroupSync.FIRST_LOGIN,
        is_enabled=True,
    )
    session.add(row)
    await session.commit()
    settings = Settings(
        oidc=OIDCSettings(
            enabled=True,
            issuer=PUBLIC,
            client_id="pystino-console",
            internal_base_url=INTERNAL,
            scopes=["openid", "profile", "email", "groups"],
        ),
    )
    await seed_from_env(session, settings, box)  # type: ignore[arg-type]
    await session.refresh(row)
    assert row.internal_base_url == INTERNAL
    # The adopted row also gains the environment's scopes (groups), added only.
    assert row.scopes == ["openid", "profile", "email", "groups"]
    row.internal_base_url = "http://elsewhere:9091/authelia"
    await session.commit()
    await seed_from_env(session, settings, box)  # type: ignore[arg-type]
    await session.refresh(row)
    assert row.internal_base_url == "http://elsewhere:9091/authelia", (
        "an administrator's value wins"
    )
