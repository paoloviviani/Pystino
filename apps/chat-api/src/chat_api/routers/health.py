"""Liveness and readiness."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from chat_api.deps import get_db

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(
    response: Response, db: Annotated[AsyncSession, Depends(get_db)]
) -> dict[str, str]:
    """Ready means the database answers.

    The gateway is deliberately *not* checked. It being down is a reason for
    requests to fail, not a reason for this container to be replaced — and a
    readiness probe that fails on a dependency's outage turns one outage into
    a restart loop.
    """
    try:
        await db.execute(text("SELECT 1"))
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "database unavailable"}
    return {"status": "ready"}
