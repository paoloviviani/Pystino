"""Opaque access tokens, introspected (GitLab as the external IdP).

GitLab's OAuth access tokens are Doorkeeper's random strings, not JWTs, so
the chat's `POST /v1/session/announce` (and every `/v1` call it makes with the
person's token) was refused with no log line: the router read `iss` out of
the token and found none. These tests run the real app against a fake GitLab
whose introspection and userinfo answer exactly as Doorkeeper 5.9 and GitLab's
own controllers do (verified against their source, see
`gateway.introspection`): introspection answers `active: true` only to the
application the token was issued to, with `client_id`, `scope`, `token_type`,
`iat` and `exp` and no `sub`; any other authenticated client gets
`{"active": false}`; unknown credentials get a 401.
"""

from __future__ import annotations

import base64
import logging
import secrets
import uuid
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import pytest_asyncio
from conftest import bearer_auth as auth
from fastapi import FastAPI
from gateway.config import OIDCSettings, Settings
from gateway.identity_registry import record_from_row
from gateway.introspection import (
    NEGATIVE_TTL_SECONDS,
    IntrospectionCache,
)
from gateway.models import IdentityProvider, User
from gateway.oidc import OIDCClient, OIDCMetadata
from gateway.routers.auth import _FLOW_COOKIE
from gateway.secrets import SecretBox
from gateway.security import generate_api_key
from gateway.types import utcnow
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

GITLAB = "https://gitlab.test"
CONSOLE_ID, CONSOLE_SECRET = "pystino-console", "console-secret"
CHAT_ID, CHAT_SECRET = "cerea", "chat-secret"
AUDIENCE = "pystino-api"


class FakeGitLab:
    """GitLab's `/oauth/introspect` and `/oauth/userinfo`, Doorkeeper-shaped."""

    def __init__(self) -> None:
        self.clients = {CONSOLE_ID: CONSOLE_SECRET, CHAT_ID: CHAT_SECRET, "other-app": "x"}
        # token -> (client it was issued to, GitLab user id, exp)
        self.tokens: dict[str, tuple[str, int, int]] = {}
        self.users = {
            42: {"email": "ada@example.org", "name": "Ada", "groups": ["research"]},
        }
        self.introspections = 0
        self.userinfo_calls = 0
        # Misbehaviours a stricter-than-GitLab IdP never shows, to prove the
        # gateway does not rely on the provider being strict.
        self.active_for_anyone = False
        self.ignore_expiry = False

    def mint(self, client_id: str, *, user: int = 42, ttl: int = 7200) -> str:
        token = secrets.token_urlsafe(32).replace("_", "x")
        self.tokens[token] = (client_id, user, int(utcnow().timestamp()) + ttl)
        return token

    def _live(self, token: str) -> tuple[str, int, int] | None:
        held = self.tokens.get(token)
        if held is None:
            return None
        if held[2] <= utcnow().timestamp() and not self.ignore_expiry:
            return None
        return held

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/introspect":
            self.introspections += 1
            header = request.headers.get("authorization", "")
            if not header.startswith("Basic "):
                return httpx.Response(400, json={"error": "invalid_request"})
            client_id, _, secret = base64.b64decode(header[6:]).decode().partition(":")
            if self.clients.get(client_id) != secret:
                return httpx.Response(401, json={"error": "invalid_client"})
            token = parse_qs(request.content.decode())["token"][0]
            held = self._live(token)
            if held is None or (held[0] != client_id and not self.active_for_anyone):
                return httpx.Response(200, json={"active": False})
            return httpx.Response(
                200,
                json={
                    "active": True,
                    "scope": "openid profile email",
                    "client_id": held[0],
                    "token_type": "Bearer",
                    "iat": int(utcnow().timestamp()),
                    "exp": held[2],
                },
            )
        if request.url.path == "/oauth/userinfo":
            self.userinfo_calls += 1
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            held = self._live(token)
            if held is None:
                return httpx.Response(401)
            # GitLab's userinfo `sub` is the user id as a string — the same
            # value its ID tokens carry (doorkeeper_openid_connect.rb).
            return httpx.Response(200, json={"sub": str(held[1]), **self.users[held[1]]})
        return httpx.Response(404)


class GitLabClient(OIDCClient):
    """Real OIDCClient; only discovery and JWKS are local, the rest is HTTP."""

    def __init__(self, settings: OIDCSettings, gitlab: FakeGitLab, key: RSAKey) -> None:
        super().__init__(settings, httpx.AsyncClient(transport=httpx.MockTransport(gitlab.handle)))
        self._key = key
        self.introspection_endpoint: str | None = f"{GITLAB}/oauth/introspect"

    async def metadata(self) -> OIDCMetadata:
        return OIDCMetadata(
            issuer=GITLAB,
            authorization_endpoint=f"{GITLAB}/oauth/authorize",
            token_endpoint=f"{GITLAB}/oauth/token",
            jwks_uri=f"{GITLAB}/oauth/discovery/keys",
            userinfo_endpoint=f"{GITLAB}/oauth/userinfo",
            introspection_endpoint=self.introspection_endpoint,
        )

    async def jwks(self, *, force: bool = False) -> KeySet:
        return KeySet([self._key])

    async def exchange_code(self, code: str, code_verifier: str) -> dict[str, Any]:
        # The console's code exchange: an ID token for the console client and
        # an opaque access token, as GitLab answers.
        now = int(utcnow().timestamp())
        id_token = jwt.encode(
            {"alg": "RS256", "kid": self._key.kid},
            {
                "iss": GITLAB,
                "sub": "42",
                "aud": CONSOLE_ID,
                "exp": now + 120,
                "iat": now,
                "email": "ada@example.org",
                "email_verified": True,
                "name": "Ada",
                "groups_direct": ["research"],
            },
            self._key,
        )
        return {"id_token": id_token, "access_token": code}


@pytest.fixture(scope="module")
def gitlab_key() -> RSAKey:
    return RSAKey.generate_key(2048, parameters={"kid": "gitlab-1"})


@pytest.fixture
def gitlab() -> FakeGitLab:
    return FakeGitLab()


@pytest_asyncio.fixture
async def gitlab_app(app: FastAPI, gitlab: FakeGitLab, gitlab_key: RSAKey) -> FastAPI:
    """The app configured as the GitLab deployment was: OIDC_KIND=generic,
    the console and chat clients, and the chat secret this change adds."""
    settings: Settings = app.state.settings
    settings.oidc = OIDCSettings(
        enabled=True,
        issuer=GITLAB,
        client_id=CONSOLE_ID,
        client_secret=CONSOLE_SECRET,
        access_token_audience=AUDIENCE,
        accepted_clients=f"{CONSOLE_ID},{CHAT_ID}",
        chat_client_id=CHAT_ID,
        chat_client_secret=CHAT_SECRET,
        groups_claim="groups",
    )
    settings.session_cookie_secure = False
    box: SecretBox = app.state.secrets
    row = IdentityProvider(
        name="gitlab",
        issuer=GITLAB,
        client_id=CONSOLE_ID,
        client_secret_encrypted=box.encrypt(CONSOLE_SECRET),
        scopes=["openid", "profile", "email"],
        fetch_userinfo=True,
        is_enabled=True,
    )
    async with app.state.session_factory() as session:
        session.add(row)
        await session.commit()
    record = record_from_row(row, box)
    origin = "http://gateway"
    client = GitLabClient(
        record.as_oidc_settings(
            f"{origin}/auth/callback/gitlab", AUDIENCE, settings.oidc.accepted_clients
        ),
        gitlab,
        gitlab_key,
    )
    app.state.oidc_providers._clients[(row.id, row.updated_at, origin)] = client
    app.state.gitlab_client = client
    return app


async def _console_login(client: Any, code: str) -> None:
    """The console's browser sign-in, start to callback, through the routes."""
    start = await client.get("/auth/login", params={"provider": "gitlab"})
    assert start.status_code == 302, start.text
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    flow = start.cookies[_FLOW_COOKIE]
    done = await client.get(
        "/auth/callback/gitlab",
        params={"code": code, "state": state},
        headers={"cookie": f"{_FLOW_COOKIE}={flow}"},
    )
    assert done.status_code < 400, done.text


async def _users(session_factory: async_sessionmaker[AsyncSession]) -> list[User]:
    async with session_factory() as db:
        return list((await db.execute(select(User).where(User.issuer == GITLAB))).scalars())


class TestAnnounceWithAnOpaqueToken:
    async def test_the_chat_signs_in(
        self, gitlab_app: FastAPI, client: Any, gitlab: FakeGitLab
    ) -> None:
        response = await client.post("/v1/session/announce", headers=auth(gitlab.mint(CHAT_ID)))
        assert response.status_code == 200, response.text
        assert response.json()["identities"] == [{"issuer": GITLAB, "subject": "42"}]

    async def test_console_and_chat_logins_are_the_same_person(
        self,
        gitlab_app: FastAPI,
        client: Any,
        gitlab: FakeGitLab,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _console_login(client, gitlab.mint(CONSOLE_ID))
        console_users = await _users(session_factory)
        assert [(u.issuer, u.subject) for u in console_users] == [(GITLAB, "42")]

        chat_token = gitlab.mint(CHAT_ID)
        announced = await client.post("/v1/session/announce", headers=auth(chat_token))
        assert announced.status_code == 200, announced.text
        me = await client.get("/v1/me", headers=auth(chat_token))
        assert me.status_code == 200, me.text

        assert announced.json()["id"] == str(console_users[0].id)
        assert me.json()["id"] == str(console_users[0].id)
        assert len(await _users(session_factory)) == 1

    async def test_a_console_token_is_not_the_chat(
        self, gitlab_app: FastAPI, client: Any, gitlab: FakeGitLab, caplog: Any
    ) -> None:
        token = gitlab.mint(CONSOLE_ID)
        with caplog.at_level(logging.WARNING):
            response = await client.post("/v1/session/announce", headers=auth(token))
        assert response.status_code == 401, response.text
        assert "expected the chat client 'cerea'" in caplog.text
        assert token not in caplog.text

    async def test_a_token_of_another_application_is_refused(
        self, gitlab_app: FastAPI, client: Any, gitlab: FakeGitLab, caplog: Any
    ) -> None:
        token = gitlab.mint("other-app")
        with caplog.at_level(logging.WARNING):
            response = await client.post("/v1/session/announce", headers=auth(token))
        assert response.status_code == 401, response.text
        assert "inactive for client 'cerea'" in caplog.text
        assert token not in caplog.text

    async def test_an_answer_naming_another_client_is_not_believed(
        self, gitlab_app: FastAPI, client: Any, gitlab: FakeGitLab, caplog: Any
    ) -> None:
        """An IdP that lets any client introspect everything would vouch for
        another application's token; the binding is the asking client."""
        gitlab.active_for_anyone = True
        token = gitlab.mint("other-app")
        with caplog.at_level(logging.WARNING):
            response = await client.post("/v1/session/announce", headers=auth(token))
        assert response.status_code == 401, response.text
        assert "names client 'other-app', not the asking client 'cerea'" in caplog.text

    async def test_an_expired_token_is_refused_even_if_the_idp_says_active(
        self, gitlab_app: FastAPI, client: Any, gitlab: FakeGitLab, caplog: Any
    ) -> None:
        gitlab.ignore_expiry = True
        token = gitlab.mint(CHAT_ID, ttl=-5)
        with caplog.at_level(logging.WARNING):
            response = await client.post("/v1/session/announce", headers=auth(token))
        assert response.status_code == 401, response.text
        assert "token expired" in caplog.text

    async def test_an_unknown_token_is_refused_and_logged(
        self, gitlab_app: FastAPI, client: Any, gitlab: FakeGitLab, caplog: Any
    ) -> None:
        with caplog.at_level(logging.WARNING):
            response = await client.post(
                "/v1/session/announce", headers=auth("not-a-token-gitlab-issued")
            )
        assert response.status_code == 401, response.text
        assert "announce: token rejected: not a JWT" in caplog.text
        assert "not-a-token-gitlab-issued" not in caplog.text

    async def test_no_introspection_endpoint_says_so(
        self, gitlab_app: FastAPI, client: Any, gitlab: FakeGitLab, caplog: Any
    ) -> None:
        gitlab_app.state.gitlab_client.introspection_endpoint = None
        with caplog.at_level(logging.WARNING):
            response = await client.post("/v1/session/announce", headers=auth(gitlab.mint(CHAT_ID)))
        assert response.status_code == 401, response.text
        assert "gitlab: no introspection_endpoint" in caplog.text

    async def test_without_the_chat_secret_the_chat_token_cannot_be_vouched_for(
        self, gitlab_app: FastAPI, client: Any, gitlab: FakeGitLab, caplog: Any
    ) -> None:
        """The deployment as it was before this change: only the console's
        credentials, which GitLab will not answer for a chat token."""
        gitlab_app.state.settings.oidc.chat_client_secret = SecretStr("")
        with caplog.at_level(logging.WARNING):
            response = await client.post("/v1/session/announce", headers=auth(gitlab.mint(CHAT_ID)))
        assert response.status_code == 401, response.text
        assert "inactive for client 'pystino-console'" in caplog.text


class TestV1WithAnOpaqueToken:
    async def test_v1_calls_hit_the_cache_not_gitlab(
        self, gitlab_app: FastAPI, client: Any, gitlab: FakeGitLab
    ) -> None:
        token = gitlab.mint(CHAT_ID)
        assert (await client.post("/v1/session/announce", headers=auth(token))).status_code == 200
        calls = (gitlab.introspections, gitlab.userinfo_calls)
        assert calls == (1, 1)
        for _ in range(3):
            assert (await client.get("/v1/me", headers=auth(token))).status_code == 200
        assert (gitlab.introspections, gitlab.userinfo_calls) == calls

    async def test_the_cache_expires_and_a_revoked_token_then_fails(
        self, gitlab_app: FastAPI, client: Any, gitlab: FakeGitLab
    ) -> None:
        now = [1000.0]
        gitlab_app.state.introspection_cache = IntrospectionCache(clock=lambda: now[0])
        token = gitlab.mint(CHAT_ID)
        assert (await client.get("/v1/me", headers=auth(token))).status_code == 200
        del gitlab.tokens[token]  # revoked at GitLab
        now[0] += 59
        assert (await client.get("/v1/me", headers=auth(token))).status_code == 200
        now[0] += 2
        assert (await client.get("/v1/me", headers=auth(token))).status_code == 401
        assert gitlab.introspections == 1 + 2  # the re-ask tries chat, then console

    async def test_a_refusal_is_cached_briefly(
        self, gitlab_app: FastAPI, client: Any, gitlab: FakeGitLab
    ) -> None:
        now = [1000.0]
        gitlab_app.state.introspection_cache = IntrospectionCache(clock=lambda: now[0])
        headers = auth("never-issued-by-gitlab")
        assert (await client.get("/v1/me", headers=headers)).status_code == 401
        asked = gitlab.introspections
        assert (await client.get("/v1/me", headers=headers)).status_code == 401
        assert gitlab.introspections == asked
        now[0] += NEGATIVE_TTL_SECONDS + 1
        assert (await client.get("/v1/me", headers=headers)).status_code == 401
        assert gitlab.introspections == 2 * asked

    async def test_our_own_keys_are_never_sent_to_gitlab(
        self,
        gitlab_app: FastAPI,
        client: Any,
        gitlab: FakeGitLab,
        seeded: Any,
    ) -> None:
        assert (await client.get("/v1/me", headers=seeded.auth)).status_code == 200
        typo = generate_api_key().secret
        assert (await client.get("/v1/me", headers=auth(typo))).status_code == 401
        assert gitlab.introspections == 0

    async def test_v1_refuses_a_client_outside_accepted_clients(
        self, gitlab_app: FastAPI, client: Any, gitlab: FakeGitLab, caplog: Any
    ) -> None:
        # What the registry injects into each provider's client from
        # GATEWAY_OIDC__ACCEPTED_CLIENTS.
        gitlab_app.state.gitlab_client._settings.accepted_clients = CONSOLE_ID
        token = gitlab.mint(CHAT_ID)
        with caplog.at_level(logging.WARNING):
            response = await client.get("/v1/me", headers=auth(token))
        assert response.status_code == 401, response.text
        assert "is not in ACCEPTED_CLIENTS" in caplog.text
        assert token not in caplog.text


class TestCache:
    def test_keys_are_hashes_never_tokens(self) -> None:
        cache = IntrospectionCache()
        cache.put_negative("secret-token", "no")
        assert "secret-token" not in repr(cache._entries)
        assert cache.get("secret-token") is not None

    def test_bounded(self) -> None:
        cache = IntrospectionCache(max_entries=3)
        for i in range(5):
            cache.put_negative(f"t{i}", "no")
        assert len(cache) == 3
        assert cache.get("t0") is None and cache.get("t4") is not None

    def test_never_outlives_the_token(self) -> None:
        now = [0.0]
        wall = [10_000.0]
        cache = IntrospectionCache(clock=lambda: now[0], wall_clock=lambda: wall[0])
        cache.put_positive("t", uuid.uuid4(), {}, exp=wall[0] + 5)
        now[0] = 4.9
        assert cache.get("t") is not None
        now[0] = 5.1
        assert cache.get("t") is None
        cache.put_positive("gone", uuid.uuid4(), {}, exp=wall[0] - 1)
        assert cache.get("gone") is None
