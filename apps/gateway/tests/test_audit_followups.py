"""Audit 2026-11 follow-ups: docs routes, router fields, BYOK reply bounds, Valkey down."""

from __future__ import annotations

import logging
import types
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from typing import Any

import httpx
import pytest
from conftest import Seeded
from fastapi import FastAPI
from gateway.config import Settings, UpstreamSettings
from gateway.main import _build_counter_store, create_app
from gateway.models import (
    ApiSurface,
    LimitMetric,
    LimitScope,
    UsageRecord,
    UsageStatus,
)
from gateway.netguard import ResponseTooLarge, build_guarded_client
from gateway.plugins import registry as plugin_registry
from gateway.quota import (
    DatabaseCounterStore,
    InMemoryCounterStore,
    QuotaAmounts,
    QuotaExceeded,
    QuotaSubject,
    QuotaUnavailable,
    UnavailableCounterStore,
)
from gateway.routers._metered import Metered
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_quota_engine import add_rule, engine_for

# -- /docs and /openapi.json -------------------------------------------------


@pytest.mark.parametrize("path", ["/docs", "/openapi.json", "/redoc"])
async def test_docs_routes_are_closed_by_default(client: httpx.AsyncClient, path: str) -> None:
    assert (await client.get(path)).status_code == 404


async def test_docs_routes_open_with_the_setting(settings: Settings) -> None:
    app = create_app(settings.model_copy(update={"docs_enabled": True}))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as http:
        assert (await http.get("/docs")).status_code == 200
        assert "paths" in (await http.get("/openapi.json")).json()


def test_the_schema_is_still_built_with_the_routes_closed(settings: Settings) -> None:
    """scripts/export_openapi.py and the console's types read ``app.openapi()``."""
    assert settings.docs_enabled is False
    assert "/api/me" in create_app(settings).openapi()["paths"]


# -- router-kind providers ---------------------------------------------------


def _metered_for(plugin_name: str) -> Metered:
    plugin = plugin_registry.resolve(plugin_name)
    return Metered(
        accounting=types.SimpleNamespace(plugin=plugin),  # type: ignore[arg-type]
        reservation=None,  # type: ignore[arg-type]
        quota=None,  # type: ignore[arg-type]
    )


def _body() -> dict[str, Any]:
    return {
        "model": "m",
        "messages": [],
        "models": ["a", "b"],
        "provider": {"order": ["x"]},
        "route": "fallback",
        "temperature": 0.2,
    }


def test_a_router_drops_the_callers_routing_fields() -> None:
    shaped = _metered_for("openrouter").shape_payload(_body(), surface=ApiSurface.CHAT_COMPLETIONS)
    assert shaped == {"model": "m", "messages": [], "temperature": 0.2}


def test_a_provider_forwards_the_body_unchanged() -> None:
    shaped = _metered_for("generic").shape_payload(_body(), surface=ApiSurface.CHAT_COMPLETIONS)
    assert shaped["provider"] == {"order": ["x"]}
    assert shaped["models"] == ["a", "b"]


def test_a_router_plugin_can_declare_the_fields_it_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = plugin_registry.resolve("openrouter")
    monkeypatch.setattr(type(plugin), "caller_routing_fields", ("provider",), raising=False)
    shaped = _metered_for("openrouter").shape_payload(_body(), surface=ApiSurface.CHAT_COMPLETIONS)
    assert "provider" in shaped
    assert "models" not in shaped
    assert "route" not in shaped


# -- BYOK replies are bounded ------------------------------------------------


@pytest.fixture
async def big_server() -> AsyncIterator[int]:
    import asyncio

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await reader.readuntil(b"\r\n\r\n")
        chunked = b"/chunked" in request.split(b"\r\n")[0]
        if chunked:
            writer.write(b"HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n")
            for _ in range(50):
                writer.write(b"400\r\n" + b"x" * 1024 + b"\r\n")
            writer.write(b"0\r\n\r\n")
        else:
            writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 50000\r\n\r\n" + b"x" * 50000)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


def _client(cap: int, port: int) -> httpx.AsyncClient:
    from test_netguard import ToLocal

    async def lan_dns(host: str, port: int) -> list[str]:
        # Not 10.1.2.3: a CI runner's own interfaces may sit on that range.
        return ["10.213.87.4"]

    return build_guarded_client(
        UpstreamSettings(base_url="http://x/v1", byok_max_response_bytes=cap),
        allow_private=lambda: True,
        resolver=lan_dns,
        inner=ToLocal(),
    )


async def test_a_declared_oversize_reply_is_refused(big_server: int) -> None:
    client = _client(10_000, big_server)
    try:
        with pytest.raises(ResponseTooLarge):
            await client.get(f"http://ollama.example.com:{big_server}/models")
    finally:
        await client.aclose()


async def test_an_undeclared_oversize_reply_is_cut_off(big_server: int) -> None:
    client = _client(10_000, big_server)
    try:
        with pytest.raises(ResponseTooLarge):
            await client.get(f"http://ollama.example.com:{big_server}/chunked")
    finally:
        await client.aclose()


async def test_a_reply_under_the_cap_is_whole(big_server: int) -> None:
    client = _client(100_000, big_server)
    try:
        response = await client.get(f"http://ollama.example.com:{big_server}/models")
    finally:
        await client.aclose()
    assert len(response.content) == 50000


def test_the_guarded_client_has_a_finite_read_timeout() -> None:
    client = build_guarded_client(UpstreamSettings(base_url="http://x/v1"))
    assert client.timeout.read == 300.0
    explicit = build_guarded_client(UpstreamSettings(base_url="http://x/v1"), read_timeout=10.0)
    assert explicit.timeout.read == 10.0


# -- Valkey configured but down at startup -----------------------------------


class _DeadRedis:
    async def ping(self) -> None:
        raise ConnectionError("refused")


@pytest.fixture
def valkey_down(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    import redis.asyncio as redis_asyncio

    monkeypatch.setattr(redis_asyncio, "from_url", lambda *a, **k: _DeadRedis())
    yield


async def test_unreachable_valkey_is_not_a_process_local_store(
    app: FastAPI, settings: Settings, valkey_down: None, caplog: pytest.LogCaptureFixture
) -> None:
    configured = settings.model_copy(update={"valkey_url": "redis://nowhere:6379/0"})
    with caplog.at_level(logging.ERROR):
        store = await _build_counter_store(app, configured)
    assert isinstance(store, UnavailableCounterStore)
    assert not isinstance(store, InMemoryCounterStore)
    assert "COUNTER STORE DOWN" in caplog.text
    assert app.state.counter_store_error


async def test_no_valkey_url_stays_the_deliberate_process_local_store(
    app: FastAPI, settings: Settings
) -> None:
    store = await _build_counter_store(app, settings.model_copy(update={"valkey_url": ""}))
    assert isinstance(store, InMemoryCounterStore)
    assert app.state.counter_store_error is None


async def test_readyz_reports_the_degraded_counter_store(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    app.state.counter_store_error = "valkey unreachable at startup (ConnectionError)"
    body = (await client.get("/readyz")).json()
    assert body["status"] == "degraded"
    assert body["checks"]["valkey"].startswith("error: valkey unreachable at startup")
    assert body["checks"]["database"] == "ok"


async def test_admission_counts_the_ledger_not_this_process(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    seeded: Seeded,
) -> None:
    """The point of the change: a limit already spent in the ledger still holds."""
    group = seeded.group
    await add_rule(
        session,
        scope=LimitScope.GROUP,
        scope_id=group.id,
        metric=LimitMetric.REQUESTS,
        limit="1",
    )
    session.add(
        UsageRecord(
            request_id="r1",
            status=UsageStatus.COMPLETED,
            group_id=group.id,
            model_name="m",
            currency="EUR",
            total_tokens=10,
            cost=Decimal("0.5"),
        )
    )
    await session.commit()
    quota = engine_for(
        UnavailableCounterStore("down"), fallback=DatabaseCounterStore(session_factory)
    )
    with pytest.raises(QuotaExceeded):
        await quota.check_and_reserve(session, QuotaSubject(None, group.id), QuotaAmounts())


async def test_admission_is_refused_when_nothing_can_answer(session: AsyncSession) -> None:
    await add_rule(session, scope=LimitScope.GLOBAL, metric=LimitMetric.REQUESTS, limit="100")
    quota = engine_for(UnavailableCounterStore("down"), fallback=None)
    with pytest.raises(QuotaUnavailable):
        await quota.check_and_reserve(session, QuotaSubject(None, None), QuotaAmounts())
