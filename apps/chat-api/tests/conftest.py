"""Fixtures: the real application, a fake gateway, and a signed-in browser."""

from __future__ import annotations

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
    """Stands in for the gateway's ``/v1``.

    A transport rather than a monkeypatched client, so the real GatewayClient
    runs: header construction, error translation and SSE parsing are exactly
    the code that ships.
    """

    def __init__(self) -> None:
        self.models_response: httpx.Response | None = None
        self.stream_chunks: list[bytes] = []
        self.stream_status = 200
        self.stream_body: bytes | None = None
        self.seen_headers: dict[str, str] = {}
        self.seen_body: dict | None = None

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
        import json as _json

        self.stream_status = status
        self.stream_body = _json.dumps(body).encode()

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.seen_headers = dict(request.headers)
        if request.url.path == "/v1/models":
            return self.models_response or httpx.Response(200, json={"data": []})
        if request.url.path == "/v1/chat/completions":
            import json as _json

            self.seen_body = _json.loads(request.content)
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
