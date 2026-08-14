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

router = APIRouter(tags=["ops"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


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
    if valkey is None:
        # Not configured; quotas fall back to the database, which is degraded but
        # serviceable, so this does not fail readiness.
        checks["valkey"] = "not configured"
    else:
        try:
            await valkey.ping()
            checks["valkey"] = "ok"
        except Exception as exc:
            checks["valkey"] = f"error: {type(exc).__name__}"

    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if ready else "degraded", "checks": checks}
