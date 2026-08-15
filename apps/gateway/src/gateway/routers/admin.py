"""Administration: the catalogue, prices, group access, limits and users.

Every route here requires ``is_admin``. Until these existed the only ways to
change any of it were ``gateway seed``, the pricing importer, or SQL — which is
fine for a demo and not fine for an operator.

There is no HTML admin panel; ``/docs`` is the console. That is a deliberate stop
short of Phase 2: the frontend owns presentation, and shipping a second UI here
would be a thing to migrate away from later.

Two rules the endpoints enforce rather than trust:

* **Prices are append-only.** There is no price edit, only a new effective-dated
  row, so the cost already recorded against past requests can never be rewritten.
* **Models are deactivated, never deleted.** Usage rows reference them, and
  ``ON DELETE SET NULL`` would silently turn historical spend into an
  unattributable row.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import Row, case, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from gateway.accounting.cost import select_price
from gateway.deps import AdminUserDep, ControlHttpDep, QuotaDep, SessionDep, SettingsDep
from gateway.errors import BadRequestError, GatewayError, UpstreamUnavailableError
from gateway.models import (
    ApiKey,
    Group,
    GroupModelAccess,
    LimitMetric,
    LimitRule,
    LimitScope,
    Membership,
    ModelDef,
    ModelPrice,
    PriceSource,
    QuotaReset,
    UsageRecord,
    UsageSource,
    UsageStatus,
    User,
)
from gateway.periods import PeriodKind
from gateway.pricing import CatalogueUnavailable, fetch_catalogue, parse_catalogue
from gateway.reporting import (
    GroupBy,
    GroupByParam,
    ReportFilter,
    build_report,
    report_to_csv,
    resolve_period,
)
from gateway.schemas import (
    CatalogueDiscoveryResponse,
    CatalogueDriftRow,
    DiscoveredModel,
    GroupAdminResponse,
    GroupUsageRow,
    LimitRuleCreateRequest,
    LimitRuleResetRequest,
    LimitRuleResponse,
    LimitRuleUpdateRequest,
    ModelAdminResponse,
    ModelCreateRequest,
    ModelImportRequest,
    ModelImportResponse,
    ModelImportResult,
    ModelUpdateRequest,
    PriceCreateRequest,
    PriceResponse,
    QuotaResetResponse,
    UsageReport,
    UserAdminResponse,
    UserUpdateRequest,
)
from gateway.types import utcnow


def _pairs[K, V](rows: Sequence[Row[tuple[K, V]]]) -> dict[K, V]:
    """Turn a two-column result set into a typed dict.

    Neither obvious spelling works on its own: ``dict(rows)`` loses the types
    (SQLAlchemy's ``Row`` is a Sequence, so mypy infers ``dict[Never, Never]``),
    while a ``{k: v for k, v in rows}`` comprehension keeps them and trips ruff's
    C416. Indexing satisfies both, and doing it here means explaining it once
    rather than at four call sites.
    """
    return {row[0]: row[1] for row in rows}


router = APIRouter(prefix="/api/admin", tags=["administration"])


class ConflictError(GatewayError):
    status_code = status.HTTP_409_CONFLICT
    error_type = "invalid_request_error"
    code = "already_exists"


class NotFoundError(GatewayError):
    status_code = status.HTTP_404_NOT_FOUND
    error_type = "invalid_request_error"
    code = "not_found"


def _price_response(price: ModelPrice | None) -> PriceResponse | None:
    if price is None:
        return None
    return PriceResponse(
        id=price.id,
        input_per_mtok=price.input_per_mtok,
        output_per_mtok=price.output_per_mtok,
        cache_read_per_mtok=price.cache_read_per_mtok,
        cache_write_per_mtok=price.cache_write_per_mtok,
        currency=price.currency,
        effective_from=price.effective_from,
        source=price.source.value,
    )


def _model_response(model: ModelDef, granted_to: list[str]) -> ModelAdminResponse:
    return ModelAdminResponse(
        id=model.id,
        name=model.name,
        upstream_model=model.upstream_model,
        provider=model.provider,
        display_name=model.display_name,
        description=model.description,
        is_active=model.is_active,
        context_window=model.context_window,
        max_output_tokens=model.max_output_tokens,
        created_at=model.created_at,
        current_price=_price_response(select_price(list(model.prices))),
        granted_to=granted_to,
    )


async def _grants_by_model(session: SessionDep) -> dict[uuid.UUID, list[str]]:
    rows = (
        await session.execute(
            select(GroupModelAccess.model_id, Group.name).join(
                Group, Group.id == GroupModelAccess.group_id
            )
        )
    ).all()
    grants: dict[uuid.UUID, list[str]] = {}
    for model_id, group_name in rows:
        grants.setdefault(model_id, []).append(group_name)
    return grants


async def _load_model(session: SessionDep, model_id: uuid.UUID) -> ModelDef:
    model = (
        await session.execute(
            select(ModelDef).where(ModelDef.id == model_id).options(selectinload(ModelDef.prices))
        )
    ).scalar_one_or_none()
    if model is None:
        raise NotFoundError(f"No model with id {model_id}.")
    return model


# -- models -----------------------------------------------------------------


@router.get("/models", response_model=list[ModelAdminResponse])
async def list_models(
    admin: AdminUserDep, session: SessionDep, include_inactive: bool = True
) -> list[ModelAdminResponse]:
    """Every model, including inactive ones — an operator needs to see those."""
    stmt = select(ModelDef).options(selectinload(ModelDef.prices)).order_by(ModelDef.name)
    if not include_inactive:
        stmt = stmt.where(ModelDef.is_active.is_(True))
    models = (await session.execute(stmt)).scalars().all()
    grants = await _grants_by_model(session)
    return [_model_response(model, sorted(grants.get(model.id, []))) for model in models]


@router.post("/models", response_model=ModelAdminResponse, status_code=status.HTTP_201_CREATED)
async def create_model(
    payload: ModelCreateRequest, admin: AdminUserDep, session: SessionDep
) -> ModelAdminResponse:
    """Add a model to the catalogue.

    It is invisible to callers until a group is granted access: absence of a
    ``group_model_access`` row means no access, with no global allow-all.
    """
    model = ModelDef(
        name=payload.name,
        upstream_model=payload.upstream_model,
        provider=payload.provider,
        display_name=payload.display_name,
        description=payload.description,
        context_window=payload.context_window,
        max_output_tokens=payload.max_output_tokens,
        is_active=payload.is_active,
    )
    session.add(model)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(f"A model named {payload.name!r} already exists.") from exc
    await session.refresh(model, attribute_names=["prices"])
    return _model_response(model, [])


@router.patch("/models/{model_id}", response_model=ModelAdminResponse)
async def update_model(
    model_id: uuid.UUID,
    payload: ModelUpdateRequest,
    admin: AdminUserDep,
    session: SessionDep,
) -> ModelAdminResponse:
    """Change a model in place. Deactivate here rather than deleting."""
    model = await _load_model(session, model_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(model, field, value)
    await session.commit()
    await session.refresh(model, attribute_names=["prices"])
    grants = await _grants_by_model(session)
    return _model_response(model, sorted(grants.get(model.id, [])))


# -- discovery ---------------------------------------------------------------


def _suggested_name(upstream_model: str) -> str:
    """Strip a provider prefix: ``openai/gpt-4o-mini`` -> ``gpt-4o-mini``.

    Only a suggestion; the client-facing name is ours to choose and the import
    request can override it.
    """
    return upstream_model.rsplit("/", 1)[-1] or upstream_model


@router.get("/models/discover", response_model=CatalogueDiscoveryResponse)
async def discover_models(
    admin: AdminUserDep,
    session: SessionDep,
    settings: SettingsDep,
    http: ControlHttpDep,
    url: str | None = None,
) -> CatalogueDiscoveryResponse:
    """Compare the provider's catalogue with ours.

    Nothing is created. The catalogue stays an allowlist an administrator curates
    — auto-adopting whatever a provider publishes would let their release notes
    silently change what users can spend money on. What this removes is the
    tedium of finding out *what* is on offer.

    Reports drift in both directions. Models we serve that the provider no longer
    offers are the more dangerous half: they keep appearing in ``/v1/models`` and
    fail only when someone calls them.
    """
    catalogue_url = url or f"{settings.upstream.base_url}/models"
    api_key = settings.upstream.api_key.get_secret_value() or None

    try:
        payload = await fetch_catalogue(http, catalogue_url, api_key)
    except CatalogueUnavailable as exc:
        raise UpstreamUnavailableError(f"Could not read the provider catalogue: {exc}") from exc

    prices, unparsable = parse_catalogue(payload)
    by_upstream = {price.model_id: price for price in prices}

    ours = (await session.execute(select(ModelDef).order_by(ModelDef.name))).scalars().all()
    our_upstream_ids = {model.upstream_model for model in ours}

    billing_currency = settings.billing_currency.upper()
    available: list[DiscoveredModel] = []
    for upstream_id, price in sorted(by_upstream.items()):
        if upstream_id in our_upstream_ids:
            continue
        blocked = None
        if price.currency != billing_currency:
            blocked = (
                f"priced in {price.currency}; this gateway bills in "
                f"{billing_currency}, so it cannot be imported with a price"
            )
        available.append(
            DiscoveredModel(
                upstream_model=upstream_id,
                suggested_name=_suggested_name(upstream_id),
                input_per_mtok=price.input_per_mtok,
                output_per_mtok=price.output_per_mtok,
                currency=price.currency,
                context_window=price.context_window,
                blocked_reason=blocked,
            )
        )

    catalogued: list[CatalogueDriftRow] = []
    missing: list[CatalogueDriftRow] = []
    for model in ours:
        row = CatalogueDriftRow(
            name=model.name, upstream_model=model.upstream_model, is_active=model.is_active
        )
        (catalogued if model.upstream_model in by_upstream else missing).append(row)

    return CatalogueDiscoveryResponse(
        provider_url=catalogue_url,
        provider_model_count=len(prices),
        available=available,
        catalogued=catalogued,
        missing_upstream=missing,
        unparsable=unparsable,
    )


@router.post(
    "/models/import",
    response_model=ModelImportResponse,
    status_code=status.HTTP_201_CREATED,
)
async def import_models(
    payload: ModelImportRequest,
    admin: AdminUserDep,
    session: SessionDep,
    settings: SettingsDep,
    http: ControlHttpDep,
    url: str | None = None,
) -> ModelImportResponse:
    """Adopt selected upstream models, with their published prices.

    Explicitly enumerated, never "import everything": the allowlist decision stays
    with a person, and this only removes the retyping.

    **Access is not granted.** A newly imported model is invisible until a group is
    granted it, because absence of a ``group_model_access`` row means no access and
    that invariant is worth more than one saved click.

    A model priced in another currency is **skipped entirely** rather than created
    without a price. An unpriced model serves happily and records a cost of zero,
    which is a quiet way to give away money.
    """
    catalogue_url = url or f"{settings.upstream.base_url}/models"
    api_key = settings.upstream.api_key.get_secret_value() or None

    try:
        catalogue = await fetch_catalogue(http, catalogue_url, api_key)
    except CatalogueUnavailable as exc:
        raise UpstreamUnavailableError(f"Could not read the provider catalogue: {exc}") from exc

    prices, _ = parse_catalogue(catalogue)
    by_upstream = {price.model_id: price for price in prices}
    billing_currency = settings.billing_currency.upper()

    existing = (await session.execute(select(ModelDef))).scalars().all()
    taken_names = {model.name for model in existing}
    taken_upstream = {model.upstream_model for model in existing}

    results: list[ModelImportResult] = []
    for item in payload.models:
        name = item.name or _suggested_name(item.upstream_model)
        price = by_upstream.get(item.upstream_model)

        if price is None:
            results.append(
                ModelImportResult(
                    upstream_model=item.upstream_model,
                    name=name,
                    imported=False,
                    priced=False,
                    reason="not offered by the provider",
                )
            )
            continue
        if item.upstream_model in taken_upstream:
            results.append(
                ModelImportResult(
                    upstream_model=item.upstream_model,
                    name=name,
                    imported=False,
                    priced=False,
                    reason="already in the catalogue",
                )
            )
            continue
        if name in taken_names:
            results.append(
                ModelImportResult(
                    upstream_model=item.upstream_model,
                    name=name,
                    imported=False,
                    priced=False,
                    reason=f"the name {name!r} is already taken; choose another",
                )
            )
            continue
        if price.currency != billing_currency:
            results.append(
                ModelImportResult(
                    upstream_model=item.upstream_model,
                    name=name,
                    imported=False,
                    priced=False,
                    reason=(
                        f"priced in {price.currency}, not {billing_currency}; import it "
                        "manually and price it yourself rather than serving it unpriced"
                    ),
                )
            )
            continue

        model = ModelDef(
            name=name,
            upstream_model=item.upstream_model,
            provider=settings.upstream.base_url,
            context_window=price.context_window,
        )
        session.add(model)
        await session.flush()
        session.add(
            ModelPrice(
                model_id=model.id,
                input_per_mtok=price.input_per_mtok,
                output_per_mtok=price.output_per_mtok,
                cache_read_per_mtok=price.cache_read_per_mtok,
                cache_write_per_mtok=price.cache_write_per_mtok,
                currency=price.currency,
                effective_from=utcnow(),
                source=PriceSource.CORTECS,
            )
        )
        taken_names.add(name)
        taken_upstream.add(item.upstream_model)
        results.append(
            ModelImportResult(
                upstream_model=item.upstream_model, name=name, imported=True, priced=True
            )
        )

    await session.commit()
    return ModelImportResponse(results=results)


# -- prices -----------------------------------------------------------------


@router.get("/models/{model_id}/prices", response_model=list[PriceResponse])
async def list_prices(
    model_id: uuid.UUID, admin: AdminUserDep, session: SessionDep
) -> list[PriceResponse]:
    """Full price history, newest first. Nothing here is ever mutated."""
    model = await _load_model(session, model_id)
    ordered = sorted(model.prices, key=lambda price: price.effective_from, reverse=True)
    return [response for price in ordered if (response := _price_response(price))]


@router.post(
    "/models/{model_id}/prices",
    response_model=PriceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_price(
    model_id: uuid.UUID,
    payload: PriceCreateRequest,
    admin: AdminUserDep,
    session: SessionDep,
    settings: SettingsDep,
) -> PriceResponse:
    """Append a price.

    Effective-dated, so a future date schedules a change and a past date cannot
    alter what already-recorded requests cost.
    """
    model = await _load_model(session, model_id)

    currency = (payload.currency or settings.billing_currency).upper()
    if currency != settings.billing_currency.upper():
        raise BadRequestError(
            f"This gateway bills in {settings.billing_currency}, so a price in "
            f"{currency} would have to be converted — and a silent exchange rate "
            "produces invoices that look correct and are not. Convert it yourself, "
            "or change GATEWAY_BILLING_CURRENCY.",
            code="price_currency_mismatch",
        )

    price = ModelPrice(
        model_id=model.id,
        input_per_mtok=payload.input_per_mtok,
        output_per_mtok=payload.output_per_mtok,
        cache_read_per_mtok=payload.cache_read_per_mtok,
        cache_write_per_mtok=payload.cache_write_per_mtok,
        currency=currency,
        effective_from=payload.effective_from or utcnow(),
        source=PriceSource.MANUAL,
    )
    session.add(price)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            "This model already has a price with that exact effective_from."
        ) from exc
    await session.refresh(price)
    result = _price_response(price)
    assert result is not None
    return result


# -- groups and access ------------------------------------------------------


@router.get("/groups", response_model=list[GroupAdminResponse])
async def list_groups(admin: AdminUserDep, session: SessionDep) -> list[GroupAdminResponse]:
    groups = (await session.execute(select(Group).order_by(Group.name))).scalars().all()

    counts: dict[uuid.UUID, int] = _pairs(
        (
            await session.execute(
                select(Membership.group_id, func.count(Membership.user_id)).group_by(
                    Membership.group_id
                )
            )
        ).all()
    )
    access_rows = (
        await session.execute(
            select(GroupModelAccess.group_id, ModelDef.name).join(
                ModelDef, ModelDef.id == GroupModelAccess.model_id
            )
        )
    ).all()
    models: dict[uuid.UUID, list[str]] = {}
    for group_id, model_name in access_rows:
        models.setdefault(group_id, []).append(model_name)

    return [
        GroupAdminResponse(
            id=group.id,
            name=group.name,
            description=group.description,
            source=group.source.value,
            is_active=group.is_active,
            member_count=int(counts.get(group.id, 0)),
            models=sorted(models.get(group.id, [])),
        )
        for group in groups
    ]


@router.put(
    "/groups/{group_id}/models/{model_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def grant_model_access(
    group_id: uuid.UUID, model_id: uuid.UUID, admin: AdminUserDep, session: SessionDep
) -> None:
    """Let a group use a model. Idempotent."""
    group = (await session.execute(select(Group).where(Group.id == group_id))).scalar_one_or_none()
    if group is None:
        raise NotFoundError(f"No group with id {group_id}.")
    await _load_model(session, model_id)

    existing = (
        await session.execute(
            select(GroupModelAccess).where(
                GroupModelAccess.group_id == group_id,
                GroupModelAccess.model_id == model_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(GroupModelAccess(group_id=group_id, model_id=model_id))
        await session.commit()


@router.delete(
    "/groups/{group_id}/models/{model_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_model_access(
    group_id: uuid.UUID, model_id: uuid.UUID, admin: AdminUserDep, session: SessionDep
) -> None:
    """Take the model away from a group. Idempotent, and immediate."""
    await session.execute(
        delete(GroupModelAccess).where(
            GroupModelAccess.group_id == group_id,
            GroupModelAccess.model_id == model_id,
        )
    )
    await session.commit()


# -- limit rules ------------------------------------------------------------


def _limit_response(rule: LimitRule, current: Decimal | None = None) -> LimitRuleResponse:
    return LimitRuleResponse(
        id=rule.id,
        name=rule.name,
        scope=rule.scope.value,
        scope_id=rule.scope_id,
        metric=rule.metric.value,
        window_seconds=rule.window_seconds,
        period=rule.period.value if rule.period else None,
        window_label=rule.window_label,
        limit_value=rule.limit_value,
        is_active=rule.is_active,
        current_value=current,
        # Requires `resets` to be loaded; every caller here does so explicitly,
        # because touching an unloaded relationship from async code raises
        # MissingGreenlet rather than quietly emitting a query.
        last_reset_at=max((reset.effective_at for reset in rule.resets), default=None),
    )


async def _load_rules(session: SessionDep) -> Sequence[LimitRule]:
    return (
        (
            await session.execute(
                select(LimitRule)
                .options(selectinload(LimitRule.resets))
                .order_by(LimitRule.scope, LimitRule.metric)
            )
        )
        .scalars()
        .all()
    )


@router.get("/limits", response_model=list[LimitRuleResponse])
async def list_limits(
    admin: AdminUserDep, session: SessionDep, quota: QuotaDep
) -> list[LimitRuleResponse]:
    """Every rule, with how much of it is used right now.

    ``current_value`` comes from the live counters, so it already accounts for any
    reset. It is absent — not zero — when the counter store cannot be reached.
    """
    rules = await _load_rules(session)
    current = await quota.current_values(rules)
    return [_limit_response(rule, current.get(rule.id)) for rule in rules]


@router.post("/limits", response_model=LimitRuleResponse, status_code=status.HTTP_201_CREATED)
async def create_limit(
    payload: LimitRuleCreateRequest, admin: AdminUserDep, session: SessionDep
) -> LimitRuleResponse:
    """Add a quota rule.

    All matching rules must pass, so adding one can only ever tighten the budget —
    there is no priority or override, because "most specific wins" surprises people
    when a user rule silently raises a group ceiling.
    """
    scope = LimitScope(payload.scope)
    if scope is LimitScope.GLOBAL and payload.scope_id is not None:
        raise BadRequestError("A global rule must not carry a scope_id.")
    if scope is not LimitScope.GLOBAL and payload.scope_id is None:
        raise BadRequestError(f"A {scope.value} rule needs a scope_id.")

    # Fail now with a readable message rather than later with a rule that silently
    # matches nothing.
    if scope is LimitScope.GROUP:
        exists = (
            await session.execute(select(Group.id).where(Group.id == payload.scope_id))
        ).scalar_one_or_none()
        if exists is None:
            raise NotFoundError(f"No group with id {payload.scope_id}.")
    elif scope is LimitScope.USER:
        exists = (
            await session.execute(select(User.id).where(User.id == payload.scope_id))
        ).scalar_one_or_none()
        if exists is None:
            raise NotFoundError(f"No user with id {payload.scope_id}.")
    elif scope is LimitScope.API_KEY:
        exists = (
            await session.execute(select(ApiKey.id).where(ApiKey.id == payload.scope_id))
        ).scalar_one_or_none()
        if exists is None:
            raise NotFoundError(f"No API key with id {payload.scope_id}.")

    rule = LimitRule(
        name=payload.name,
        scope=scope,
        scope_id=payload.scope_id,
        metric=LimitMetric(payload.metric),
        window_seconds=payload.window_seconds,
        period=PeriodKind(payload.period) if payload.period else None,
        limit_value=payload.limit_value,
        is_active=payload.is_active,
    )
    session.add(rule)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            "A rule already exists for that scope, metric and window. Update it "
            "instead of adding a second one."
        ) from exc
    await session.refresh(rule, attribute_names=["resets"])
    return _limit_response(rule)


async def _load_rule(session: SessionDep, rule_id: uuid.UUID) -> LimitRule:
    rule = (
        await session.execute(
            select(LimitRule).where(LimitRule.id == rule_id).options(selectinload(LimitRule.resets))
        )
    ).scalar_one_or_none()
    if rule is None:
        raise NotFoundError(f"No limit rule with id {rule_id}.")
    return rule


@router.patch("/limits/{rule_id}", response_model=LimitRuleResponse)
async def update_limit(
    rule_id: uuid.UUID,
    payload: LimitRuleUpdateRequest,
    admin: AdminUserDep,
    session: SessionDep,
    quota: QuotaDep,
) -> LimitRuleResponse:
    """Change a rule's name, limit or active flag.

    Not its window or scope: those decide the counter key, so changing one would
    abandon the consumption recorded so far and read as a quota that reset itself.
    """
    rule = await _load_rule(session, rule_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(rule, field, value)
    await session.commit()
    await session.refresh(rule, attribute_names=["resets"])
    current = await quota.current_values([rule])
    return _limit_response(rule, current.get(rule.id))


@router.post("/limits/{rule_id}/reset", response_model=QuotaResetResponse)
async def reset_limit(
    rule_id: uuid.UUID,
    payload: LimitRuleResetRequest,
    admin: AdminUserDep,
    session: SessionDep,
) -> QuotaResetResponse:
    """Set a rule's consumption back to zero, effective immediately.

    The feature Cortecs budgets do not have, and the one an administrator actually
    needs mid-project: a group hits its monthly cap on the 20th, the spend is
    legitimate, and the alternative is either raising the limit permanently or
    telling them to wait eleven days.

    Two things this deliberately is not:

    * **Not a change to the bill.** Only what the quota counts moves; the usage
      records behind every report are untouched, so the monthly total is exactly
      what it was a second ago. Enforcement and accounting stay separate.
    * **Not schedulable.** Resets take effect now. A future-dated reset is a
      scheduler, with a scheduler's failure modes, and can be driven from outside
      by calling this endpoint if it is ever wanted.

    The reason is required and kept: the audit trail is the point.
    """
    rule = await _load_rule(session, rule_id)
    reset = QuotaReset(rule_id=rule.id, created_by=admin.id, reason=payload.reason)
    session.add(reset)
    await session.commit()
    await session.refresh(reset)
    return QuotaResetResponse(
        id=reset.id,
        rule_id=reset.rule_id,
        effective_at=reset.effective_at,
        reason=reset.reason,
        created_by=reset.created_by,
        created_by_email=admin.email,
    )


@router.get("/limits/{rule_id}/resets", response_model=list[QuotaResetResponse])
async def list_resets(
    rule_id: uuid.UUID, admin: AdminUserDep, session: SessionDep
) -> list[QuotaResetResponse]:
    """Who zeroed this rule, when, and why. Newest first."""
    await _load_rule(session, rule_id)
    rows = (
        await session.execute(
            select(QuotaReset, User.email)
            .outerjoin(User, User.id == QuotaReset.created_by)
            .where(QuotaReset.rule_id == rule_id)
            .order_by(QuotaReset.effective_at.desc())
        )
    ).all()
    return [
        QuotaResetResponse(
            id=reset.id,
            rule_id=reset.rule_id,
            effective_at=reset.effective_at,
            reason=reset.reason,
            created_by=reset.created_by,
            # Null once the account is erased. The reset row itself survives, which
            # is why created_by is ON DELETE SET NULL rather than CASCADE.
            created_by_email=email,
        )
        for reset, email in rows
    ]


@router.delete("/limits/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_limit(rule_id: uuid.UUID, admin: AdminUserDep, session: SessionDep) -> None:
    """Remove a rule outright.

    Safe to delete, unlike a model or a key: nothing in the ledger references a
    rule. Counters for it simply stop being read.
    """
    await session.execute(delete(LimitRule).where(LimitRule.id == rule_id))
    await session.commit()


# -- users ------------------------------------------------------------------


@router.get("/users", response_model=list[UserAdminResponse])
async def list_users(admin: AdminUserDep, session: SessionDep) -> list[UserAdminResponse]:
    users = (
        (
            await session.execute(
                select(User)
                .options(selectinload(User.memberships).selectinload(Membership.group))
                .order_by(User.email)
            )
        )
        .scalars()
        .all()
    )

    key_counts: dict[uuid.UUID, int] = _pairs(
        (
            await session.execute(
                select(ApiKey.user_id, func.count(ApiKey.id))
                .where(ApiKey.revoked_at.is_(None))
                .group_by(ApiKey.user_id)
            )
        ).all()
    )
    group_names: dict[uuid.UUID, str] = _pairs(
        (await session.execute(select(Group.id, Group.name))).all()
    )

    return [
        UserAdminResponse(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            issuer=user.issuer,
            subject=user.subject,
            is_active=user.is_active,
            is_admin=user.is_admin,
            groups=sorted(m.group.name for m in user.memberships),
            default_billing_group=group_names.get(user.default_billing_group_id or uuid.uuid4()),
            active_key_count=int(key_counts.get(user.id, 0)),
            last_login_at=user.last_login_at,
        )
        for user in users
    ]


@router.patch("/users/{user_id}", response_model=UserAdminResponse)
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdateRequest,
    admin: AdminUserDep,
    session: SessionDep,
    settings: SettingsDep,
) -> UserAdminResponse:
    """Deactivate a user, or grant admin manually.

    Note the interaction with ``GATEWAY_OIDC__ADMIN_GROUPS``: when that is set the
    identity provider is authoritative, so an ``is_admin`` granted here is
    overwritten at the user's next login. Manage admin in the IdP, and use this for
    the local accounts that never log in through it.
    """
    user = (
        await session.execute(
            select(User)
            .where(User.id == user_id)
            .options(selectinload(User.memberships).selectinload(Membership.group))
        )
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError(f"No user with id {user_id}.")

    fields = payload.model_dump(exclude_unset=True)
    if "is_admin" in fields and settings.oidc.admin_groups and user.issuer != "local":
        raise BadRequestError(
            "GATEWAY_OIDC__ADMIN_GROUPS is configured, so admin follows identity "
            f"provider group membership ({', '.join(settings.oidc.admin_groups)}) and "
            "this change would be undone at the user's next login. Change it in the "
            "identity provider instead.",
            code="admin_managed_by_idp",
        )
    for field, value in fields.items():
        setattr(user, field, value)
    await session.commit()

    listing = await list_users(admin, session)
    return next(entry for entry in listing if entry.id == user_id)


# -- reports ------------------------------------------------------------------


@router.get("/reports/usage", response_model=UsageReport)
async def usage_report(
    admin: AdminUserDep,
    session: SessionDep,
    settings: SettingsDep,
    period: str = "",
    start: datetime | None = None,
    end: datetime | None = None,
    group_by: GroupByParam = "group",
    group_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    api_key_id: uuid.UUID | None = None,
    model: str | None = None,
) -> UsageReport:
    """Chargeback reporting: spend over a calendar period, broken down.

    ``period`` is a name — ``2026-08``, ``2026-Q3``, ``2026``, ``2026-W33``,
    ``2026-08-15`` — resolved in the billing timezone
    (``GATEWAY_BILLING_TIMEZONE``, default Europe/Rome), so the same request
    returns the same numbers next year. Defaults to the current month. Pass
    ``start`` and ``end`` instead for an arbitrary range.

    The boundaries are the ones calendar quotas use, so a monthly budget and this
    report cannot disagree about when August began.
    """
    return await build_report(
        session,
        ReportFilter(
            period=resolve_period(period, start, end, settings.billing_timezone),
            group_by=GroupBy(group_by),
            group_id=group_id,
            user_id=user_id,
            api_key_id=api_key_id,
            model_name=model,
        ),
        currency=settings.billing_currency,
        timezone=settings.billing_timezone,
    )


@router.get("/reports/usage.csv", response_class=PlainTextResponse)
async def usage_report_csv(
    admin: AdminUserDep,
    session: SessionDep,
    settings: SettingsDep,
    period: str = "",
    start: datetime | None = None,
    end: datetime | None = None,
    group_by: GroupByParam = "group",
    group_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    api_key_id: uuid.UUID | None = None,
    model: str | None = None,
) -> Response:
    """The same report as CSV, for finance.

    A separate path rather than a ``format=csv`` parameter, so the JSON endpoint
    keeps one response type and the download has a filename a browser will use.
    """
    report = await usage_report(
        admin, session, settings, period, start, end, group_by, group_id, user_id, api_key_id, model
    )
    filename = f"usage-{report.period.label}-by-{report.group_by}.csv"
    return Response(
        content=report_to_csv(report),
        media_type="text/csv; charset=utf-8",
        headers={"content-disposition": f'attachment; filename="{filename}"'},
    )


# -- usage ------------------------------------------------------------------


@router.get("/usage", response_model=list[GroupUsageRow])
async def usage_by_group(
    admin: AdminUserDep,
    session: SessionDep,
    settings: SettingsDep,
    window_seconds: int = 86_400,
) -> list[GroupUsageRow]:
    """Spend per group over a rolling window, highest first."""
    if not 60 <= window_seconds <= 366 * 86_400:
        raise BadRequestError("window_seconds must be between 60 and one year.")

    since = utcnow() - timedelta(seconds=window_seconds)
    rows = (
        await session.execute(
            select(
                UsageRecord.group_id,
                func.count(UsageRecord.id),
                func.coalesce(func.sum(UsageRecord.total_tokens), 0),
                func.coalesce(func.sum(UsageRecord.cost), 0),
                func.coalesce(
                    func.sum(case((UsageRecord.usage_source == UsageSource.ESTIMATED, 1), else_=0)),
                    0,
                ),
            )
            .where(
                UsageRecord.created_at >= since,
                UsageRecord.status != UsageStatus.IN_PROGRESS,
            )
            .group_by(UsageRecord.group_id)
        )
    ).all()

    names: dict[uuid.UUID, str] = _pairs(
        (await session.execute(select(Group.id, Group.name))).all()
    )
    result = [
        GroupUsageRow(
            group_id=group_id,
            # A usage row whose group was deleted keeps its spend visible rather
            # than vanishing from the totals.
            group_name=names.get(group_id, "(deleted group)") if group_id else "(no group)",
            requests=int(requests or 0),
            total_tokens=int(tokens or 0),
            cost=Decimal(str(cost or 0)),
            estimated_requests=int(estimated or 0),
        )
        for group_id, requests, tokens, cost, estimated in rows
    ]
    return sorted(result, key=lambda row: row.cost, reverse=True)
