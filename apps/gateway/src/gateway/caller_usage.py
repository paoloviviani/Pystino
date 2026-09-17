"""The caller's own quota rules and spend, read once for every door that shows it.

Extracted from ``routers/me.py`` when ADR 0074 added a second door onto the same
facts: `/api/me/limits`, `/api/me/usage` and `/api/me/usage/groups` are behind a
session cookie (``get_management_user``), which a cross-origin or bearer-only
`/v1` caller cannot present. Duplicating the three queries for
``GET /v1/pystino/usage`` would let the two doors drift — a fixed bug or a
changed rule (which scopes constrain a caller, how "estimated" is counted)
applied to one and not the other. These functions take the plain facts a caller
is authenticated by (a user id, their effective group ids) rather than a
``User`` or a ``Principal``, so both a cookie-authenticated ``User`` and a
bearer ``Principal`` — which wraps the same ``User`` row, memberships already
loaded (ADR 0057) — call the same code and get the same shape back.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import and_, case, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from gateway.config import Settings
from gateway.models import Group, LimitRule, LimitScope, UsageRecord, UsageSource, UsageStatus
from gateway.quota import QuotaEngine
from gateway.quota.notifications import thresholds_for_user
from gateway.schemas import MyLimitResponse, UsageSummaryResponse
from gateway.types import utcnow


async def caller_limits(
    session: AsyncSession,
    quota: QuotaEngine,
    *,
    user_id: uuid.UUID,
    group_ids: Iterable[uuid.UUID],
) -> list[MyLimitResponse]:
    """The rules that constrain this caller, and how much of each is spent.

    A rule applies if it is global, scoped to this user, or scoped to a group
    they are in. **API-key rules are excluded**: which key a request used is not
    known until the request is made, so a key rule is not a fact about the
    person and showing it beside their own consumption would suggest a ceiling
    they are not necessarily near.

    Quotas are *all rules must pass* (ADR 0009), so the list is a set of
    ceilings and the binding one is whichever is nearest. That is left to the
    reader rather than computed here: "nearest" across different metrics —
    euros, tokens, requests — is not a comparison this function can make
    honestly.
    """
    group_ids = list(group_ids)
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
                and_(LimitRule.scope == LimitScope.USER, LimitRule.scope_id == user_id),
                and_(
                    LimitRule.scope == LimitScope.GROUP,
                    LimitRule.scope_id.in_(group_ids) if group_ids else false(),
                ),
            ),
        )
        .order_by(LimitRule.scope, LimitRule.metric)
    )
    rules = list((await session.execute(stmt)).scalars().all())

    # Absent, not zero, when the counter store cannot be reached — a budget
    # shown as 0% used because Valkey is down is worse than one shown as
    # unknown.
    current = await quota.current_values(rules)
    my_thresholds = await thresholds_for_user(session, user_id)

    return [
        MyLimitResponse(
            id=rule.id,
            name=rule.name,
            scope=rule.scope.value,
            metric=rule.metric.value,
            window_label=rule.window_label,
            limit_value=rule.limit_value,
            current_value=current.get(rule.id),
            notification_thresholds=my_thresholds.get(rule.id, []),
        )
        for rule in rules
    ]


async def caller_usage(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    settings: Settings,
    window_seconds: int = 86_400,
) -> UsageSummaryResponse:
    """This caller's own rolling spend, over the trailing ``window_seconds``."""
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
        UsageRecord.user_id == user_id,
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


async def caller_group_usage(
    session: AsyncSession,
    *,
    group_ids: Iterable[uuid.UUID],
    settings: Settings,
    window_seconds: int = 86_400,
) -> dict[str, UsageSummaryResponse]:
    """Spend per group the caller belongs to, so they can see what they charge."""
    group_ids = list(group_ids)
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
