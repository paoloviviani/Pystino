"""Management API: identity, billing group, API keys and spend.

Everything a user can do to their own account, plus read-only visibility of
limits. Group *membership* is deliberately not editable here: the identity
provider owns it (see :func:`gateway.oidc.provision_user`), and offering an edit
that the next login silently reverts would be worse than offering none.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

from fastapi import APIRouter, status
from sqlalchemy import case, func, select
from sqlalchemy.orm import selectinload

from gateway.deps import ManagementUserDep, SessionDep, SettingsDep
from gateway.errors import BadRequestError, PermissionError_
from gateway.models import (
    ApiKey,
    Group,
    LimitRule,
    UsageRecord,
    UsageSource,
    UsageStatus,
)
from gateway.schemas import (
    ApiKeyCreatedResponse,
    ApiKeyCreateRequest,
    ApiKeyResponse,
    GroupSummary,
    LimitRuleResponse,
    MeResponse,
    SetDefaultBillingGroupRequest,
    UsageSummaryResponse,
)
from gateway.security import generate_api_key
from gateway.types import utcnow

router = APIRouter(prefix="/api", tags=["management"])


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


@router.get("/me/keys", response_model=list[ApiKeyResponse])
async def list_keys(user: ManagementUserDep, session: SessionDep) -> list[ApiKeyResponse]:
    stmt = (
        select(ApiKey)
        .where(ApiKey.user_id == user.id)
        .options(selectinload(ApiKey.billing_group))
        .order_by(ApiKey.created_at.desc())
    )
    keys = (await session.execute(stmt)).scalars().all()
    return [_key_response(key) for key in keys]


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


@router.get("/admin/limits", response_model=list[LimitRuleResponse])
async def list_limits(user: ManagementUserDep, session: SessionDep) -> list[LimitRuleResponse]:
    if not user.is_admin:
        raise PermissionError_("Administrator access is required.")

    rules = (
        (await session.execute(select(LimitRule).order_by(LimitRule.scope, LimitRule.metric)))
        .scalars()
        .all()
    )
    return [
        LimitRuleResponse(
            id=rule.id,
            name=rule.name,
            scope=rule.scope.value,
            scope_id=rule.scope_id,
            metric=rule.metric.value,
            window_seconds=rule.window_seconds,
            limit_value=rule.limit_value,
            is_active=rule.is_active,
        )
        for rule in rules
    ]
