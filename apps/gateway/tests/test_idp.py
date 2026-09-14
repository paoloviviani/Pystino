"""The house issuer (ADR 0068).

Each test named in the ADR is here: discovery's shape and its absence when
disabled; the client and redirect checks that run before any authentication;
PKCE mandatory and downgrade refused; single-use codes bound to client and
redirect; an ``id_token`` whose signature the deployment's own JWKS verifies
and whose ``sub`` is the local row's subject; opaque access credentials that
open ``/v1`` like any key; refresh rotation that kills the old credential;
``end_session`` ending the session it is handed; and the switch, which off
means absent — the management doors untouched.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from conftest import FakeUpstream
from fastapi import FastAPI
from gateway.config import Settings
from gateway.idp import IdpSigner, pkce_s256
from gateway.main import create_app, init_app_state, shutdown_app_state
from gateway.models import LocalCredential, User
from gateway.oidc import issue_session_token
from gateway.types import utcnow
from joserfc import jwt
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ISSUER = "https://cerea.test"
INTERNAL = "http://gateway.internal:8000"
MINT_TOKEN = "internal-mint-token-not-a-secret"
REDIRECT_PATH = "/chat/login/callback"
SESSION_COOKIE = "gw_session"


def _a_signing_key() -> str:
    """A throwaway ES256 key. Test-only; the deployment brings its own."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


@pytest_asyncio.fixture
async def idp_app(
    settings: Settings, fake_upstream: FakeUpstream
) -> AsyncIterator[FastAPI]:
    """The app with the IdP enabled, wired by the real init code.

    Two registered clients: the chat, and a second first-party client used to
    prove that a credential minted for one is not another's.
    """
    from gateway.config import IdPClientSettings

    settings.idp.enabled = True
    settings.idp.issuer = ISSUER
    settings.idp.internal_base_url = INTERNAL
    settings.idp.signing_key = SecretStr(_a_signing_key())
    settings.idp.internal_token = SecretStr(MINT_TOKEN)
    settings.idp.clients = [
        IdPClientSettings(client_id="cerea", redirect_path=REDIRECT_PATH),
        IdPClientSettings(client_id="desktop", redirect_path="/desktop/callback"),
    ]

    # No console in this app, by construction rather than by checkout: the
    # end_session tests assert the console-less answers (a refused foreign
    # landing is an honest 200 with nowhere to fall back to), and the source
    # tree's build-artifact directory must not get a vote. Same
    # never-a-real-directory as test_console.py uses for the same purpose.
    settings = settings.model_copy(update={"console_dir": "/nonexistent"})

    application = create_app(settings)
    # Schema first, wiring second — the reverse of the shared app fixture.
    # init_app_state starts the FX poller, whose *first* refresh runs
    # immediately rather than after its hour, so a table-less database turns
    # that refresh into a race the shared fixture keeps winning only by
    # timing. The schema is laid with a throwaway engine, because the app's
    # own only exists once the wiring has run.
    from gateway.db import create_engine
    from gateway.models import Base

    schema_engine = create_engine(settings)
    async with schema_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await schema_engine.dispose()
    await init_app_state(
        application,
        settings,
        upstream_http=fake_upstream.client(),
        control_http=httpx.AsyncClient(),
    )
    try:
        yield application
    finally:
        await shutdown_app_state(application)


@pytest_asyncio.fixture
async def idp_client(idp_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=idp_app)
    async with httpx.AsyncClient(transport=transport, base_url=ISSUER) as http:
        yield http


async def _a_local_user(
    session_factory: async_sessionmaker[AsyncSession], *, email: str | None = None
) -> User:
    """A local account, the kind the house issuer issues for.

    One group and a default, because a groupless caller has nothing to bill
    and `/v1` answers 403 by design. A unique address per call, because
    `(issuer, subject)` is unique and several tests need several people.
    """
    address = email or f"person-{uuid.uuid4().hex[:8]}@example.org"
    async with session_factory() as db:
        user = User(issuer="local", subject=address, email=address, display_name="Person")
        db.add(user)
        await db.flush()
        db.add(
            LocalCredential(
                user_id=user.id,
                password_hash="not-a-real-hash",
            )
        )
        from gateway.models import Group, GroupSource, Membership

        group = Group(name=f"team-{uuid.uuid4().hex[:8]}", source=GroupSource.MANUAL)
        db.add(group)
        await db.flush()
        db.add(Membership(user_id=user.id, group_id=group.id))
        user.default_billing_group_id = group.id
        await db.commit()
        return user


def _sign_in(idp_app: FastAPI, idp_client: httpx.AsyncClient, user: User) -> None:
    """Give the test client the management cookie, minted as both doors mint it.

    Set on the client rather than per request: httpx deprecates per-request
    cookies, and this deployment treats warnings as failures.
    """
    settings = idp_app.state.settings
    idp_client.cookies.set(
        SESSION_COOKIE,
        issue_session_token(
            user.id,
            secret=settings.session_secret.get_secret_value(),
            ttl_seconds=settings.session_ttl_seconds,
        ),
    )


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(hashlib.sha256(b"a verifier").digest()).decode()
    return verifier, pkce_s256(verifier)


def _authorize_url(
    *, redirect_uri: str | None = None, client_id: str = "cerea", **extra: str
) -> str:
    _verifier, challenge = _pkce()
    parameters: dict[str, str] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri or f"{ISSUER}{REDIRECT_PATH}",
        "scope": "openid profile email",
        "state": "a-state-value",
        "nonce": "a-nonce-value",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    parameters.update(extra)
    from urllib.parse import urlencode

    return f"/oauth/authorize?{urlencode(parameters)}"


class TestDiscovery:
    async def test_the_document_a_standards_client_builds_from(
        self, idp_client: httpx.AsyncClient
    ) -> None:
        """Cerea's ``openid-client`` discovers against this and refuses to
        build a flow if a field it needs is missing — so the shape is the
        contract, not decoration."""
        response = await idp_client.get("/.well-known/openid-configuration")
        assert response.status_code == 200, response.text
        document = response.json()
        assert document["issuer"] == ISSUER
        # Browser-facing endpoints are public; the endpoints only another
        # backend fetches are advertised at the internal base, which is what
        # makes the compose network the minting path rather than the edge.
        assert document["authorization_endpoint"] == f"{ISSUER}/oauth/authorize"
        assert document["end_session_endpoint"] == f"{ISSUER}/oauth/end_session"
        assert document["token_endpoint"] == f"{INTERNAL}/oauth/token"
        assert document["jwks_uri"] == f"{INTERNAL}/oauth/jwks.json"
        assert document["userinfo_endpoint"] == f"{INTERNAL}/oauth/userinfo"
        assert document["id_token_signing_alg_values_supported"] == ["ES256"]
        assert set(document["grant_types_supported"]) == {"authorization_code", "refresh_token"}
        assert document["code_challenge_methods_supported"] == ["S256"]

    async def test_the_jwks_verifies_what_the_issuer_signs(
        self, idp_app: FastAPI, idp_client: httpx.AsyncClient
    ) -> None:
        response = await idp_client.get("/oauth/jwks.json")
        assert response.status_code == 200
        keys = response.json()["keys"]
        assert len(keys) == 1
        assert keys[0]["kty"] == "EC"
        # Round-trip: a token signed by the private key verifies against this.
        signer: IdpSigner = idp_app.state.idp_signer
        token = signer.sign_id_token({"iss": ISSUER, "sub": "x", "exp": 9999999999})
        decoded = jwt.decode(token, signer.key_set())
        assert decoded.claims["sub"] == "x"

    async def test_off_means_absent(self, client: httpx.AsyncClient) -> None:
        """The default app has no IdP: no discovery, no authorize, nothing.

        The client fixture is the IdP-less one deliberately — off means the
        routes were never registered, not that they answer 403.
        """
        assert (await client.get("/.well-known/openid-configuration")).status_code == 404
        assert (await client.get("/oauth/authorize")).status_code == 404


class TestAuthorize:
    async def test_an_unknown_client_is_refused_without_a_redirect(
        self, idp_client: httpx.AsyncClient
    ) -> None:
        """Before client and redirect are validated, nothing may be trusted —
        an error redirect to an unvalidated URI is an open redirect wearing
        the spec's clothes."""
        response = await idp_client.get(
            "/oauth/authorize",
            params={"response_type": "code", "client_id": "who-is-this"},
        )
        assert response.status_code == 400
        assert "not registered" in response.text

    async def test_an_unregistered_redirect_is_refused(
        self, idp_client: httpx.AsyncClient
    ) -> None:
        response = await idp_client.get(
            _authorize_url(redirect_uri="https://attacker.test/callback")
        )
        assert response.status_code == 400
        assert "not registered" in response.text

    async def test_pkce_is_mandatory_and_s256_only(
        self, idp_client: httpx.AsyncClient
    ) -> None:
        missing = await idp_client.get(
            _authorize_url(code_challenge="", code_challenge_method="")
        )
        assert missing.status_code == 302
        assert "error=invalid_request" in missing.headers["location"]

        verifier, _ = _pkce()
        downgrade = await idp_client.get(
            _authorize_url(
                code_challenge=hashlib.sha256(verifier.encode()).hexdigest(),
                code_challenge_method="plain",
            )
        )
        assert downgrade.status_code == 302
        assert "error=invalid_request" in downgrade.headers["location"]

    async def test_a_signed_in_browser_gets_a_single_use_code(
        self,
        idp_app: FastAPI,
        idp_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from gateway.models import IdpAuthorizationCode
        from sqlalchemy import select

        user = await _a_local_user(session_factory)
        _sign_in(idp_app, idp_client, user)
        response = await idp_client.get(_authorize_url())
        assert response.status_code == 302, response.text
        location = response.headers["location"]
        assert location.startswith(f"{ISSUER}{REDIRECT_PATH}?")
        assert "code=" in location and "state=a-state-value" in location

        # Exactly one row, stored as a hash: the response carries none of the
        # code in the clear and the table holds nothing that works.
        async with session_factory() as db:
            codes = (await db.execute(select(IdpAuthorizationCode))).scalars().all()
        assert len(codes) == 1
        assert codes[0].code_hash != location.split("code=")[1].split("&")[0]


class TestToken:
    async def _code_for(
        self,
        idp_app: FastAPI,
        idp_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        email: str | None = None,
    ) -> str:
        from urllib.parse import parse_qs, urlparse

        user = await _a_local_user(session_factory, email=email)
        _sign_in(idp_app, idp_client, user)
        response = await idp_client.get(_authorize_url())
        assert response.status_code == 302, response.text
        return parse_qs(urlparse(response.headers["location"]).query)["code"][0]

    async def _exchange(
        self,
        idp_client: httpx.AsyncClient,
        code: str,
        *,
        verifier: str | None = None,
        redirect_uri: str | None = None,
        auth: tuple[str, str] | None = None,
    ) -> httpx.Response:
        real_verifier, _ = _pkce()
        data: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": "cerea",
            "redirect_uri": redirect_uri or f"{ISSUER}{REDIRECT_PATH}",
            "code_verifier": verifier or real_verifier,
        }
        headers = {}
        if auth:
            encoded = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
            headers["authorization"] = f"Basic {encoded}"
        return await idp_client.post("/oauth/token", data=data, headers=headers)

    async def test_the_exchange_mints_opaque_credentials_and_an_id_token(
        self,
        idp_app: FastAPI,
        idp_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        code = await self._code_for(
            idp_app, idp_client, session_factory, email="person@example.org"
        )
        response = await self._exchange(idp_client, code)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["access_token"].startswith("gwa_")
        assert body["refresh_token"].startswith("gwr_")
        assert body["expires_in"] == 900

        # The id_token verifies against the issuer's own JWKS and names the
        # local row: iss is the configured issuer, aud is the client, and sub
        # is the casefolded email — the aliasing rule of ADR 0068 made flesh.
        signer: IdpSigner = idp_app.state.idp_signer
        decoded = jwt.decode(body["id_token"], signer.key_set())
        assert decoded.claims["iss"] == ISSUER
        assert decoded.claims["aud"] == "cerea"
        assert decoded.claims["sub"] == "person@example.org"
        assert decoded.claims["nonce"] == "a-nonce-value"
        assert decoded.claims["email"] == "person@example.org"
        assert decoded.claims["email_verified"] is True

    async def test_a_code_is_single_use(
        self,
        idp_app: FastAPI,
        idp_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        code = await self._code_for(idp_app, idp_client, session_factory)
        first = await self._exchange(idp_client, code)
        assert first.status_code == 200
        second = await self._exchange(idp_client, code)
        assert second.status_code == 400
        assert second.json()["error"] == "invalid_grant"

    async def test_a_wrong_verifier_or_redirect_is_refused(
        self,
        idp_app: FastAPI,
        idp_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        code = await self._code_for(idp_app, idp_client, session_factory)
        bad_verifier = await self._exchange(
            idp_client, code, verifier="not-the-verifier-that-made-the-challenge"
        )
        assert bad_verifier.json()["error"] == "invalid_grant"

        code = await self._code_for(idp_app, idp_client, session_factory)
        bad_redirect = await self._exchange(
            idp_client, code, redirect_uri=f"{ISSUER}/somewhere/else"
        )
        assert bad_redirect.json()["error"] == "invalid_grant"

    async def test_the_access_token_opens_v1_like_any_key(
        self,
        idp_app: FastAPI,
        idp_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The whole point of the substrate: /v1 learns nothing new."""
        code = await self._code_for(
            idp_app, idp_client, session_factory, email="person@example.org"
        )
        body = (await self._exchange(idp_client, code)).json()
        me = await idp_client.get(
            "/v1/me", headers={"authorization": f"Bearer {body['access_token']}"}
        )
        assert me.status_code == 200, me.text
        assert me.json()["email"] == "person@example.org"

    async def test_refresh_rotates_and_the_old_credential_dies(
        self,
        idp_app: FastAPI,
        idp_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Rotation is what keeps the flow alive at all: the refresh secret is
        stored hashed, so a response that returned nothing would strand the
        client one access TTL from a dead session."""
        code = await self._code_for(idp_app, idp_client, session_factory)
        first = (await self._exchange(idp_client, code)).json()

        rotated = await idp_client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": first["refresh_token"],
                "client_id": "cerea",
            },
        )
        assert rotated.status_code == 200, rotated.text
        second = rotated.json()
        assert second["refresh_token"] != first["refresh_token"]
        assert second["access_token"].startswith("gwa_")

        # The old refresh is dead — not expired, *spent* — and the new one works.
        replay = await idp_client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": first["refresh_token"],
                "client_id": "cerea",
            },
        )
        assert replay.status_code == 400
        assert replay.json()["error"] == "invalid_grant"

        again = await idp_client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": second["refresh_token"],
                "client_id": "cerea",
            },
        )
        assert again.status_code == 200

        # And a credential minted for one client is not another's: cerea's
        # refresh presented at the token endpoint as the desktop client names
        # a family that is not its own.
        stranger = await idp_client.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": second["refresh_token"],
                "client_id": "desktop",
            },
        )
        assert stranger.status_code == 400
        assert stranger.json()["error"] == "invalid_grant"

    async def test_userinfo_answers_with_the_same_subject(
        self,
        idp_app: FastAPI,
        idp_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        code = await self._code_for(
            idp_app, idp_client, session_factory, email="person@example.org"
        )
        body = (await self._exchange(idp_client, code)).json()
        info = await idp_client.get(
            "/oauth/userinfo", headers={"authorization": f"Bearer {body['access_token']}"}
        )
        assert info.status_code == 200, info.text
        assert info.json()["sub"] == "person@example.org"

        # The id_token is an identity document, not a credential.
        refused = await idp_client.get(
            "/oauth/userinfo", headers={"authorization": f"Bearer {body['id_token']}"}
        )
        assert refused.status_code == 400


class TestEndSession:
    async def test_it_ends_the_session_and_honours_a_same_origin_landing(
        self,
        idp_app: FastAPI,
        idp_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        user = await _a_local_user(session_factory)
        _sign_in(idp_app, idp_client, user)
        response = await idp_client.get(
            "/oauth/end_session",
            params={"post_logout_redirect_uri": f"{ISSUER}/chat/", "state": "s1"},
        )
        assert response.status_code == 302
        assert response.headers["location"] == f"{ISSUER}/chat/?state=s1"
        # The session cookie is cleared in the response, not merely ignored.
        cleared = response.headers["set-cookie"]
        assert SESSION_COOKIE in cleared
        assert "Max-Age=0" in cleared or "expires=" in cleared.lower()

    async def test_a_foreign_landing_is_refused_quietly(
        self, idp_client: httpx.AsyncClient
    ) -> None:
        """A logout that redirects wherever the URL says is a logout button
        that phishes; an off-origin landing is refused. This test app serves
        no console, so there is nowhere to fall back to and the answer is the
        honest 200 — the session was still ended."""
        response = await idp_client.get(
            "/oauth/end_session",
            params={"post_logout_redirect_uri": "https://attacker.test/"},
        )
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestTheSwitchAndTheInternals:
    async def test_the_legacy_minting_endpoints_need_the_internal_token(
        self,
        idp_app: FastAPI,
        idp_client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The 0046 endpoints leave the public internet when the IdP is on."""
        user = await _a_local_user(session_factory)
        async with session_factory() as db:
            from gateway.models import RefreshCredential
            from gateway.security import generate_api_key

            generated = generate_api_key(environment_prefix="gwr")
            db.add(
                RefreshCredential(
                    user_id=user.id,
                    client="some-legacy-client",
                    prefix=generated.prefix,
                    secret_hash=generated.key_hash,
                    expires_at=utcnow() + timedelta(hours=1),
                )
            )
            await db.commit()

        refused = await idp_client.post(
            "/auth/token", json={"refresh_token": generated.secret}
        )
        assert refused.status_code == 401

        answered = await idp_client.post(
            "/auth/token",
            json={"refresh_token": generated.secret},
            headers={"x-mint-token": MINT_TOKEN},
        )
        assert answered.status_code == 200, answered.text
        assert answered.json()["access_token"].startswith("gwa_")

    async def test_without_the_idp_the_legacy_endpoints_are_as_they_were(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Off means nothing changed for a deployment that never enabled it."""
        user = User(issuer="local", subject=f"{uuid.uuid4()}@example.org")
        async with session_factory() as db:
            from gateway.models import RefreshCredential
            from gateway.security import generate_api_key

            db.add(user)
            await db.flush()
            generated = generate_api_key(environment_prefix="gwr")
            db.add(
                RefreshCredential(
                    user_id=user.id,
                    client="legacy",
                    prefix=generated.prefix,
                    secret_hash=generated.key_hash,
                    expires_at=utcnow() + timedelta(hours=1),
                )
            )
            await db.commit()

        answered = await client.post("/auth/token", json={"refresh_token": generated.secret})
        assert answered.status_code == 200, answered.text

    async def test_a_misconfigured_idp_refuses_to_start(self) -> None:
        """Enabled but unconfigured is a startup error, not a first-login one."""
        with pytest.raises(ValueError, match="GATEWAY_IDP__ISSUER"):
            Settings(idp={"enabled": True})  # type: ignore[arg-type]
