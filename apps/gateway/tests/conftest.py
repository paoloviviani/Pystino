"""Shared fixtures.

Tests run against SQLite in memory and a fake upstream transport, so the suite
needs no PostgreSQL, no Valkey and no network. The schema is created from the ORM
metadata; the Alembic migration is verified separately (it round-trips
upgrade/downgrade in CI and under `docker compose`).

One deliberate limitation, recorded here rather than discovered later: money
columns are ``Numeric``, which SQLite cannot store natively, so exact monetary
arithmetic is tested as pure ``Decimal`` functions in ``test_cost.py``. Database
tests use values that survive a float round-trip.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import httpx
import orjson
import pytest
import pytest_asyncio
from fastapi import FastAPI
from gateway.config import (
    QuotaSettings,
    RedactionSettings,
    Settings,
    UpstreamSettings,
)
from gateway.main import create_app, init_app_state, shutdown_app_state
from gateway.models import (
    ApiKey,
    Base,
    Group,
    GroupModelAccess,
    Membership,
    ModelDef,
    ModelPrice,
    Provider,
    User,
)
from gateway.secrets import SecretBox, hint_for
from gateway.security import generate_api_key
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

UPSTREAM_BASE = "http://fake-upstream/v1"


# --------------------------------------------------------------------------
# fake upstream
# --------------------------------------------------------------------------


class FakeUpstream:
    """A stand-in provider that records what we sent it.

    Recording the request body is the point: several tests assert that the
    gateway forced ``stream_options.include_usage`` and rewrote the model name,
    which is only observable from the upstream's side.
    """

    def __init__(self) -> None:
        self.bodies: list[dict] = []
        self.headers: list[httpx.Headers] = []
        self._responder: Callable[[httpx.Request], httpx.Response] | None = None

    @property
    def last_body(self) -> dict:
        assert self.bodies, "the upstream was never called"
        return self.bodies[-1]

    def set_json(
        self, payload: dict, *, status: int = 200, headers: dict[str, str] | None = None
    ) -> None:
        """A JSON response, optionally with headers.

        `headers` exists because some counterparties report facts there rather
        than in the body — a router naming the sub-provider that served the
        request is the case that matters (ADR 0032), and it cannot be simulated
        from the payload alone.
        """

        def responder(_: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json=payload, headers=headers)

        self._responder = responder

    def set_raw(self, body: bytes, *, status: int, content_type: str = "application/json") -> None:
        def responder(_: httpx.Request) -> httpx.Response:
            return httpx.Response(status, content=body, headers={"content-type": content_type})

        self._responder = responder

    def set_stream(self, chunks: list[bytes], *, status: int = 200) -> None:
        """Reply with an SSE stream delivered as exactly these byte chunks."""

        def responder(_: httpx.Request) -> httpx.Response:
            async def stream() -> AsyncIterator[bytes]:
                for chunk in chunks:
                    yield chunk

            return httpx.Response(
                status,
                content=stream(),
                headers={"content-type": "text/event-stream"},
            )

        self._responder = responder

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.headers.append(request.headers)
        self.bodies.append(orjson.loads(request.content) if request.content else {})
        assert self._responder is not None, "no upstream response configured"
        return self._responder(request)

    def client(self) -> httpx.AsyncClient:
        # Same timeout shape as production: read must be None for streams.
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self._handle),
            timeout=httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0),
        )


@pytest.fixture
def fake_upstream() -> FakeUpstream:
    return FakeUpstream()


# --------------------------------------------------------------------------
# settings, app, database
# --------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # A file per test rather than ":memory:". In-memory SQLite needs a single
    # pooled connection to be shared across sessions, and that pool does not
    # always close cleanly at interpreter teardown; a temp file exercises a real
    # connection pool and leaves nothing behind.
    return Settings(
        environment="dev",
        log_json=False,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'gateway-test.db'}",
        # Empty means "no shared counter store": the in-memory store is used,
        # which is exactly right for a single-process test.
        valkey_url="",
        billing_currency="EUR",
        session_secret="test-secret-not-for-production",
        # Encrypts provider credentials (ADR 0027). Distinct from the session
        # secret on purpose, and the fixtures below need it to store a key.
        secret_key="test-encryption-key-not-for-production",
        session_cookie_secure=False,
        persist_transcripts=True,
        # Flush on every event so transcript persistence is observable without
        # sleeping in tests.
        transcript_flush_chars=1,
        transcript_flush_seconds=0.0,
        upstream=UpstreamSettings(base_url=UPSTREAM_BASE, api_key="upstream-key"),
        redaction=RedactionSettings(engine="noop"),
        quota=QuotaSettings(enabled=True, default_max_output_tokens=1000),
    )


@pytest_asyncio.fixture
async def app(settings: Settings, fake_upstream: FakeUpstream) -> AsyncIterator[FastAPI]:
    """The real application, wired by the real init code, with a fake upstream."""
    application = create_app(settings)
    await init_app_state(
        application,
        settings,
        upstream_http=fake_upstream.client(),
        control_http=httpx.AsyncClient(),
    )

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
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as db_session:
        yield db_session


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as http:
        yield http


# --------------------------------------------------------------------------
# seeded fixtures
# --------------------------------------------------------------------------


@dataclass
class Seeded:
    user: User
    group: Group
    provider: Provider
    model: ModelDef
    api_key: ApiKey
    secret: str

    @property
    def auth(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.secret}"}


@pytest_asyncio.fixture
async def seeded(session_factory: async_sessionmaker[AsyncSession]) -> Seeded:
    """A group, a member, a priced model the group may use, and a usable key.

    Prices are chosen so the arithmetic is easy to assert by hand:
    1 EUR per million input tokens, 2 EUR per million output tokens.
    """
    async with session_factory() as db:
        group = Group(name="research", description="test group")
        db.add(group)
        await db.flush()

        user = User(
            issuer="https://idp.test",
            subject="subject-1",
            email="member@example.org",
            display_name="Test Member",
            default_billing_group_id=group.id,
        )
        db.add(user)
        await db.flush()
        db.add(Membership(user_id=user.id, group_id=group.id))

        # Every model needs a provider now (ADR 0027); the fake upstream is one.
        # Its credential is stored the way a real one is — encrypted — so the
        # tests exercise the decrypt-and-send path rather than skipping it.
        box = SecretBox(["test-encryption-key-not-for-production"])
        provider = Provider(
            name="fake",
            base_url=UPSTREAM_BASE,
            api_key_encrypted=box.encrypt("upstream-key"),
            api_key_hint=hint_for("upstream-key"),
        )
        db.add(provider)
        await db.flush()

        model = ModelDef(
            name="test-model",
            upstream_model="upstream/test-model",
            provider_id=provider.id,
            context_window=8192,
        )
        db.add(model)
        await db.flush()
        db.add(
            ModelPrice(
                model_id=model.id,
                input_per_mtok=Decimal("1"),
                output_per_mtok=Decimal("2"),
                currency="EUR",
            )
        )
        db.add(GroupModelAccess(group_id=group.id, model_id=model.id))

        generated = generate_api_key()
        api_key = ApiKey(
            user_id=user.id,
            prefix=generated.prefix,
            key_hash=generated.key_hash,
            name="test key",
            billing_group_id=group.id,
        )
        db.add(api_key)
        await db.commit()

        # Re-read with relationships loaded, detached from this session.
        await db.refresh(user)
        await db.refresh(group)
        await db.refresh(model)
        await db.refresh(api_key)

        return Seeded(
            user=user,
            group=group,
            provider=provider,
            model=model,
            api_key=api_key,
            secret=generated.secret,
        )


# SSE/payload builders live in helpers.py so test modules can import them
# directly; re-exported here purely for convenience.
__all__ = ["UPSTREAM_BASE", "FakeUpstream", "Seeded"]
