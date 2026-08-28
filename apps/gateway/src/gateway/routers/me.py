"""Self-service API: a user's own identity, billing group, API keys and spend.

Everything here acts on the caller's own account and needs no special role.
Administration of models, prices, group access and limits lives in
``routers/admin.py``.

Group *membership* is deliberately not editable anywhere: the identity provider
owns it (see :func:`gateway.oidc.provision_user`), and offering an edit that the
next login silently reverts would be worse than offering none.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import and_, case, false, func, or_, select
from sqlalchemy.orm import selectinload

from gateway.deps import ManagementUserDep, QuotaDep, SessionDep, SettingsDep
from gateway.errors import BadRequestError, PermissionError_
from gateway.models import (
    ApiKey,
    Group,
    LimitRule,
    LimitScope,
    UsageRecord,
    UsageSource,
    UsageStatus,
)
from gateway.pagination import Page, PageDep, count_of
from gateway.reporting import (
    GroupBy,
    ReportFilter,
    build_report,
    report_to_csv,
    resolve_period,
)
from gateway.schemas import (
    ApiKeyCreatedResponse,
    ApiKeyCreateRequest,
    ApiKeyResponse,
    GroupSummary,
    MeResponse,
    MyLimitResponse,
    SetDefaultBillingGroupRequest,
    UsageReport,
    UsageSummaryResponse,
)
from gateway.security import generate_api_key
from gateway.types import utcnow

router = APIRouter(prefix="/api", tags=["self-service"])

# Deliberately narrower than the admin surface: see my_usage_report.
MyGroupBy = Literal["model", "day", "group", "api_key", "total"]


def _group_summary(group: Group | None) -> GroupSummary | None:
    if group is None:
        return None
    return GroupSummary(id=group.id, name=group.name, description=group.description)


def _key_response(key: ApiKey) -> ApiKeyResponse:
    return ApiKeyResponse(
        id=key.id,
        name=key.name,
        prefix=key.prefix,
        billing_group=_group_summary(key.billing_group),
        created_at=key.created_at,
        expires_at=key.expires_at,
        revoked_at=key.revoked_at,
        last_used_at=key.last_used_at,
    )


@router.get("/me", response_model=MeResponse)
async def me(user: ManagementUserDep, session: SessionDep) -> MeResponse:
    groups = [membership.group for membership in user.memberships]
    default = next((group for group in groups if group.id == user.default_billing_group_id), None)
    return MeResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_admin=user.is_admin,
        groups=[summary for group in groups if (summary := _group_summary(group))],
        default_billing_group=_group_summary(default),
    )


@router.put("/me/default-billing-group", response_model=MeResponse)
async def set_default_billing_group(
    payload: SetDefaultBillingGroupRequest,
    user: ManagementUserDep,
    session: SessionDep,
) -> MeResponse:
    """Users pick their own default billing group, from their own groups only."""
    if payload.group_id not in user.group_ids():
        raise PermissionError_("You are not a member of that group.")

    user.default_billing_group_id = payload.group_id
    await session.commit()
    await session.refresh(user)
    return await me(user, session)


@router.get("/me/keys", response_model=Page[ApiKeyResponse])
async def list_keys(
    user: ManagementUserDep, session: SessionDep, page: PageDep
) -> Page[ApiKeyResponse]:
    """The caller's keys, newest first, revoked ones included.

    Paginated like the admin listings even though one person's keys are few:
    revoked keys are never deleted, so this grows with time, and having one
    response shape for every listing is worth more than the exception.
    """
    stmt = (
        select(ApiKey)
        .where(ApiKey.user_id == user.id)
        .options(selectinload(ApiKey.billing_group))
        .order_by(ApiKey.created_at.desc())
    )
    total = await count_of(session, stmt)
    keys = (await session.execute(page.apply(stmt))).scalars().all()
    return page.page([_key_response(key) for key in keys], total)


@router.post("/me/keys", response_model=ApiKeyCreatedResponse, status_code=status.HTTP_201_CREATED)
async def create_key(
    payload: ApiKeyCreateRequest,
    user: ManagementUserDep,
    session: SessionDep,
    settings: SettingsDep,
) -> ApiKeyCreatedResponse:
    """Mint a key. The secret is in this response and nowhere else, ever."""
    if payload.billing_group_id is not None and payload.billing_group_id not in user.group_ids():
        raise PermissionError_("You are not a member of that group.")

    if payload.billing_group_id is None and user.default_billing_group_id is None:
        raise BadRequestError(
            "Set a default billing group, or name one for this key, before "
            "creating it — otherwise requests using it cannot be billed."
        )

    generated = generate_api_key(environment_prefix=settings.api_key_prefix)
    key = ApiKey(
        user_id=user.id,
        prefix=generated.prefix,
        key_hash=generated.key_hash,
        name=payload.name,
        billing_group_id=payload.billing_group_id,
        expires_at=(
            utcnow() + timedelta(days=payload.expires_in_days) if payload.expires_in_days else None
        ),
    )
    session.add(key)
    await session.commit()
    await session.refresh(key, attribute_names=["billing_group"])

    return ApiKeyCreatedResponse(
        **_key_response(key).model_dump(),
        secret=generated.secret,
    )


@router.delete("/me/keys/{key_id}", response_model=ApiKeyResponse)
async def revoke_key(
    key_id: uuid.UUID, user: ManagementUserDep, session: SessionDep
) -> ApiKeyResponse:
    """Revoke, never delete.

    The usage ledger references keys, and a deleted key would turn historical
    spend into an unattributable row.
    """
    stmt = (
        select(ApiKey)
        .where(ApiKey.id == key_id, ApiKey.user_id == user.id)
        .options(selectinload(ApiKey.billing_group))
    )
    key = (await session.execute(stmt)).scalar_one_or_none()
    if key is None:
        raise PermissionError_("No such key.")

    if key.revoked_at is None:
        key.revoked_at = utcnow()
        await session.commit()
        await session.refresh(key)
    return _key_response(key)


@router.get("/me/usage", response_model=UsageSummaryResponse)
async def my_usage(
    user: ManagementUserDep,
    session: SessionDep,
    settings: SettingsDep,
    window_seconds: int = 86_400,
) -> UsageSummaryResponse:
    if not 60 <= window_seconds <= 366 * 86_400:
        raise BadRequestError("window_seconds must be between 60 and one year.")

    since = utcnow() - timedelta(seconds=window_seconds)
    stmt = select(
        func.count(UsageRecord.id),
        func.coalesce(func.sum(UsageRecord.total_tokens), 0),
        func.coalesce(func.sum(UsageRecord.cost), 0),
        # Surfaced so a reader can see how much of this figure is inferred rather
        # than measured.
        func.coalesce(
            func.sum(case((UsageRecord.usage_source == UsageSource.ESTIMATED, 1), else_=0)),
            0,
        ),
    ).where(
        UsageRecord.user_id == user.id,
        UsageRecord.created_at >= since,
        UsageRecord.status != UsageStatus.IN_PROGRESS,
    )
    requests, tokens, cost, estimated = (await session.execute(stmt)).one()

    return UsageSummaryResponse(
        window_seconds=window_seconds,
        requests=int(requests or 0),
        total_tokens=int(tokens or 0),
        cost=Decimal(str(cost or 0)),
        currency=settings.billing_currency,
        estimated_requests=int(estimated or 0),
    )


@router.get("/me/reports/usage", response_model=UsageReport)
async def my_usage_report(
    user: ManagementUserDep,
    session: SessionDep,
    settings: SettingsDep,
    period: str = "",
    start: datetime | None = None,
    end: datetime | None = None,
    group_by: MyGroupBy = "model",
) -> UsageReport:
    """The caller's own spend over a calendar period.

    The same report the administrator surface produces, pinned to this user — one
    implementation, so the number a user sees for themselves is the number their
    group lead sees for them. Everyone can see their own spend; nobody sees
    another's from here.

    ``group_by=user`` is not offered: with the filter pinned it could only ever
    return a single row, which invites the misreading that it shows other people.
    """
    return await build_report(
        session,
        ReportFilter(
            period=resolve_period(period, start, end, settings.billing_timezone),
            group_by=GroupBy(group_by),
            user_id=user.id,
        ),
        currency=settings.billing_currency,
        timezone=settings.billing_timezone,
    )


@router.get("/me/reports/usage.csv", response_class=PlainTextResponse)
async def my_usage_report_csv(
    user: ManagementUserDep,
    session: SessionDep,
    settings: SettingsDep,
    period: str = "",
    start: datetime | None = None,
    end: datetime | None = None,
    group_by: MyGroupBy = "model",
) -> Response:
    report = await my_usage_report(user, session, settings, period, start, end, group_by)
    filename = f"my-usage-{report.period.label}-by-{report.group_by}.csv"
    return Response(
        content=report_to_csv(report),
        media_type="text/csv; charset=utf-8",
        headers={"content-disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/me/usage/groups", response_model=dict[str, UsageSummaryResponse])
async def my_group_usage(
    user: ManagementUserDep,
    session: SessionDep,
    settings: SettingsDep,
    window_seconds: int = 86_400,
) -> dict[str, UsageSummaryResponse]:
    """Spend per group the user belongs to, so they can see what they are charging."""
    group_ids = user.group_ids()
    if not group_ids:
        return {}

    since = utcnow() - timedelta(seconds=window_seconds)
    stmt = (
        select(
            UsageRecord.group_id,
            func.count(UsageRecord.id),
            func.coalesce(func.sum(UsageRecord.total_tokens), 0),
            func.coalesce(func.sum(UsageRecord.cost), 0),
        )
        .where(
            UsageRecord.group_id.in_(group_ids),
            UsageRecord.created_at >= since,
            UsageRecord.status != UsageStatus.IN_PROGRESS,
        )
        .group_by(UsageRecord.group_id)
    )
    rows = (await session.execute(stmt)).all()
    names = {
        group.id: group.name
        for group in (await session.execute(select(Group).where(Group.id.in_(group_ids))))
        .scalars()
        .all()
    }

    return {
        names.get(group_id, str(group_id)): UsageSummaryResponse(
            window_seconds=window_seconds,
            requests=int(requests or 0),
            total_tokens=int(tokens or 0),
            cost=Decimal(str(cost or 0)),
            currency=settings.billing_currency,
        )
        for group_id, requests, tokens, cost in rows
    }


# -- the quota rules that apply to you ---------------------------------------


@router.get("/me/limits", response_model=Page[MyLimitResponse])
async def my_limits(
    user: ManagementUserDep, session: SessionDep, quota: QuotaDep, page: PageDep
) -> Page[MyLimitResponse]:
    """The rules that constrain this caller, and how much of each is spent.

    Filtered here rather than in the browser, and that is the whole point of the
    route existing: the admin listing returns every rule in the deployment, so
    "fetch them all and hide the rest" would hand a non-admin the shape of every
    budget in the organisation.

    A rule applies if it is global, scoped to this user, or scoped to a group
    they are in. **API-key rules are excluded**: which key a request used is not
    known until the request is made, so a key rule is not a fact about the
    person and showing it beside their own consumption would suggest a ceiling
    they are not necessarily near.

    Quotas are *all rules must pass* (ADR 0009), so the list is a set of
    ceilings and the binding one is whichever is nearest. That is left to the
    reader rather than computed here: "nearest" across different metrics —
    euros, tokens, requests — is not a comparison this route can make honestly.
    """
    group_ids = user.group_ids()
    stmt = (
        select(LimitRule)
        # `resets` is not decoration: `current_values` reads it to discount
        # consumption from before a reset, and touching it unloaded raises
        # MissingGreenlet under asyncio rather than quietly emitting a query.
        .options(selectinload(LimitRule.resets))
        .where(
            LimitRule.is_active.is_(True),
            or_(
                LimitRule.scope == LimitScope.GLOBAL,
                and_(LimitRule.scope == LimitScope.USER, LimitRule.scope_id == user.id),
                and_(
                    LimitRule.scope == LimitScope.GROUP,
                    LimitRule.scope_id.in_(group_ids) if group_ids else false(),
                ),
            ),
        )
        .order_by(LimitRule.scope, LimitRule.metric)
    )
    rules = list((await session.execute(stmt)).scalars().all())

    # Absent, not zero, when the counter store cannot be reached — the same
    # distinction the admin listing makes. A budget shown as 0% used because
    # Valkey is down is worse than one shown as unknown.
    current = await quota.current_values(rules)

    # The same envelope every other listing returns, though a person has a
    # handful of rules at most. Consistency is the point: a client that has to
    # remember which listings are wrapped and which are bare will get it wrong,
    # and the failure is a screen that renders nothing with no error.
    return page.slice(
        [
            MyLimitResponse(
                id=rule.id,
                name=rule.name,
                scope=rule.scope.value,
                metric=rule.metric.value,
                window_label=rule.window_label,
                limit_value=rule.limit_value,
                current_value=current.get(rule.id),
            )
            for rule in rules
        ]
    )
