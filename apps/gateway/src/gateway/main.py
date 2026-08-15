"""Application factory and process lifecycle."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from gateway.accounting import DEFAULT_ESTIMATOR
from gateway.config import Settings, get_settings
from gateway.db import create_engine, create_session_factory
from gateway.errors import GatewayError, error_payload, gateway_error_handler
from gateway.logging_config import configure_logging
from gateway.oidc import OIDCClient
from gateway.quota import (
    DatabaseCounterStore,
    InMemoryCounterStore,
    QuotaEngine,
    ValkeyCounterStore,
)
from gateway.redaction import build_redactor
from gateway.routers import admin, auth, chat, health, me, models
from gateway.upstream import OpenAICompatibleUpstream, build_http_client

logger = logging.getLogger(__name__)


async def _build_counter_store(app: FastAPI, settings: Settings) -> Any:
    """Connect to Valkey, or fall back to a process-local store.

    An unreachable Valkey at startup is not fatal: the quota engine's database
    fallback keeps limits correct, so the gateway comes up degraded rather than
    not at all.
    """
    if not settings.valkey_url:
        logger.warning(
            "no valkey_url configured; using a process-local counter store. "
            "This is only correct with a single worker process."
        )
        return InMemoryCounterStore()

    try:
        import redis.asyncio as redis_asyncio

        client = redis_asyncio.from_url(settings.valkey_url, decode_responses=True)
        await client.ping()
    except Exception:
        logger.warning(
            "could not reach the counter store at startup; quotas will use the "
            "database fallback until it recovers",
            exc_info=True,
        )
        return InMemoryCounterStore()

    app.state.valkey = client
    return ValkeyCounterStore(client)


async def init_app_state(
    app: FastAPI,
    settings: Settings,
    *,
    upstream_http: httpx.AsyncClient | None = None,
    control_http: httpx.AsyncClient | None = None,
) -> None:
    """Wire up every shared service on ``app.state``.

    Factored out of the lifespan so that tests exercise this exact wiring with a
    fake transport injected, rather than a hand-built approximation of it that can
    drift from what production does.
    """
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)

    # Two HTTP clients on purpose. The upstream client has *no* read timeout so
    # long streams survive; a control-plane call (OIDC discovery, JWKS) must never
    # inherit that, or a hung identity provider would hang a login forever.
    upstream_http = upstream_http or build_http_client(settings.upstream)
    control_http = control_http or httpx.AsyncClient(timeout=httpx.Timeout(10.0))

    store = await _build_counter_store(app, settings)

    app.state.session_factory = session_factory
    app.state.engine = engine
    app.state.upstream_http = upstream_http
    app.state.control_http = control_http
    app.state.upstream = OpenAICompatibleUpstream(settings.upstream, upstream_http)
    app.state.quota_engine = QuotaEngine(
        store,
        settings=settings.quota,
        fallback=DatabaseCounterStore(session_factory),
        # The same timezone reporting uses, so a monthly budget and a monthly
        # report agree about when the month started (ADR 0024).
        billing_timezone=settings.billing_timezone,
    )
    app.state.redactor = build_redactor(settings.redaction)
    app.state.token_estimator = DEFAULT_ESTIMATOR
    # Strong references to detached finalisation tasks; see chat.py.
    app.state.background_tasks = set()
    app.state.oidc_client = (
        OIDCClient(settings.oidc, control_http) if settings.oidc.enabled else None
    )

    # An empty counter cache is not a failed read — it answers confidently with
    # zero, which would hand every group a fresh budget after Valkey is wiped.
    # One worker rebuilds from the ledger; the rest see the marker and skip.
    try:
        async with session_factory() as session:
            await app.state.quota_engine.rebuild_if_cache_is_cold(session)
    except Exception:
        # Never block startup on this: a gateway that will not boot is worse than
        # one whose counters are briefly optimistic.
        logger.warning("could not check the counter cache at startup", exc_info=True)

    logger.info(
        "gateway started",
        extra={
            "environment": settings.environment,
            "upstream": settings.upstream.base_url,
            "redaction_engine": settings.redaction.engine,
            "oidc_enabled": settings.oidc.enabled,
            "quota_enabled": settings.quota.enabled,
        },
    )


async def shutdown_app_state(app: FastAPI) -> None:
    """Release everything :func:`init_app_state` acquired."""
    # Give detached accounting writes a moment to land before tearing the engine
    # down, or a client that disconnected during shutdown loses its usage row.
    pending: set[asyncio.Task[None]] = getattr(app.state, "background_tasks", set())
    if pending:
        logger.info("waiting for %d accounting task(s)", len(pending))
        await asyncio.wait(pending, timeout=5.0)

    await app.state.upstream_http.aclose()
    await app.state.control_http.aclose()
    # A redaction engine may own a connection pool. Optional rather than part of
    # the Redactor protocol: most engines have nothing to release, and requiring
    # an empty aclose() from every plugin author is friction for no benefit.
    if (closer := getattr(app.state.redactor, "aclose", None)) is not None:
        await closer()
    if (valkey := getattr(app.state, "valkey", None)) is not None:
        await valkey.aclose()
    await app.state.engine.dispose()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await init_app_state(app, app.state.settings)
    try:
        yield
    finally:
        await shutdown_app_state(app)


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return validation failures in OpenAI's error envelope.

    FastAPI's default 422 body is not the shape an OpenAI client expects, so it
    surfaces as an opaque transport error instead of a readable message.
    """
    assert isinstance(exc, RequestValidationError)
    first = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
    message = first.get("msg", "Invalid request.")
    return JSONResponse(
        status_code=400,
        content=error_payload(
            f"{location}: {message}" if location else str(message),
            code="invalid_request",
            param=location or None,
        ),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()
    configure_logging(level=resolved.log_level, json_output=resolved.log_json)

    app = FastAPI(
        title="LLM Platform Gateway",
        version="0.1.0",
        summary="OpenAI-compatible gateway with per-group accounting, quotas and policy",
        lifespan=lifespan,
        # The management API is documented; the /v1 surface is OpenAI's.
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.state.settings = resolved

    app.add_exception_handler(GatewayError, gateway_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)

    app.include_router(health.router)
    app.include_router(chat.router)
    app.include_router(models.router)
    app.include_router(auth.router)
    app.include_router(me.router)
    app.include_router(admin.router)

    return app


app = create_app()
