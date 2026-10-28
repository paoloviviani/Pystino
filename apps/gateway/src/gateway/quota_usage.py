"""Which quotas apply to each user, and how much of each they have used.

The data behind the console's "Quota usage" page. For a user it assembles every
active rule that constrains them (their own ``scope=user`` rules, the global
``each_user`` rules, the ``each_member`` rules of their groups, and the shared
group or global totals they count towards), reads the figure for *that user*
through the engine (so a whole-rule reset and their own per-user reset both
apply, as they do to the next request), and marks the rules an override
replaces rather than listing them as a second budget.

API-key rules are left out for the reason ``caller_limits`` gives: which key a
request uses is not a fact about the person.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from sqlalchemy import and_, false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from gateway.models import (
    Group,
    LimitAppliesTo,
    LimitRule,
    LimitScope,
    Membership,
    QuotaReset,
    User,
)
from gateway.quota import Measurement, QuotaEngine, QuotaSubject
from gateway.quota.engine import is_per_user
from gateway.schemas import UserQuotaEntry
from gateway.types import utcnow


def kind_of(rule: LimitRule) -> str:
    if rule.scope is LimitScope.USER:
        return "own"
    if rule.applies_to is LimitAppliesTo.EACH_USER:
        return "each_user"
    if rule.applies_to is LimitAppliesTo.EACH_MEMBER:
        return "each_member"
    return "shared"


async def user_quota_entries(
    session: AsyncSession, quota: QuotaEngine, users: Sequence[User]
) -> dict[uuid.UUID, list[UserQuotaEntry]]:
    """Every applicable rule for each of *users*, in one pass over the rules.

    Rules, memberships, group names and reset authors are read once for the
    whole set, so a page of users costs a constant number of queries plus one
    counter read for the whole page.
    """
    ids = [user.id for user in users]
    if not ids:
        return {}
    memberships: dict[uuid.UUID, set[uuid.UUID]] = {user_id: set() for user_id in ids}
    for user_id, group_id in (
        await session.execute(
            select(Membership.user_id, Membership.group_id).where(Membership.user_id.in_(ids))
        )
    ).all():
        memberships[user_id].add(group_id)
    group_ids = {group_id for groups in memberships.values() for group_id in groups}

    rules = list(
        (
            await session.execute(
                select(LimitRule)
                # Only the whole-rule resets and those of the listed users: a
                # per-user rule may hold one reset row per user in the system.
                .options(
                    selectinload(
                        LimitRule.resets.and_(
                            or_(QuotaReset.user_id.is_(None), QuotaReset.user_id.in_(ids))
                        )
                    )
                )
                .where(
                    LimitRule.is_active.is_(True),
                    or_(
                        LimitRule.scope == LimitScope.GLOBAL,
                        and_(LimitRule.scope == LimitScope.USER, LimitRule.scope_id.in_(ids)),
                        and_(
                            LimitRule.scope == LimitScope.GROUP,
                            LimitRule.scope_id.in_(group_ids) if group_ids else false(),
                        ),
                    ),
                )
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    group_names: dict[uuid.UUID, str] = (
        {
            row[0]: row[1]
            for row in await session.execute(
                select(Group.id, Group.name).where(Group.id.in_(group_ids))
            )
        }
        if group_ids
        else {}
    )
    authors = {
        reset.created_by
        for rule in rules
        for reset in rule.resets
        if reset.created_by is not None and (reset.user_id is None or reset.user_id in ids)
    }
    emails: dict[uuid.UUID, str | None] = (
        {
            row[0]: row[1]
            for row in await session.execute(
                select(User.id, User.email).where(User.id.in_(authors))
            )
        }
        if authors
        else {}
    )

    now = utcnow().timestamp()
    applicable: dict[uuid.UUID, list[LimitRule]] = {}
    for user in users:
        applicable[user.id] = [
            rule
            for rule in rules
            if rule.scope is LimitScope.GLOBAL
            or (rule.scope is LimitScope.USER and rule.scope_id == user.id)
            or (rule.scope is LimitScope.GROUP and rule.scope_id in memberships[user.id])
        ]
    enforced_by_user = {uid: QuotaEngine.drop_overridden(mine) for uid, mine in applicable.items()}
    batch = await quota.measure_many(
        [
            (QuotaSubject(user_id=user.id, group_id=None), enforced_by_user[user.id])
            for user in users
        ],
        now=now,
    )
    out: dict[uuid.UUID, list[UserQuotaEntry]] = {}
    for user, measured in zip(users, batch, strict=True):
        mine = applicable[user.id]
        kept = {rule.id for rule in enforced_by_user[user.id]}
        entries = [
            _entry(
                rule,
                user.id,
                replaced_by=None if rule.id in kept else _replacement(rule, mine),
                measured=measured.get(rule.id),
                group_names=group_names,
                emails=emails,
            )
            for rule in mine
        ]
        # The binding quota first: highest percentage used, unknowns last, and
        # a replaced rule (not a budget this user is held to) after everything.
        entries.sort(
            key=lambda e: (
                e.replaced_by_rule_id is not None,
                -(e.percent if e.percent is not None else -1),
                e.rule_name,
            )
        )
        out[user.id] = entries
    return out


async def override_replacing(
    session: AsyncSession, rule: LimitRule, user_id: uuid.UUID
) -> LimitRule | None:
    """The user's active override that replaces *rule* for them, if any."""
    if rule.scope is not LimitScope.GLOBAL or rule.applies_to is not LimitAppliesTo.EACH_USER:
        return None
    candidates = (
        (
            await session.execute(
                select(LimitRule).where(
                    LimitRule.is_active.is_(True),
                    LimitRule.scope == LimitScope.USER,
                    LimitRule.scope_id == user_id,
                    LimitRule.overrides_each_user.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    return _replacement(rule, candidates)


def _replacement(rule: LimitRule, applicable: Sequence[LimitRule]) -> LimitRule | None:
    return next(
        (
            other
            for other in applicable
            if other.scope is LimitScope.USER
            and other.overrides_each_user
            and (other.metric, other.window_seconds, other.period)
            == (rule.metric, rule.window_seconds, rule.period)
        ),
        None,
    )


def _entry(
    rule: LimitRule,
    user_id: uuid.UUID,
    *,
    replaced_by: LimitRule | None,
    measured: Measurement | None,
    group_names: dict[uuid.UUID, str],
    emails: dict[uuid.UUID, str | None],
) -> UserQuotaEntry:
    value: Decimal | None = None
    resets_at: datetime | None = None
    estimate = False
    if measured is not None:
        value, resets_at = measured.value, measured.resets_at
        estimate = measured.resets_at_is_estimate
    percent = (
        int(value / rule.limit_value * 100) if value is not None and rule.limit_value > 0 else None
    )
    relevant = [r for r in rule.resets if r.user_id is None or r.user_id == user_id]
    latest: QuotaReset | None = max(relevant, key=lambda r: r.effective_at, default=None)
    return UserQuotaEntry(
        rule_id=rule.id,
        rule_name=rule.name,
        kind=kind_of(rule),
        scope=rule.scope.value,
        scope_label=group_names.get(rule.scope_id)
        if rule.scope is LimitScope.GROUP and rule.scope_id
        else None,
        metric=rule.metric.value,
        limit_value=rule.limit_value,
        current_value=value,
        percent=percent,
        window_label=rule.window_label,
        window_seconds=rule.window_seconds,
        period=rule.period.value if rule.period else None,
        resets_at=resets_at,
        resets_at_is_estimate=estimate,
        last_reset_at=latest.effective_at if latest else None,
        last_reset_by_email=emails.get(latest.created_by) if latest and latest.created_by else None,
        last_reset_for_user=latest is not None and latest.user_id is not None,
        can_reset_for_user=is_per_user(rule) and replaced_by is None,
        replaced_by_rule_id=replaced_by.id if replaced_by else None,
        replaced_by_name=replaced_by.name if replaced_by else None,
    )
