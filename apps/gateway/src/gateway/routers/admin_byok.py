"""Administrators' view of users' private (bring-your-own-key) providers and models.

``/api/admin/byok``. Read-mostly, and **never the credential**: not the key, not
its hint, not the header values (an owner's ``extra_headers`` can carry one).
What an administrator can see is what they need to run the deployment: who has
configured which endpoint (by host, not by full URL, since a path can carry a
token), what models sit behind it, whether it is live, when it was last used and
how much. What they can do is hold a row (``disable``) and release it
(``enable``): a separate flag from the owner's own ``is_active``, so the owner
cannot undo it. Both are audit-logged like any other administrative act.

The rows themselves are not editable here and the ordinary admin catalogue
routes cannot reach them (`gateway.byok.CatalogueScope`): a private provider is
its owner's, and the one thing an administrator may need to do with it is stop it.

The usage totals are the owner's ``byok`` ledger rows. They are shown here and
in the owner's own usage, and **nowhere else**: no group report, billing total
or quota reads them.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter
from sqlalchemy import func, or_, select

from gateway.admin_audit import record_audit
from gateway.deps import AdminUserDep, ProvidersDep, SessionDep
from gateway.errors import NotFoundError
from gateway.models import ModelDef, Provider, UsageRecord, UsageStatus, User
from gateway.pagination import Page, PageDep, count_of
from gateway.response_model import ResponseModel
from gateway.schemas import Money

router = APIRouter(prefix="/api/admin/byok", tags=["admin-byok"])


class ByokUsage(ResponseModel):
    requests: int
    total_tokens: int
    #: The sum of the rows' own costs, meaningful only in one currency: null when
    #: the rows were priced in more than one (each private price row names its own).
    cost: Money | None
    #: That one currency, or null with no priced usage or with mixed currencies.
    currency: str | None
    last_used_at: datetime | None


class ByokOwner(ResponseModel):
    id: uuid.UUID
    email: str | None
    name: str | None


class ByokProviderResponse(ResponseModel):
    id: uuid.UUID
    owner: ByokOwner
    name: str
    #: The provider type (plugin) and kind, as in the owner's own console.
    plugin: str | None
    kind: str
    #: Host only: a full URL can carry a path with a token in it.
    base_url_host: str
    has_api_key: bool
    #: The owner's own switch.
    is_active: bool
    #: The administrator's hold, which the owner cannot lift.
    admin_disabled: bool
    model_count: int
    usage: ByokUsage
    created_at: datetime


class ByokModelResponse(ResponseModel):
    id: uuid.UUID
    owner: ByokOwner
    #: The name as the owner stored it; on /v1 it is `my/<name>`.
    name: str
    api_name: str
    upstream_model: str
    kind: str
    provider_id: uuid.UUID
    provider_name: str
    is_active: bool
    admin_disabled: bool
    usage: ByokUsage
    created_at: datetime


def _owner(user: User | None, owner_id: uuid.UUID) -> ByokOwner:
    return ByokOwner(
        id=owner_id,
        email=user.email if user is not None else None,
        name=(user.display_name or user.username) if user is not None else None,
    )


def _host(base_url: str) -> str:
    return urlsplit(base_url).hostname or ""


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _usage(row: Any | None) -> ByokUsage:
    if row is None:
        return ByokUsage(
            requests=0, total_tokens=0, cost=Decimal(0), currency=None, last_used_at=None
        )
    low, high = row[4], row[5]
    mixed = low is not None and high is not None and low != high
    return ByokUsage(
        requests=int(row[0] or 0),
        total_tokens=int(row[1] or 0),
        cost=None if mixed else Decimal(str(row[2] or 0)),
        currency=None if mixed else low,
        last_used_at=row[3],
    )


def _usage_columns() -> tuple[Any, ...]:
    return (
        func.count(UsageRecord.id),
        func.coalesce(func.sum(UsageRecord.total_tokens), 0),
        func.coalesce(func.sum(UsageRecord.cost), 0),
        func.max(UsageRecord.created_at),
        func.min(UsageRecord.cost_currency),
        func.max(UsageRecord.cost_currency),
    )


@router.get("/providers", response_model=Page[ByokProviderResponse])
async def list_private_providers(
    admin: AdminUserDep,
    session: SessionDep,
    page: PageDep,
    q: str = "",
    owner_id: uuid.UUID | None = None,
) -> Page[ByokProviderResponse]:
    """Every user's private providers; ``q`` matches the owner or the provider."""
    stmt = (
        select(Provider, User)
        .outerjoin(User, User.id == Provider.owner_id)
        .where(Provider.owner_id.is_not(None))
        .order_by(func.coalesce(User.email, ""), Provider.name)
    )
    if owner_id is not None:
        stmt = stmt.where(Provider.owner_id == owner_id)
    if needle := q.strip():
        like = _like(needle)
        stmt = stmt.where(
            or_(
                Provider.name.ilike(like, escape="\\"),
                User.email.ilike(like, escape="\\"),
                User.display_name.ilike(like, escape="\\"),
                User.username.ilike(like, escape="\\"),
            )
        )
    total = await count_of(session, stmt)
    rows = (await session.execute(page.apply(stmt))).all()
    return page.page(await _provider_responses(session, [(r[0], r[1]) for r in rows]), total)


async def _provider_responses(
    session: SessionDep, rows: list[tuple[Provider, User | None]]
) -> list[ByokProviderResponse]:
    ids = [provider.id for provider, _ in rows]
    counts: dict[uuid.UUID, int] = {}
    usage: dict[uuid.UUID, Any] = {}
    if ids:
        counts = {
            pid: int(n)
            for pid, n in (
                await session.execute(
                    select(ModelDef.provider_id, func.count(ModelDef.id))
                    .where(ModelDef.provider_id.in_(ids))
                    .group_by(ModelDef.provider_id)
                )
            ).all()
            if pid is not None
        }
        usage = {
            pid: row[1:]
            for row in (
                await session.execute(
                    select(ModelDef.provider_id, *_usage_columns())
                    .join(ModelDef, ModelDef.id == UsageRecord.model_id)
                    .where(
                        ModelDef.provider_id.in_(ids),
                        UsageRecord.byok.is_(True),
                        UsageRecord.status != UsageStatus.IN_PROGRESS,
                    )
                    .group_by(ModelDef.provider_id)
                )
            ).all()
            if (pid := row[0]) is not None
        }
    items = []
    for provider, owner in rows:
        assert provider.owner_id is not None
        items.append(
            ByokProviderResponse(
                id=provider.id,
                owner=_owner(owner, provider.owner_id),
                name=provider.name,
                plugin=provider.plugin,
                kind=provider.kind.value,
                base_url_host=_host(provider.base_url),
                has_api_key=bool(provider.api_key_encrypted),
                is_active=provider.is_active,
                admin_disabled=provider.admin_disabled,
                model_count=counts.get(provider.id, 0),
                usage=_usage(usage.get(provider.id)),
                created_at=provider.created_at,
            )
        )
    return items


@router.get("/models", response_model=Page[ByokModelResponse])
async def list_private_models(
    admin: AdminUserDep,
    session: SessionDep,
    page: PageDep,
    q: str = "",
    owner_id: uuid.UUID | None = None,
    provider_id: uuid.UUID | None = None,
) -> Page[ByokModelResponse]:
    """Every user's private models; ``q`` matches the owner's email or name, or the model's."""
    stmt = (
        select(ModelDef, Provider, User)
        .join(Provider, Provider.id == ModelDef.provider_id)
        .outerjoin(User, User.id == ModelDef.owner_id)
        .where(ModelDef.owner_id.is_not(None))
        .order_by(func.coalesce(User.email, ""), ModelDef.name)
    )
    if owner_id is not None:
        stmt = stmt.where(ModelDef.owner_id == owner_id)
    if provider_id is not None:
        stmt = stmt.where(ModelDef.provider_id == provider_id)
    if needle := q.strip():
        like = _like(needle)
        stmt = stmt.where(
            or_(
                ModelDef.name.ilike(like, escape="\\"),
                ModelDef.upstream_model.ilike(like, escape="\\"),
                User.email.ilike(like, escape="\\"),
                User.display_name.ilike(like, escape="\\"),
                User.username.ilike(like, escape="\\"),
            )
        )
    total = await count_of(session, stmt)
    rows = (await session.execute(page.apply(stmt))).all()
    return page.page(await _model_responses(session, [(r[0], r[1], r[2]) for r in rows]), total)


async def _model_responses(
    session: SessionDep, rows: list[tuple[ModelDef, Provider, User | None]]
) -> list[ByokModelResponse]:
    ids = [model.id for model, _, _ in rows]
    usage: dict[uuid.UUID, Any] = {}
    if ids:
        usage = {
            row[0]: row[1:]
            for row in (
                await session.execute(
                    select(UsageRecord.model_id, *_usage_columns())
                    .where(
                        UsageRecord.model_id.in_(ids),
                        UsageRecord.byok.is_(True),
                        UsageRecord.status != UsageStatus.IN_PROGRESS,
                    )
                    .group_by(UsageRecord.model_id)
                )
            ).all()
        }
    items = []
    for model, provider, owner in rows:
        assert model.owner_id is not None and model.upstream_model is not None
        items.append(
            ByokModelResponse(
                id=model.id,
                owner=_owner(owner, model.owner_id),
                name=model.name,
                api_name=model.api_name,
                upstream_model=model.upstream_model,
                kind=model.kind.value,
                provider_id=provider.id,
                provider_name=provider.name,
                is_active=model.is_active,
                admin_disabled=model.admin_disabled,
                usage=_usage(usage.get(model.id)),
                created_at=model.created_at,
            )
        )
    return items


async def _private_provider(
    session: SessionDep, provider_id: uuid.UUID
) -> tuple[Provider, User | None]:
    row = (
        await session.execute(
            select(Provider, User)
            .outerjoin(User, User.id == Provider.owner_id)
            .where(Provider.id == provider_id, Provider.owner_id.is_not(None))
        )
    ).first()
    if row is None:
        # A deployment provider's id is "no such private provider": this surface
        # is the one place private rows are reached, and it never touches the rest.
        raise NotFoundError(f"No private provider with id {provider_id}.")
    return row[0], row[1]


async def _private_model(session: SessionDep, model_id: uuid.UUID) -> tuple[ModelDef, User | None]:
    row = (
        await session.execute(
            select(ModelDef, User)
            .outerjoin(User, User.id == ModelDef.owner_id)
            .where(ModelDef.id == model_id, ModelDef.owner_id.is_not(None))
        )
    ).first()
    if row is None:
        raise NotFoundError(f"No private model with id {model_id}.")
    return row[0], row[1]


async def _set_provider_hold(
    session: SessionDep,
    providers: Any,
    admin: User,
    provider_id: uuid.UUID,
    *,
    held: bool,
) -> ByokProviderResponse:
    provider, owner = await _private_provider(session, provider_id)
    before = {"admin_disabled": provider.admin_disabled, "owner": owner.email if owner else None}
    provider.admin_disabled = held
    record_audit(
        session,
        admin,
        "byok.provider.disable" if held else "byok.provider.enable",
        "provider",
        provider.id,
        provider.name,
        before=before,
        after={"admin_disabled": held, "owner": before["owner"]},
    )
    await session.commit()
    await session.refresh(provider)
    # Whatever client was cached for it was built while it was live.
    await providers.forget(provider.id)
    return (await _provider_responses(session, [(provider, owner)]))[0]


@router.post("/providers/{provider_id}/disable", response_model=ByokProviderResponse)
async def disable_private_provider(
    provider_id: uuid.UUID, admin: AdminUserDep, session: SessionDep, providers: ProvidersDep
) -> ByokProviderResponse:
    """Hold a user's private provider: unusable until released, and the owner cannot undo it."""
    return await _set_provider_hold(session, providers, admin, provider_id, held=True)


@router.post("/providers/{provider_id}/enable", response_model=ByokProviderResponse)
async def enable_private_provider(
    provider_id: uuid.UUID, admin: AdminUserDep, session: SessionDep, providers: ProvidersDep
) -> ByokProviderResponse:
    """Release an administrator's hold on a private provider."""
    return await _set_provider_hold(session, providers, admin, provider_id, held=False)


async def _set_model_hold(
    session: SessionDep, admin: User, model_id: uuid.UUID, *, held: bool
) -> ByokModelResponse:
    model, owner = await _private_model(session, model_id)
    provider = await session.get(Provider, model.provider_id)
    assert provider is not None
    before = {"admin_disabled": model.admin_disabled, "owner": owner.email if owner else None}
    model.admin_disabled = held
    record_audit(
        session,
        admin,
        "byok.model.disable" if held else "byok.model.enable",
        "model",
        model.id,
        model.name,
        before=before,
        after={"admin_disabled": held, "owner": before["owner"]},
    )
    await session.commit()
    await session.refresh(model)
    return (await _model_responses(session, [(model, provider, owner)]))[0]


@router.post("/models/{model_id}/disable", response_model=ByokModelResponse)
async def disable_private_model(
    model_id: uuid.UUID, admin: AdminUserDep, session: SessionDep
) -> ByokModelResponse:
    """Hold a user's private model: unusable until released, and the owner cannot undo it."""
    return await _set_model_hold(session, admin, model_id, held=True)


@router.post("/models/{model_id}/enable", response_model=ByokModelResponse)
async def enable_private_model(
    model_id: uuid.UUID, admin: AdminUserDep, session: SessionDep
) -> ByokModelResponse:
    """Release an administrator's hold on a private model."""
    return await _set_model_hold(session, admin, model_id, held=False)
