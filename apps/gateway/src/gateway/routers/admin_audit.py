"""`GET /api/admin/audit`: the administrators' change log (issue P9).

Read-only. Rows are written where the change happens, through
`gateway.admin_audit.record_audit`, in the same transaction.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import ColumnElement, select

from gateway.deps import AdminUserDep, SessionDep
from gateway.models import AdminAuditEvent
from gateway.pagination import Page, PageDep, count_of

router = APIRouter(prefix="/api/admin", tags=["admin-audit"])


class AdminAuditEventResponse(BaseModel):
    id: uuid.UUID
    created_at: datetime
    actor_user_id: uuid.UUID | None
    actor_label: str
    action: str
    target_type: str
    target_id: str | None
    target_label: str
    changes: dict[str, Any]


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/audit", response_model=Page[AdminAuditEventResponse])
async def list_admin_audit(
    admin: AdminUserDep,
    session: SessionDep,
    page: PageDep,
    start: datetime | None = None,
    end: datetime | None = None,
    actor: str | None = None,
    action: str | None = None,
    target_type: str | None = None,
) -> Page[AdminAuditEventResponse]:
    """Newest first. `start` is inclusive, `end` exclusive.

    `actor` is an administrator's user id, or part of the email they had at the
    time. `action` is a prefix, so `grant.` lists every grant change.
    """
    stmt = select(AdminAuditEvent)
    if start is not None:
        stmt = stmt.where(AdminAuditEvent.created_at >= start)
    if end is not None:
        stmt = stmt.where(AdminAuditEvent.created_at < end)
    if actor:
        clause: ColumnElement[bool] = AdminAuditEvent.actor_label.ilike(
            f"%{_like_escape(actor)}%", escape="\\"
        )
        with contextlib.suppress(ValueError):
            clause = clause | (AdminAuditEvent.actor_user_id == uuid.UUID(actor))
        stmt = stmt.where(clause)
    if action:
        stmt = stmt.where(AdminAuditEvent.action.startswith(action, autoescape=True))
    if target_type:
        stmt = stmt.where(AdminAuditEvent.target_type == target_type)
    stmt = stmt.order_by(AdminAuditEvent.created_at.desc(), AdminAuditEvent.id)

    total = await count_of(session, stmt)
    rows = (await session.execute(page.apply(stmt))).scalars().all()
    return page.page(
        [
            AdminAuditEventResponse(
                id=row.id,
                created_at=row.created_at,
                actor_user_id=row.actor_user_id,
                actor_label=row.actor_label,
                action=row.action,
                target_type=row.target_type,
                target_id=row.target_id,
                target_label=row.target_label,
                changes=row.changes,
            )
            for row in rows
        ],
        total,
    )
