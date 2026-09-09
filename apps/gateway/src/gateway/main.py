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
from gateway.fx import FXService
from gateway.identity_registry import OIDCProviderRegistry, seed_from_env
from gateway.logging_config import configure_logging
from gateway.login_throttle import LoginThrottle
from gateway.oidc_policy import OIDCPolicyResolver
from gateway.providers import ProviderRegistry
from gateway.quota import (
    DatabaseCounterStore,
    InMemoryCounterStore,
    QuotaEngine,
    ValkeyCounterStore,
)
from gateway.quota.notifications import QuotaNotifier
from gateway.redaction.base import Redactor
from gateway.redaction.resolver import RedactionResolver
from gateway.routers import (
    admin,
    auth,
    billing,
    chat,
    console,
    embeddings,
    health,
    images,
    me,
    messages,
    models,
    ocr,
    tokens,
)
from gateway.routers import responses as responses_router
from gateway.routers.auth import ResetRequestThrottle
from gateway.secrets import SecretBox
from gateway.upstream import build_http_client

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
    # Whether the caller supplied one matters below: an injected client is a
    # test's fake transport and must be used for every provider, while the
    # default is only a fallback nothing routes through any more.
    injected_upstream = upstream_http
    upstream_http = upstream_http or build_http_client(settings.upstream)
    control_http = control_http or httpx.AsyncClient(timeout=httpx.Timeout(10.0))

    store = await _build_counter_store(app, settings)

    app.state.session_factory = session_factory
    app.state.engine = engine
    app.state.upstream_http = upstream_http
    app.state.control_http = control_http
    # One client per provider, built on demand (ADR 0027). `settings.upstream`
    # still supplies transport tuning — timeouts and pool sizes are properties of
    # this gateway, not of any provider — but the endpoint and credentials now
    # come from the provider row a model points at.
    app.state.secrets = SecretBox(settings.secret_key_list())
    app.state.providers = ProviderRegistry(
        settings.upstream,
        app.state.secrets,
        # An injected client means a test's fake transport: reuse it for every
        # provider rather than building real ones the fake would never see. In
        # production this is None, so each provider gets its own pool.
        client_factory=(lambda _settings: injected_upstream) if injected_upstream else None,
    )
    # The quota notifier is built before the engine so the engine can carry it
    # (ADR 0052); its poller starts beside the others further down. The set is
    # app.state.background_tasks, shared with chat.py's detached finalisations.
    background_tasks: set[asyncio.Task[None]] = set()
    notifier = QuotaNotifier(
        session_factory,
        settings,
        app.state.secrets,
        background_tasks=background_tasks,
    )
    app.state.quota_notifier = notifier
    # FX rates (ADR 0054): one fetch per day per pair, persisted, last known
    # kept forever. Started with the other pollers further down.
    fx_service = FXService(session_factory, settings)
    app.state.fx = fx_service
    app.state.quota_engine = QuotaEngine(
        store,
        settings=settings.quota,
        fallback=DatabaseCounterStore(session_factory),
        # The same timezone reporting uses, so a monthly budget and a monthly
        # report agree about when the month started (ADR 0024).
        billing_timezone=settings.billing_timezone,
        notifier=notifier,
    )

    # `app.state.redactor` stays the one place the request path reads, so
    # `get_redactor` remains a single attribute lookup. The resolver replaces it
    # when an admin changes the engine, and polls to learn about a change another
    # worker's request made (ADR 0033).
    def _swap_redactor(replacement: Redactor) -> None:
        app.state.redactor = replacement

    resolver = RedactionResolver(settings.redaction, session_factory, on_change=_swap_redactor)
    app.state.redaction = resolver
    app.state.redactor = resolver.redactor
    # Read once before serving, so a worker never answers a request with the
    # environment's engine when the console has already overridden it. Failure is
    # non-fatal by construction: `refresh` keeps the environment's engine and
    # logs, rather than refusing to boot over a database blip.
    await resolver.refresh()
    resolver.start()
    # The identity policy (ADR 0048): console-decided, environment-baselined,
    # polled the same way redaction is. Read once before serving, so a worker
    # never answers a login with the environment's policy when the console has
    # already overridden it. Failure is non-fatal by construction: under the
    # test fixtures the schema does not exist yet at this point, and in
    # production a database blip must not refuse to boot — the poll catches up,
    # and until then the environment's policy stands.
    oidc_policy_resolver = OIDCPolicyResolver(settings.oidc, session_factory)
    app.state.oidc_policy = oidc_policy_resolver
    try:
        await oidc_policy_resolver.refresh_once()
    except Exception:
        logger.warning("could not read the oidc policy at startup", exc_info=True)
    oidc_policy_resolver.start()
    # The reset-email cooldown, armed only when the feature is: an absent
    # throttle means POST /auth/password-reset answers 503, the same switch
    # the login throttle is.
    app.state.reset_throttle = (
        ResetRequestThrottle(settings.local_auth.password_reset.request_cooldown_seconds)
        if settings.local_auth.password_reset.enabled
        else None
    )
    app.state.token_estimator = DEFAULT_ESTIMATOR
    # Strong references to detached finalisation tasks (chat.py) and quota
    # notification sends; the notifier shares this set.
    app.state.background_tasks = background_tasks
    # Non-fatal by construction, like every startup read here: under the test
    # fixtures the schema does not exist yet, and a database blip must not
    # refuse to boot. The poll catches up; until then there are no
    # subscriptions and observe() is a no-op.
    try:
        await notifier.refresh_once()
    except Exception:
        logger.warning("could not read quota notification settings at startup", exc_info=True)
    notifier.start()
    # The day's rate is fetched before serving, so the first USD-priced
    # request of the day does not wait on the rates API. Failure is the same
    # non-fatal story: the last known rate answers, or admission refuses
    # loudly rather than billing at a guess.
    try:
        await fx_service.refresh_once()
    except Exception:
        logger.warning("could not fetch fx rates at startup", exc_info=True)
    fx_service.start()
    # Identity providers (ADR 0051): rows seeded from the environment when the
    # table is empty, and cached per-provider clients built on demand. The old
    # single-client state is gone — the registry is the only way in.
    registry = OIDCProviderRegistry(control_http, app.state.secrets, settings)
    app.state.oidc_providers = registry
    # The seed is best-effort for the same reason every startup read here is:
    # under the test fixtures the schema does not exist yet, and the fallback
    # (list_providers over an empty table) answers meanwhile.
    try:
        async with session_factory() as session:
            await seed_from_env(session, settings, app.state.secrets)
    except Exception:
        logger.warning("could not seed identity providers at startup", exc_info=True)
    # The local-login throttle. Its presence *is* the feature switch: an
    # absent throttle means POST /auth/login answers 503, and /auth/methods
    # reports no local way in.
    app.state.login_throttle = (
        LoginThrottle(
            max_failed_attempts=settings.local_auth.max_failed_attempts,
            window_seconds=settings.local_auth.throttle_window_seconds,
        )
        if settings.local_auth.enabled
        else None
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
            "secret_key_configured": app.state.secrets.enabled,
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
    if (providers := getattr(app.state, "providers", None)) is not None:
        await providers.aclose()
    # Stops the poller and releases the engine's own resources — a connection
    # pool, for the HTTP one. The optional-aclose dance moved into the resolver,
    # which is also the only thing that knows which engine is currently in force
    # after a console change.
    if (resolver := getattr(app.state, "redaction", None)) is not None:
        await resolver.aclose()
    if (policy := getattr(app.state, "oidc_policy", None)) is not None:
        await policy.stop()
    if (notifier := getattr(app.state, "quota_notifier", None)) is not None:
        await notifier.stop()
    if (fx := getattr(app.state, "fx", None)) is not None:
        await fx.stop()
        await fx.close()
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
        title="Pystino model gateway",
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
    app.include_router(embeddings.router)
    app.include_router(responses_router.router)
    app.include_router(messages.router)
    app.include_router(images.router)
    app.include_router(ocr.router)
    app.include_router(models.router)
    app.include_router(billing.router)
    app.include_router(auth.router)
    app.include_router(tokens.router)
    app.include_router(me.router)
    app.include_router(admin.router)

    # Last, so a console route can never shadow an API one. Mounts only if the
    # assets are in the image and the setting allows it (ADR 0023).
    app.state.console_mounted = console.mount_console(app, resolved)

    return app


app = create_app()
