"""The redaction log: what was hidden or refused in each request (ADR 0112).

``/api/me/redaction/events`` is for the console (a cookie session): the
caller's own requests where redaction hid something or a pipeline refused, with
**no values in it** (``redaction.report``), so there is nothing here that a
serializer could leak. ``/api/admin/redaction/events`` is the administrators'
read of everyone's, never including a user's own-key (BYOK) requests.

A user's own rules, pipelines and test box live in ``me_privacy`` (ADR 0113).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter
from sqlalchemy import func, select

from gateway.deps import AdminUserDep, ManagementUserDep, SessionDep
from gateway.models import ModelDef, Provider, UsageRecord, User
from gateway.pagination import Page, PageDep, count_of
from gateway.schemas import (
    RedactionEventEntity,
    RedactionEventRow,
    RoutingInfo,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/me/redaction", tags=["self-service"])
admin_router = APIRouter(prefix="/api/admin/redaction", tags=["administration"])


def _event_row(r: Any, *, with_user: bool) -> RedactionEventRow:
    detail = r.redaction_detail or {}
    return RedactionEventRow(
        request_id=r.request_id,
        created_at=r.created_at,
        user_id=r.user_id if with_user else None,
        user_label=r.user_label if with_user else None,
        model=r.model_name,
        provider=r.provider_name,
        byok=bool(r.byok),
        blocked=bool(detail.get("blocked")),
        entities=[RedactionEventEntity.model_validate(e) for e in detail.get("entities", [])],
        routing=(RoutingInfo.model_validate(detail["routing"]) if detail.get("routing") else None),
    )


def _events_query(
    *, user_id: uuid.UUID | None, request_id: str | None, own_keys: bool | None
) -> Any:
    stmt = (
        select(
            UsageRecord.request_id,
            UsageRecord.created_at,
            UsageRecord.user_id,
            func.coalesce(User.email, User.subject).label("user_label"),
            UsageRecord.model_name,
            Provider.name.label("provider_name"),
            UsageRecord.byok,
            UsageRecord.redaction_detail,
        )
        .select_from(UsageRecord)
        .outerjoin(User, User.id == UsageRecord.user_id)
        .outerjoin(ModelDef, ModelDef.id == UsageRecord.model_id)
        .outerjoin(Provider, Provider.id == ModelDef.provider_id)
        .where(UsageRecord.redaction_detail.is_not(None))
    )
    if user_id is not None:
        stmt = stmt.where(UsageRecord.user_id == user_id)
    if request_id:
        stmt = stmt.where(UsageRecord.request_id == request_id)
    if own_keys is not None:
        stmt = stmt.where(UsageRecord.byok.is_(own_keys))
    return stmt.order_by(UsageRecord.created_at.desc(), UsageRecord.id.desc())


@router.get("/events", response_model=Page[RedactionEventRow])
async def my_redaction_events(
    user: ManagementUserDep,
    session: SessionDep,
    page: PageDep,
    request_id: str | None = None,
) -> Page[RedactionEventRow]:
    """Requests of the caller's where redaction hid something or refused, newest first.

    The caller's own requests only, **including the ones on their own provider
    keys** (BYOK; flagged ``byok``): those requests are theirs, and nobody else
    reads them. No parameter names another user. The entries carry no values.
    """
    stmt = _events_query(user_id=user.id, request_id=request_id, own_keys=None)
    total = await count_of(session, stmt)
    rows = (await session.execute(page.apply(stmt))).all()
    return page.page([_event_row(r, with_user=False) for r in rows], total)


@admin_router.get("/events", response_model=Page[RedactionEventRow])
async def all_redaction_events(
    admin: AdminUserDep,
    session: SessionDep,
    page: PageDep,
    request_id: str | None = None,
    user_id: uuid.UUID | None = None,
) -> Page[RedactionEventRow]:
    """Everyone's redaction log, newest first, for an administrator.

    **Never a user's own-key (BYOK) requests**, as every administrator read of
    the ledger: those belong to their owner. No values, as in the user's view.
    """
    stmt = _events_query(user_id=user_id, request_id=request_id, own_keys=False)
    total = await count_of(session, stmt)
    rows = (await session.execute(page.apply(stmt))).all()
    return page.page([_event_row(r, with_user=True) for r in rows], total)
