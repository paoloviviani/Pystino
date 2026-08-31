"""Fixtures: the real application, a fake gateway, and a signed-in browser."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from chat_api.config import GatewaySettings, OIDCSettings, Settings
from chat_api.deps import SESSION_COOKIE
from chat_api.main import create_app, init_app_state, shutdown_app_state
from chat_api.models import Base, Session
from chat_api.oidc import issue_session_cookie
from chat_api.secrets import SecretBox
from chat_api.types import utcnow
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

GATEWAY_BASE = "http://gateway.test"
SESSION_SECRET = "chat-test-session-secret"
ENCRYPTION_KEY = "chat-test-encryption-key"


class FakeGateway:
    """Stands in for the gateway's ``/v1`` and its local-credential routes.

    A transport rather than a monkeypatched client, so the real GatewayClient
    runs: header construction, error translation and SSE parsing are exactly
    the code that ships.

    The auth half (ADR 0046) issues and judges opaque ``gwr_``/``gwa_``
    credentials the way the real gateway does — hashed comparison, expiry,
    revocation — because the point of these tests is that chat-api's session
    lifecycle matches the gateway's, not a stub's idea of it.
    """

    def __init__(self) -> None:
        self.models_response: httpx.Response | None = None
        self.stream_chunks: list[bytes] = []
        self.stream_status = 200
        self.stream_body: bytes | None = None
        self.seen_headers: dict[str, str] = {}
        self.seen_body: dict | None = None
        # Refresh credential -> {"access": ..., "expired": bool}; issued by the
        # fake /auth/login, judged by the fake /auth/token and /auth/revoke.
        self.refresh_credentials: dict[str, dict] = {}
        self.access_credentials: dict[str, dict] = {}
        self.local_login_response: tuple[int, dict] | None = None
        self.methods = {"local": True, "oidc": False}
        # The whole gateway unreachable: every request raises ConnectError.
        # Tests what chat-api does when the gateway *fails* rather than
        # refuses — the distinction the session lifecycle is built on.
        self.down = False

    def set_models(
        self, ids: list[str], *, status: int = 200, features: list[str] | None = None
    ) -> None:
        self.models_response = httpx.Response(
            status,
            json={
                "object": "list",
                "data": [
                    {"id": i, "owned_by": "test", "supported_features": features or []}
                    for i in ids
                ],
            },
        )

    def set_stream(self, chunks: list[bytes]) -> None:
        self.stream_chunks = chunks
        self.stream_status = 200
        self.stream_body = None

    def set_stream_error(self, status: int, body: dict) -> None:

        self.stream_status = status
        self.stream_body = json.dumps(body).encode()

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("gateway down", request=request)
        self.seen_headers = dict(request.headers)
        if request.url.path == "/v1/models":
            return self.models_response or httpx.Response(200, json={"data": []})
        if request.url.path == "/auth/methods":
            return httpx.Response(200, json=self.methods)
        if request.url.path == "/auth/login":
            return self._handle_local_login(request)
        if request.url.path == "/auth/token":
            return self._handle_token(request)
        if request.url.path == "/auth/revoke":
            body = json.loads(request.content)
            self.refresh_credentials.pop(body.get("refresh_token", ""), None)
            return httpx.Response(204)
        if request.url.path == "/v1/chat/completions":

            self.seen_body = json.loads(request.content)
            if self.stream_body is not None:
                return httpx.Response(
                    self.stream_status,
                    content=self.stream_body,
                    headers={"content-type": "application/json"},
                )

            async def stream() -> AsyncIterator[bytes]:
                for chunk in self.stream_chunks:
                    yield chunk

            return httpx.Response(
                200,
                stream=_Iter(stream()),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(404, json={"error": {"message": "no such route"}})

    def _handle_local_login(self, request: httpx.Request) -> httpx.Response:
        if self.local_login_response is not None:
            status, payload = self.local_login_response
            return httpx.Response(status, json=payload)
        body = json.loads(request.content)
        # The real gateway casefolds the address before the lookup — that is
        # the behaviour the casefold test exists to pin — so the fake matches
        # the same way.
        if (
            body.get("email", "").casefold() != "person@example.org"
            or body.get("password") != "good password"
        ):
            return httpx.Response(
                401,
                json={
                    "error": {
                        "message": "Incorrect email or password.",
                        "code": "invalid_credentials",
                    }
                },
            )
        refresh = f"gwr_test_{len(self.refresh_credentials) + 1:04d}"
        self.refresh_credentials[refresh] = {"expired": False}
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "refresh_token": refresh,
                "email": body["email"],
                "display_name": "Person",
                "groups": ["research"],
                "is_admin": False,
            },
        )

    def _handle_token(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        refresh = body.get("refresh_token", "")
        record = self.refresh_credentials.get(refresh)
        if record is None or record["expired"]:
            return httpx.Response(
                401,
                json={"error": {"message": "The refresh credential is not valid."}},
            )
        access = f"gwa_test_{len(self.access_credentials) + 1:04d}"
        self.access_credentials[access] = {"refresh": refresh}
        return httpx.Response(
            200,
            json={"access_token": access, "token_type": "bearer", "expires_in": 900},
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self._handle), base_url=GATEWAY_BASE
        )


class _Iter(httpx.AsyncByteStream):
    def __init__(self, iterator: AsyncIterator[bytes]) -> None:
        self._iterator = iterator

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._iterator:
            yield chunk


@pytest.fixture
def fake_gateway() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        environment="dev",
        log_json=False,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'chat-test.db'}",
        session_secret=SESSION_SECRET,
        secret_key=ENCRYPTION_KEY,
        session_cookie_secure=False,
        spa_enabled=False,
        oidc=OIDCSettings(
            issuer="https://idp.test/realms/llm-platform",
            client_id="llm-chat",
            client_secret="chat-secret",
            redirect_uri="http://localhost:8100/auth/callback",
        ),
        gateway=GatewaySettings(base_url=GATEWAY_BASE),
    )


class StubOIDC:
    """Refreshes without a network. Returns a token the fake gateway accepts."""

    def __init__(self) -> None:
        self.refresh_calls = 0
        self.next_refresh_token: str | None = None
        self.fail = False

    async def refresh(self, refresh_token: str) -> tuple[str, str | None]:
        from chat_api.oidc import OIDCError

        self.refresh_calls += 1
        if self.fail:
            raise OIDCError("refresh token was rejected")
        return f"access-for-{refresh_token}", self.next_refresh_token or refresh_token


@pytest_asyncio.fixture
async def app(settings: Settings, fake_gateway: FakeGateway) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    await init_app_state(
        application,
        settings,
        gateway_http=fake_gateway.client(),
        control_http=httpx.AsyncClient(),
    )
    application.state.oidc_client = StubOIDC()

    async with application.state.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield application
    finally:
        await shutdown_app_state(application)


@pytest.fixture
def session_factory(app: FastAPI) -> async_sessionmaker[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = app.state.session_factory
    return factory


@pytest_asyncio.fixture
async def signed_in(
    app: FastAPI, session_factory: async_sessionmaker[AsyncSession]
) -> Callable[..., dict[str, str]]:
    """Create a session row and return the cookie header for it."""
    box = SecretBox([ENCRYPTION_KEY])

    async def make(
        subject: str = "subject-1",
        *,
        groups: list[str] | None = None,
        is_admin: bool = False,
    ) -> dict[str, str]:
        async with session_factory() as db:
            session = Session(
                id=uuid.uuid4(),
                issuer="https://idp.test/realms/llm-platform",
                subject=subject,
                email=f"{subject}@example.org",
                display_name=subject.title(),
                groups=groups or ["research"],
                is_admin=is_admin,
                refresh_token_encrypted=box.encrypt(f"refresh-{subject}"),
                expires_at=utcnow() + timedelta(hours=1),
            )
            db.add(session)
            await db.commit()
            cookie = issue_session_cookie(
                session.id, secret=SESSION_SECRET, ttl_seconds=3600
            )
        return {"cookie": f"{SESSION_COOKIE}={cookie}"}

    return make


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://chat") as http:
        yield http
