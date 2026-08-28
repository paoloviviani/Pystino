"""The application, and how it is wired.

Deliberately small. This service owns conversations and the loop that produces
them; everything else it needs — who someone is, what they may use, what it
costs — belongs to Keycloak and the gateway and is asked for rather than
reimplemented.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from chat_api.config import Settings
from chat_api.db import make_engine, make_session_factory
from chat_api.gateway_client import GatewayClient
from chat_api.oidc import OIDCClient
from chat_api.routers import auth, conversations, health, spa
from chat_api.secrets import SecretBox

logger = logging.getLogger(__name__)


def create_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Tests call init_app_state themselves, with their own HTTP clients, and
        # must not have it done twice. The marker is the wiring, not a flag.
        if not hasattr(app.state, "session_factory"):
            await init_app_state(app, settings)
        yield
        await shutdown_app_state(app)

    app = FastAPI(
        title="LLM Platform Chat",
        version="0.1.0",
        lifespan=lifespan,
        # No interactive docs in a browser-facing app: the CSP forbids the CDN
        # they load from anyway, so they would render as a blank page and read
        # as a bug.
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = settings

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error on %s", request.url.path)
        return JSONResponse(
            status_code=500,
            content={"error": {"message": "Something went wrong.", "type": "server_error"}},
        )

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(conversations.router)
    return app


async def init_app_state(
    app: FastAPI,
    settings: Settings,
    *,
    gateway_http: httpx.AsyncClient | None = None,
    control_http: httpx.AsyncClient | None = None,
) -> None:
    """Everything with a connection in it, injectable so tests can substitute."""
    engine = make_engine(settings.database_url)
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)

    app.state.secret_box = SecretBox(
        [key.strip() for key in settings.secret_key.get_secret_value().split(",")]
    )
    app.state.control_http = control_http or httpx.AsyncClient(timeout=10.0)
    app.state.oidc_client = OIDCClient(settings.oidc, app.state.control_http)

    app.state.gateway_http = gateway_http or httpx.AsyncClient(
        timeout=httpx.Timeout(settings.gateway.timeout_seconds, connect=10.0)
    )
    app.state.gateway_client = GatewayClient(app.state.gateway_http, settings.gateway.base_url)

    # Last, because it registers a catch-all route: anything mounted after it
    # would be shadowed by the SPA fallback.
    directory = spa.resolve_spa_dir(settings)
    if directory is not None:
        spa.mount(app, directory)
        app.include_router(spa.build_router(directory))
    else:
        logger.info(
            "no chat assets in this image; serving the API only. "
            "Build with --build-arg INCLUDE_WEB=true to include them."
        )


async def shutdown_app_state(app: FastAPI) -> None:
    for attribute in ("gateway_http", "control_http"):
        client = getattr(app.state, attribute, None)
        if client is not None:
            await client.aclose()
    engine = getattr(app.state, "engine", None)
    if engine is not None:
        await engine.dispose()
