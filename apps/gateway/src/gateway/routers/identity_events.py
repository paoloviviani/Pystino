"""`GET /admin/identity-events`: the trail `record_event` writes to (ADR 0093).

Read-only, deliberately: there is no `POST` here. Every write happens where
the action itself happens, through `gateway.identity_events.record_event`;
this route only ever selects, which is also all the database's append-only
triggers would allow it to do.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select

from gateway.deps import AdminUserDep, SessionDep
from gateway.models import IdentityEvent, IdentityEventAction, IdentityEventActor
from gateway.pagination import Page, PageDep, count_of

router = APIRouter(prefix="/api/admin", tags=["identity-events"])


class IdentityEventResponse(BaseModel):
    id: uuid.UUID
    at: datetime
    actor_type: IdentityEventActor
    actor_user_id: uuid.UUID | None
    actor_label: str
    action: IdentityEventAction
    target_user_id: uuid.UUID | None
    target_label: str
    issuer: str | None
    subject: str | None
    detail: dict[str, Any]
    reason: str | None


def _response(row: IdentityEvent) -> IdentityEventResponse:
    return IdentityEventResponse(
        id=row.id,
        at=row.at,
        actor_type=row.actor_type,
        actor_user_id=row.actor_user_id,
        actor_label=row.actor_label,
        action=row.action,
        target_user_id=row.target_user_id,
        target_label=row.target_label,
        issuer=row.issuer,
        subject=row.subject,
        detail=row.detail,
        reason=row.reason,
    )


@router.get("/identity-events", response_model=Page[IdentityEventResponse])
async def list_identity_events(
    admin: AdminUserDep,
    session: SessionDep,
    page: PageDep,
    user_id: uuid.UUID | None = None,
    action: IdentityEventAction | None = None,
    before: datetime | None = None,
) -> Page[IdentityEventResponse]:
    """The audit trail, newest first.

    `user_id` matches either side: what an administrator asks about one
    person is "what happened to them, and what did they do", not one or the
    other, so it is a single filter over both `actor_user_id` and
    `target_user_id`.
    """
    stmt = select(IdentityEvent)
    if user_id is not None:
        stmt = stmt.where(
            (IdentityEvent.actor_user_id == user_id) | (IdentityEvent.target_user_id == user_id)
        )
    if action is not None:
        stmt = stmt.where(IdentityEvent.action == action)
    if before is not None:
        stmt = stmt.where(IdentityEvent.at < before)
    stmt = stmt.order_by(IdentityEvent.at.desc())

    total = await count_of(session, stmt)
    rows = (await session.execute(page.apply(stmt))).scalars().all()
    return page.page([_response(row) for row in rows], total)
