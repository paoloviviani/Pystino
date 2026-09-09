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
from gateway.accounting.cost import TokenCounts
from gateway.config import (
    OIDCSettings,
    QuotaSettings,
    RedactionSettings,
    Settings,
    UpstreamSettings,
)
from gateway.main import create_app, init_app_state, shutdown_app_state
from gateway.models import (
    ApiKey,
    ApiSurface,
    Base,
    Group,
    GroupModelAccess,
    IdentityProvider,
    Membership,
    MembershipSource,
    ModelDef,
    ModelPrice,
    Provider,
    User,
)
from gateway.oidc import OIDCClient, OIDCMetadata
from gateway.plugins import registry as plugin_registry
from gateway.plugins.base import ProviderKind, ReportedCost
from gateway.secrets import SecretBox, hint_for
from gateway.security import generate_api_key
from gateway.types import utcnow
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey
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
        # This user's issuer is a directory, so the membership is one a login
        # made — which is what keeps the revocation tests meaningful (ADR 0057).
        db.add(
            Membership(
                user_id=user.id, group_id=group.id, source=MembershipSource.OIDC
            )
        )

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


# -- test-only provider plugins ----------------------------------------------
#
# Since ADR 0032 slice 3, what unit a counterparty reports its cost in is the
# plugin's knowledge and not a column an operator fills in. These two exist so
# the tests that used to set `providers.upstream_cost_unit` can still make their
# point, and they make it against the real seam: a provider row names a plugin,
# the registry resolves it, and the recorder reads what it returns.


class DollarShopPlugin:
    """A router that charges in dollars and means it.

    The case that matters is not conversion — it is that a foreign figure is
    recorded in its own unit and never summed into a euro total. Authoritative,
    so pass-through billing is selectable for it and the *currency* is the only
    thing standing between its figure and the invoice.
    """

    name = "dollar-shop"
    label = "Dollar shop (test)"
    description = "Reports an authoritative charge in US dollars."
    kind = ProviderKind.ROUTER
    reports_authoritative_cost = True

    def auth_headers(self, credential: str) -> dict[str, str]:
        return {"authorization": f"Bearer {credential}"}

    def prepare_payload(
        self, payload: dict[str, object], *, surface: ApiSurface
    ) -> dict[str, object]:
        return payload

    def read_usage(self, usage: dict[str, object] | None, *, surface: ApiSurface) -> TokenCounts:
        return TokenCounts.from_usage(usage)

    def read_served_by(self, payload: object, headers: object) -> None:
        return None

    def read_reported_cost(self, usage: dict[str, object] | None) -> ReportedCost | None:
        if not usage or (raw := usage.get("cost")) is None or isinstance(raw, bool):
            return None
        return ReportedCost(amount=Decimal(str(raw)), currency="USD", authoritative=True)


class IndicativeCostPlugin:
    """Reads a figure and asserts nothing about it.

    The distinction ADR 0032 turns on: reporting a number is not claiming it is
    the charge. Everything about this row can be configured for pass-through and
    the request path must still bill from our own prices, recording the figure
    beside the one it did not use.
    """

    name = "indicative"
    label = "Indicative cost (test)"
    description = "Reports a euro figure it does not stand behind."
    kind = ProviderKind.PROVIDER
    reports_authoritative_cost = False

    def auth_headers(self, credential: str) -> dict[str, str]:
        return {"authorization": f"Bearer {credential}"}

    def prepare_payload(
        self, payload: dict[str, object], *, surface: ApiSurface
    ) -> dict[str, object]:
        return payload

    def read_usage(self, usage: dict[str, object] | None, *, surface: ApiSurface) -> TokenCounts:
        return TokenCounts.from_usage(usage)

    def read_served_by(self, payload: object, headers: object) -> None:
        return None

    def read_reported_cost(self, usage: dict[str, object] | None) -> ReportedCost | None:
        if not usage or (raw := usage.get("cost")) is None or isinstance(raw, bool):
            return None
        # Micro-EUR, like the reference router, so the arithmetic in a test that
        # compares the two figures is the same arithmetic.
        return ReportedCost(
            amount=Decimal(str(raw)) * Decimal("0.000001"), currency="EUR", authoritative=False
        )


@pytest.fixture(autouse=True)
def test_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register the two above for every test.

    Autouse and unconditional: a plugin name is resolved lazily, deep in the
    request path, so a test that forgot to ask for the fixture would fail with
    "unknown provider plugin" from three frames inside the recorder rather than
    from its own setup.
    """
    monkeypatch.setitem(plugin_registry._BUILTIN, DollarShopPlugin.name, DollarShopPlugin)
    monkeypatch.setitem(plugin_registry._BUILTIN, IndicativeCostPlugin.name, IndicativeCostPlugin)


# --------------------------------------------------------------------------
# OIDC access tokens on /v1 (ADR 0040)
#
# Here rather than in test_bearer_auth.py because the query-count suite pins the
# bearer path's round trips and needs the same fixtures.
# --------------------------------------------------------------------------

BEARER_ISSUER = "https://idp.test"
BEARER_AUDIENCE = "llm-gateway"


class _Absent:
    """Sentinel for `make_token(aud=ABSENT)` — a claim that is not merely empty.

    A live Keycloak access token has no `aud` key at all unless a mapper adds
    one, and "missing" and "empty list" are different tests.
    """


ABSENT = _Absent()


class StubOIDCClient(OIDCClient):
    """An OIDCClient whose discovery and JWKS are local.

    Subclassed rather than mocked so every line of ``validate_access_token`` —
    including the unknown-``kid`` retry — runs as it does in production.
    """

    def __init__(self, oidc_settings: OIDCSettings, key: RSAKey) -> None:
        super().__init__(oidc_settings, http=None)  # type: ignore[arg-type]
        self._key = key

    async def metadata(self) -> OIDCMetadata:
        return OIDCMetadata(
            issuer=BEARER_ISSUER,
            authorization_endpoint=f"{BEARER_ISSUER}/auth",
            token_endpoint=f"{BEARER_ISSUER}/token",
            jwks_uri=f"{BEARER_ISSUER}/jwks",
            # Published, because every provider this deployment is pointed at
            # publishes one and its absence made `logout` untestable: the route
            # returns a null `redirect_to` both when it cannot find the
            # provider and when the provider has no logout endpoint, so a stub
            # without this could not tell a working logout from a broken one.
            end_session_endpoint=f"{BEARER_ISSUER}/logout",
        )

    async def jwks(self, *, force: bool = False) -> KeySet:
        return KeySet([self._key])


@pytest.fixture(scope="module")
def signing_key() -> RSAKey:
    return RSAKey.generate_key(2048, parameters={"kid": "test-key-1"})


def make_token(key: RSAKey, **overrides: object) -> str:
    """A token shaped like the ones this deployment's Keycloak issues.

    The claim set was copied from a live token on 2026-08-28 rather than written
    from the specification, which matters: `azp` names the *chat* client while
    `aud` names the gateway, and that is only true because of an audience mapper.
    """
    now = int(utcnow().timestamp())
    claims: dict[str, object] = {
        "iss": BEARER_ISSUER,
        "sub": "subject-1",
        "aud": [BEARER_AUDIENCE],
        "typ": "Bearer",
        "azp": "llm-chat",
        "exp": now + 300,
        "iat": now,
        "email": "member@example.org",
        "name": "Test Member",
        "groups": ["research"],
    }
    claims.update(overrides)
    return jwt.encode(
        {"alg": "RS256", "kid": key.kid},
        {k: v for k, v in claims.items() if not isinstance(v, _Absent)},
        key,
    )


def bearer_auth(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def seed_identity_provider(
    app: FastAPI,
    session_factory: async_sessionmaker[AsyncSession],
    signing_key: RSAKey,
    *,
    name: str = "default",
    issuer: str = BEARER_ISSUER,
) -> IdentityProvider:
    """An identity provider row the bearer tests can authenticate against.

    The client the registry would build is replaced with the stub (local
    discovery and JWKS) by being placed in the registry's cache directly,
    keyed to the origin the test transport uses. This is the new single
    configuration path: ADR 0051 moved providers into rows, so tests seed
    rows rather than stuffing ``app.state.oidc_client``.
    """
    from gateway.identity_registry import record_from_row

    box: SecretBox = app.state.secrets
    row = IdentityProvider(
        name=name,
        issuer=issuer,
        client_id="llm-gateway",
        client_secret_encrypted=box.encrypt("test-idp-secret"),
        scopes=["openid", "profile", "email"],
        is_enabled=True,
    )

    async with session_factory() as session:
        session.add(row)
        await session.commit()


    record = record_from_row(row, box)
    origin = "http://gateway"
    # The audience is what switches /v1 bearer tokens on (ADR 0040) — the
    # registry would inject it in client_for; the stub bypasses client_for, so
    # it injects it itself.
    settings: Settings = app.state.settings
    stub_settings = record.as_oidc_settings(
        f"{origin}/auth/callback/{name}", settings.oidc.access_token_audience
    )
    app.state.oidc_providers._clients[(row.id, row.updated_at, origin)] = StubOIDCClient(
        stub_settings, signing_key
    )
    return row


@pytest_asyncio.fixture
async def bearer_app(app: FastAPI, signing_key: RSAKey) -> FastAPI:
    """The real app, configured to accept tokens for BEARER_AUDIENCE."""
    app_settings: Settings = app.state.settings
    app_settings.oidc = OIDCSettings(
        enabled=True,
        issuer=BEARER_ISSUER,
        client_id="llm-gateway",
        groups_claim="groups",
        access_token_audience=BEARER_AUDIENCE,
    )
    await seed_identity_provider(app, app.state.session_factory, signing_key)
    return app
