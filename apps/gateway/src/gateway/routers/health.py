"""Liveness and readiness.

Split deliberately. ``/healthz`` answers "is this process alive" and must not
touch a dependency, or a database blip would make an orchestrator kill every
otherwise-healthy worker. ``/readyz`` answers "should traffic be sent here" and
does check dependencies.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import text

from gateway.privacy.state import state_of

router = APIRouter(tags=["ops"])


@router.get("/healthz")
async def healthz(request: Request) -> dict[str, str]:
    # Alive means alive: a worker that has not read its privacy rules is reported
    # (and refuses the requests they govern) but is not killed for it.
    privacy = state_of(request)
    if privacy.loaded:
        return {"status": "ok"}
    return {"status": "ok", "privacy_routing": "rules not loaded yet; some requests are refused"}


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, Any]:
    checks: dict[str, str] = {}
    ready = True

    try:
        factory = request.app.state.session_factory
        async with factory() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"
        ready = False

    valkey = getattr(request.app.state, "valkey", None)
    startup_error = getattr(request.app.state, "counter_store_error", None)
    degraded = False
    if startup_error:
        # Valkey is configured and was down at startup: the quota engine is on
        # the database fallback, and only a restart brings it back.
        checks["valkey"] = f"error: {startup_error}; restart the gateway once it is reachable"
        degraded = True
    elif valkey is None:
        # Not configured; quotas fall back to the database, which is degraded but
        # serviceable, so this does not fail readiness.
        checks["valkey"] = "not configured"
    else:
        try:
            await valkey.ping()
            checks["valkey"] = "ok"
        except Exception as exc:
            checks["valkey"] = f"error: {type(exc).__name__}"
            degraded = True

    privacy = state_of(request)
    if privacy.loaded:
        checks["privacy_routing"] = (
            f"ok, {len(privacy.bad_rules)} rule(s) skipped as unreadable"
            if privacy.bad_rules
            else "ok"
        )
        degraded = degraded or bool(privacy.bad_rules)
    else:
        # Requests for kinds that rules govern are refused until the first read.
        checks["privacy_routing"] = f"error: rules never loaded ({privacy.load_error or 'pending'})"
        degraded = True

    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if ready and not degraded else "degraded", "checks": checks}
