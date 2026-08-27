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
from collections.abc import Collection
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import case, func, select
from sqlalchemy.orm import selectinload

from gateway.config import (
    DEFAULT_REDACTION_POLICY,
    EntityMode,
    RedactionPolicy,
    Settings,
)
from gateway.deps import ManagementUserDep, SessionDep, SettingsDep
from gateway.errors import BadRequestError, PermissionError_
from gateway.models import (
    ApiKey,
    Group,
    RedactionRule,
    RedactionScope,
    UsageRecord,
    UsageSource,
    UsageStatus,
    User,
)
from gateway.pagination import Page, PageDep, count_of
from gateway.redaction.resolver import RedactionResolver
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
    MyRedactionRequest,
    MyRedactionResponse,
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


# -- a person's own redaction policy -----------------------------------------
#
# The user scope of ADR 0038, from the inside. Two properties make this safe to
# expose without an admin in the loop, and they are worth stating because they
# are why there is no approval step:
#
# * **it can only tighten** — `RedactionPolicy.combine` takes the strictest
#   answer for every entity type, so nothing written here can loosen what an
#   administrator set, whatever it says; and
# * **it is the caller's own row and no other** — the scope_id is taken from the
#   session, never from the body, so there is no id to get wrong.
#
# The validation below therefore exists for the *error message*, not for safety:
# a weakening rule saved anyway would be inert, and "accepted and silently
# ignored" reads to the person who wrote it as the feature not working.
#
# Note what this surface is not: `/api/me` authenticates from the OIDC session
# cookie, so an API key cannot set its owner's policy. That is the right default
# for a control whose blast radius is every key the person owns — including the
# unattended ones. Because it can only tighten, the symptom of a mistake is
# "my agent's prompts come back with more placeholders", never "my key stopped
# working".


def _resolver(request: Request) -> RedactionResolver | None:
    return getattr(request.app.state, "redaction", None)


def _fold_for(
    resolver: RedactionResolver | None,
    user: User,
    settings: Settings,
    *,
    include_own: bool,
) -> RedactionPolicy:
    """The deployment policy folded with this person's group rules, and their own.

    **Every** group they belong to, not just the one they bill to by default.
    Which group pays is a per-request fact and is not known here, and folding all
    of them can only make the answer stricter — which is the safe direction for a
    floor whose only job is to explain a refusal.

    Provider- and model-scoped rules are absent for the same reason and cannot be
    fixed here: which model a request names is not knowable outside a request.
    They still apply at request time, and can only tighten this further.

    ``include_own=False`` gives the floor a personal policy is measured against —
    with one exception that is the whole reason this is not a one-line filter. A
    user-scoped rule an **administrator** wrote is not the person's own policy; it
    is a decision made about them, and leaving it out of the floor would let them
    PUT a weaker one and relax it to the group's level. So a user-scoped rule is
    excluded only when that person wrote it themselves.
    """
    if resolver is None:
        return settings.redaction.policy
    imposed = _imposed_on(resolver, user) if not include_own else None
    user_id = user.id if include_own else None
    groups = user.group_ids()
    if not groups:
        own = resolver.policy_for(user_id=user_id).policy
        return RedactionPolicy.combine([own, imposed]) if imposed else own
    # The deployment's policy first: `combine` takes the allow-list from the
    # first policy alone, and the first must always be the deployment's.
    return RedactionPolicy.combine(
        [
            resolver.policy,
            *(
                resolver.policy_for(group_id=group_id, user_id=user_id).policy
                for group_id in groups
            ),
            *([imposed] if imposed is not None else []),
        ]
    )


def _imposed_on(resolver: RedactionResolver, user: User) -> RedactionPolicy | None:
    """A user-scoped rule somebody else wrote about this person, if there is one.

    One row per subject means an admin-authored `scope=user` rule and a person's
    own policy are the *same* row, so "their own" cannot be decided by scope
    alone — only by who wrote it. Without this an administrator's rule about one
    person is the one rule that person can overrule, which is precisely backwards.
    """
    rule = resolver.rule_for(RedactionScope.USER, user.id)
    if rule is None or rule.created_by == user.id:
        return None
    return rule.policy


def _weakening(
    candidate: RedactionPolicy, baseline: RedactionPolicy, known: Collection[str]
) -> str | None:
    """Which entity type *candidate* protects less than *baseline*, in words.

    `RedactionPolicy.weakens` is what *decides*; this only names the offender, so
    the refusal can say "PERSON: you asked for off where the deployment requires
    anonymise_restore" rather than "this weakens the policy". The two walk the
    same comparisons deliberately — a message that named the wrong field would be
    worse than one that named nothing.
    """
    order = list(EntityMode)
    if order.index(candidate.default_mode) < order.index(baseline.default_mode):
        return (
            f"the default for every unlisted type ({candidate.default_mode.value}, "
            f"against {baseline.default_mode.value})"
        )
    for name in sorted(set(candidate.entities) | set(baseline.entities) | set(known)):
        mine, theirs = candidate.mode_for(name), baseline.mode_for(name)
        if order.index(mine) < order.index(theirs):
            return f"{name} ({mine.value}, against {theirs.value})"
        # A raised threshold catches fewer spans, so it protects less even though
        # the mode is unchanged — the failure dressed as a tightening.
        if candidate.threshold_for(name, 0.0) > baseline.threshold_for(name, 0.0):
            return (
                f"{name} (a confidence threshold of "
                f"{candidate.threshold_for(name, 0.0)}, against "
                f"{baseline.threshold_for(name, 0.0)})"
            )
    return None


async def _own_rule(session: SessionDep, user: User) -> RedactionRule | None:
    return (
        await session.execute(
            select(RedactionRule).where(
                RedactionRule.scope == RedactionScope.USER, RedactionRule.scope_id == user.id
            )
        )
    ).scalar_one_or_none()


def _my_redaction_response(
    rule: RedactionRule | None,
    *,
    effective: RedactionPolicy,
    baseline: RedactionPolicy,
    propagation_seconds: float,
) -> MyRedactionResponse:
    return MyRedactionResponse(
        policy=RedactionPolicy.model_validate(rule.policy or {}) if rule is not None else None,
        rule_id=rule.id if rule is not None else None,
        updated_at=rule.updated_at if rule is not None else None,
        effective=effective,
        baseline=baseline,
        propagation_seconds=propagation_seconds,
    )


@router.get("/me/redaction", response_model=MyRedactionResponse)
async def my_redaction(
    user: ManagementUserDep,
    session: SessionDep,
    settings: SettingsDep,
    request: Request,
) -> MyRedactionResponse:
    """What is redacted in this person's requests, and what they may change.

    Three documents, because one is not enough to act on: their own rule (which
    may not exist), what actually applies to them, and the floor below which a
    change will be refused.
    """
    resolver = _resolver(request)
    return _my_redaction_response(
        await _own_rule(session, user),
        effective=_fold_for(resolver, user, settings, include_own=True),
        baseline=_fold_for(resolver, user, settings, include_own=False),
        propagation_seconds=resolver.refresh_seconds if resolver is not None else 0.0,
    )


@router.put("/me/redaction", response_model=MyRedactionResponse)
async def set_my_redaction(
    payload: MyRedactionRequest,
    user: ManagementUserDep,
    session: SessionDep,
    settings: SettingsDep,
    request: Request,
) -> MyRedactionResponse:
    """Tighten your own redaction. Two refusals, both about protecting less.

    **An allow-list is refused outright.** It is the one field in a policy that
    can only weaken — "never redact this value, whatever the detector says" — and
    neither way of combining one across scopes is safe: union lets a person
    exempt what an administrator redacts, and intersection silently voids the
    deployment's own exemptions the moment any scoped rule carries an empty list.
    So it stays on `redaction_config`, admin-only (ADR 0037, ADR 0038 §3).

    **A policy weaker than the administrators' is refused, by name.** The floor
    is the deployment policy folded with the rules set for this person's groups.
    Model- and provider-scoped rules are not in it because they are not knowable
    outside a request; they can only tighten the result further, never loosen it,
    so leaving them out cannot make an accepted policy unsafe.

    One consequence the console has to be built around: a type this policy does
    not name falls back to *its own* ``default_mode``, so omitting a type the
    administrators named at a stronger mode is itself a weakening and is refused.
    The document to edit is therefore the ``baseline`` this route returns, not an
    empty one — which is why the GET returns it.
    """
    if payload.policy.allow_list:
        raise BadRequestError(
            "A personal redaction policy may not carry an allow-list: exempting a "
            "value from redaction is the one change that protects less, and it "
            "stays an administrator's decision.",
            code="redaction_allow_list_forbidden",
        )

    resolver = _resolver(request)
    baseline = _fold_for(resolver, user, settings, include_own=False)
    # The types nobody has written a rule about are where a downgrade hides, so
    # the comparison covers every type either policy names plus the ones the
    # deployment default protects. The detector's own list would be the complete
    # answer and is deliberately not fetched: it costs an HTTP call to a service
    # that may be down, and a person's policy change must not fail because the
    # detection service is unreachable. `weakens` compares `default_mode`
    # directly, which is what closes the gap that list would have closed.
    known = set(DEFAULT_REDACTION_POLICY.entities) | set(baseline.entities)
    if payload.policy.weakens(baseline, known=known):
        named = _weakening(payload.policy, baseline, known) or "a value it exempts"
        raise BadRequestError(
            "This policy protects less than the one your administrators set, so it "
            f"was not saved: {named}. Your own policy may only tighten.",
            code="redaction_policy_weakens",
        )

    rule = await _own_rule(session, user)
    if rule is None:
        rule = RedactionRule(
            name=f"{user.email or user.subject}'s own policy",
            scope=RedactionScope.USER,
            # From the session, never from the body: there is no id here to get
            # wrong, and therefore none to forge.
            scope_id=user.id,
            created_by=user.id,
        )
        session.add(rule)
    rule.policy = payload.policy.model_dump(mode="json")
    rule.reason = payload.reason
    rule.is_active = True
    await session.commit()
    await session.refresh(rule)

    # This worker, now: the other workers pick it up within the poll interval,
    # which is what `propagation_seconds` in the response is for.
    if resolver is not None:
        await resolver.refresh()

    return _my_redaction_response(
        rule,
        effective=_fold_for(resolver, user, settings, include_own=True),
        baseline=baseline,
        propagation_seconds=resolver.refresh_seconds if resolver is not None else 0.0,
    )
