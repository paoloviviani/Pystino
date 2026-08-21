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

import time
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import ColumnElement, Row, case, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import InstrumentedAttribute, selectinload

from gateway.accounting.cost import select_price
from gateway.config import RedactionSettings, Settings
from gateway.deps import (
    AdminUserDep,
    ControlHttpDep,
    ProvidersDep,
    QuotaDep,
    SecretsDep,
    SessionDep,
    SettingsDep,
)
from gateway.errors import BadRequestError, GatewayError, UpstreamUnavailableError
from gateway.models import (
    ApiKey,
    AuthScheme,
    Group,
    GroupModelAccess,
    LimitMetric,
    LimitRule,
    LimitScope,
    Membership,
    ModelDef,
    ModelKind,
    ModelPrice,
    PriceSource,
    Provider,
    QuotaReset,
    UsageRecord,
    UsageSource,
    UsageStatus,
    User,
    UserModelAccess,
)
from gateway.pagination import Page, PageDep, count_of
from gateway.periods import PeriodKind
from gateway.pricing import CatalogueUnavailable, fetch_catalogue, parse_catalogue
from gateway.providers import ProviderConfigurationError
from gateway.redaction import Redactor
from gateway.redaction import registry as redaction_registry
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
    ProviderCreateRequest,
    ProviderResponse,
    ProviderTestResponse,
    ProviderUpdateRequest,
    QuotaResetResponse,
    RedactionActivity,
    RedactionServiceHealth,
    RedactionStatusResponse,
    UsageReport,
    UserAdminResponse,
    UserUpdateRequest,
)
from gateway.secrets import SecretBox, SecretsUnavailableError, hint_for
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


def _matches(needle: str, *columns: InstrumentedAttribute[str | None]) -> ColumnElement[bool]:
    """A case-insensitive substring search across several columns.

    Once a listing is paginated, filtering has to happen in the database:
    narrowing the fifty rows that came back is not a search, it is a search of
    page one. So the console's search box is a query parameter now.

    ``%`` and ``_`` in what the operator typed are escaped, or a search for
    ``gpt_4`` would quietly also match ``gpt-4``. ``ilike`` is native on
    Postgres and emulated by SQLAlchemy elsewhere, which is what the tests run
    on.
    """
    escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    return or_(*(column.ilike(pattern, escape="\\") for column in columns))


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
        per_image=price.per_image,
        currency=price.currency,
        effective_from=price.effective_from,
        source=price.source.value,
    )


def _model_response(
    model: ModelDef, granted_to: list[str], granted_to_users: list[str] | None = None
) -> ModelAdminResponse:
    return ModelAdminResponse(
        id=model.id,
        name=model.name,
        upstream_model=model.upstream_model,
        provider_id=model.provider_id,
        provider_name=model.provider.name,
        # Surfaced per model because deactivating a provider silently takes every
        # model behind it out of service, and the catalogue is where that is
        # noticed.
        provider_is_active=model.provider.is_active,
        kind=model.kind.value,
        display_name=model.display_name,
        description=model.description,
        is_active=model.is_active,
        context_window=model.context_window,
        max_output_tokens=model.max_output_tokens,
        input_modalities=list(model.input_modalities or []),
        output_modalities=list(model.output_modalities or []),
        supported_features=list(model.supported_features or []),
        created_at=model.created_at,
        current_price=_price_response(select_price(list(model.prices))),
        granted_to=granted_to,
        granted_to_users=granted_to_users or [],
    )


async def _grants_by_model(
    session: SessionDep, model_ids: Sequence[uuid.UUID] | None = None
) -> dict[uuid.UUID, list[str]]:
    """Group grants, narrowed to the models being rendered.

    The listing shows one page of models, so the grant tables are read for that
    page only: unbounded here means catalogue size times group count.
    """
    stmt = select(GroupModelAccess.model_id, Group.name).join(
        Group, Group.id == GroupModelAccess.group_id
    )
    if model_ids is not None:
        stmt = stmt.where(GroupModelAccess.model_id.in_(model_ids))
    rows = (await session.execute(stmt)).all()
    grants: dict[uuid.UUID, list[str]] = {}
    for model_id, group_name in rows:
        grants.setdefault(model_id, []).append(group_name)
    return grants


async def _user_grants_by_model(
    session: SessionDep, model_ids: Sequence[uuid.UUID] | None = None
) -> dict[uuid.UUID, list[str]]:
    """Personal grants, which are unioned with group grants at request time."""
    stmt = select(UserModelAccess.model_id, func.coalesce(User.email, User.subject)).join(
        User, User.id == UserModelAccess.user_id
    )
    if model_ids is not None:
        stmt = stmt.where(UserModelAccess.model_id.in_(model_ids))
    rows = (await session.execute(stmt)).all()
    grants: dict[uuid.UUID, list[str]] = {}
    for model_id, label in rows:
        grants.setdefault(model_id, []).append(label)
    return grants


async def _load_model(session: SessionDep, model_id: uuid.UUID) -> ModelDef:
    model = (
        await session.execute(
            select(ModelDef)
            .where(ModelDef.id == model_id)
            .options(selectinload(ModelDef.prices), selectinload(ModelDef.provider))
        )
    ).scalar_one_or_none()
    if model is None:
        raise NotFoundError(f"No model with id {model_id}.")
    return model


async def _load_provider(session: SessionDep, provider_id: uuid.UUID) -> Provider:
    provider = (
        await session.execute(select(Provider).where(Provider.id == provider_id))
    ).scalar_one_or_none()
    if provider is None:
        raise NotFoundError(f"No provider with id {provider_id}.")
    return provider


# -- providers ---------------------------------------------------------------
#
# Adding an inference endpoint is configuration, not a deploy (ADR 0027). The
# API key is write-only throughout: it goes in encrypted and never comes back
# out, only a hint.


def _provider_response(provider: Provider, model_count: int) -> ProviderResponse:
    return ProviderResponse(
        id=provider.id,
        name=provider.name,
        description=provider.description,
        base_url=provider.base_url,
        api_key_hint=provider.api_key_hint,
        has_api_key=bool(provider.api_key_encrypted),
        extra_headers=dict(provider.extra_headers or {}),
        is_active=provider.is_active,
        forward_stream_options=provider.forward_stream_options,
        auth_scheme=provider.auth_scheme.value,
        upstream_cost_unit=provider.upstream_cost_unit,
        model_count=model_count,
        created_at=provider.created_at,
        updated_at=provider.updated_at,
    )


async def _model_counts(session: SessionDep) -> dict[uuid.UUID, int]:
    """How many models each provider serves.

    Shown beside every provider because it is the blast radius of deactivating
    or deleting one, and an operator should see it before the click rather than
    after.
    """
    rows = (
        await session.execute(
            select(ModelDef.provider_id, func.count(ModelDef.id)).group_by(ModelDef.provider_id)
        )
    ).all()
    return _pairs(rows)


def _store_api_key(provider: Provider, secrets: SecretBox, plaintext: str) -> None:
    """Encrypt and attach a credential, or clear it.

    The hint is derived here, at write time, so displaying a provider never
    needs the decryption key.
    """
    if not plaintext:
        provider.api_key_encrypted = ""
        provider.api_key_hint = ""
        return
    try:
        provider.api_key_encrypted = secrets.encrypt(plaintext)
    except SecretsUnavailableError as exc:
        raise BadRequestError(str(exc), code="secret_key_missing") from exc
    provider.api_key_hint = hint_for(plaintext)


@router.get("/providers", response_model=Page[ProviderResponse])
async def list_providers(
    admin: AdminUserDep, session: SessionDep, page: PageDep
) -> Page[ProviderResponse]:
    stmt = select(Provider).order_by(Provider.name)
    total = await count_of(session, stmt)
    providers = (await session.execute(page.apply(stmt))).scalars().all()
    counts = await _model_counts(session)
    return page.page(
        [_provider_response(provider, counts.get(provider.id, 0)) for provider in providers],
        total,
    )


@router.post("/providers", response_model=ProviderResponse, status_code=status.HTTP_201_CREATED)
async def create_provider(
    payload: ProviderCreateRequest,
    admin: AdminUserDep,
    session: SessionDep,
    secrets: SecretsDep,
) -> ProviderResponse:
    """Add an inference endpoint.

    No model points at it yet, so creating one changes nothing observable until
    a model is created against it or repointed.
    """
    provider = Provider(
        name=payload.name,
        description=payload.description,
        base_url=payload.base_url.rstrip("/"),
        extra_headers=payload.extra_headers,
        is_active=payload.is_active,
        forward_stream_options=payload.forward_stream_options,
        auth_scheme=AuthScheme(payload.auth_scheme),
        upstream_cost_unit=payload.upstream_cost_unit,
    )
    if payload.api_key is not None:
        _store_api_key(provider, secrets, payload.api_key.get_secret_value())

    session.add(provider)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(f"A provider named {payload.name!r} already exists.") from exc
    await session.refresh(provider)
    return _provider_response(provider, 0)


@router.patch("/providers/{provider_id}", response_model=ProviderResponse)
async def update_provider(
    provider_id: uuid.UUID,
    payload: ProviderUpdateRequest,
    admin: AdminUserDep,
    session: SessionDep,
    secrets: SecretsDep,
    providers: ProvidersDep,
) -> ProviderResponse:
    """Change a provider.

    ``name`` is editable, which it was not. The argument against was that
    people refer to a provider by name and a rename makes past mentions wrong —
    true, and outweighed by the case that actually happens: it was mistyped
    when it was created. There is no way out of that otherwise, because a
    provider serving any model refuses to be deleted, so the typo is permanent
    and appears in the console and in ``owned_by`` on every ``/v1/models`` card
    it serves.

    Nothing references the name as a key: models point at the row by id, and
    ``usage_records`` stores the provider the *upstream* reported, not this
    name, so renaming leaves historical spend intact and correctly attributed.

    The three-way ``api_key`` convention matters here — omitted keeps the stored
    credential, a value replaces it, an empty string clears it. A two-way
    optional field cannot express "remove the key".
    """
    provider = await _load_provider(session, provider_id)
    fields = payload.model_dump(exclude_unset=True)

    if "api_key" in fields:
        secret = payload.api_key.get_secret_value() if payload.api_key is not None else ""
        _store_api_key(provider, secrets, secret)
    fields.pop("api_key", None)

    if (base_url := fields.pop("base_url", None)) is not None:
        provider.base_url = base_url.rstrip("/")
    if (scheme := fields.pop("auth_scheme", None)) is not None:
        provider.auth_scheme = AuthScheme(scheme)
    for field, value in fields.items():
        setattr(provider, field, value)

    try:
        await session.commit()
    except IntegrityError as exc:
        # Names are unique, so a rename can collide the same way a create can,
        # and must answer the same way rather than with a 500.
        await session.rollback()
        raise ConflictError(f"A provider named {payload.name!r} already exists.") from exc
    await session.refresh(provider)

    # The cached client was built from the old values. Dropped rather than
    # rebuilt: the next request through this provider builds it, and doing it
    # here would pay the cost on an admin request that may never be followed by
    # any traffic.
    await providers.forget(provider.id)

    counts = await _model_counts(session)
    return _provider_response(provider, counts.get(provider.id, 0))


@router.delete("/providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(
    provider_id: uuid.UUID,
    admin: AdminUserDep,
    session: SessionDep,
    providers: ProvidersDep,
) -> None:
    """Remove a provider that nothing uses.

    Refused while any model points at it. The database would refuse anyway —
    the foreign key is ON DELETE RESTRICT — but a 409 naming the count is a far
    better answer than an integrity error, and cascading would leave historical
    spend attributed to a model that can no longer be explained.
    """
    await _load_provider(session, provider_id)
    counts = await _model_counts(session)
    if (count := counts.get(provider_id, 0)) > 0:
        raise ConflictError(
            f"{count} model(s) still use this provider. Repoint or remove them first, "
            "or deactivate the provider instead."
        )

    await session.execute(delete(Provider).where(Provider.id == provider_id))
    await session.commit()
    await providers.forget(provider_id)


@router.post("/providers/{provider_id}/test", response_model=ProviderTestResponse)
async def test_provider(
    provider_id: uuid.UUID,
    admin: AdminUserDep,
    session: SessionDep,
    providers: ProvidersDep,
) -> ProviderTestResponse:
    """Call the provider's ``/models`` and report what came back.

    Run against the row **as stored**, credential included, so it exercises
    exactly what a real request would send. A wrong base URL or a stale key
    should be found when it is entered, not by a user's request failing an hour
    later.

    Never raises for a provider-side failure: "it did not work, and here is why"
    is the useful answer, and an exception would make the console show a generic
    error instead of the detail.
    """
    provider = await _load_provider(session, provider_id)

    try:
        upstream, client = providers.build_probe(provider)
    except ProviderConfigurationError as exc:
        return ProviderTestResponse(ok=False, detail=str(exc))

    started = time.monotonic()
    try:
        result = await upstream.list_models()
    except Exception as exc:
        return ProviderTestResponse(
            ok=False,
            detail=f"could not reach {provider.base_url}: {exc}",
            latency_ms=int((time.monotonic() - started) * 1000),
        )
    finally:
        await client.aclose()

    latency = int((time.monotonic() - started) * 1000)
    if result.status_code >= 400:
        hint = ""
        if result.status_code in (401, 403):
            hint = " — check the API key"
        elif result.status_code == 404:
            hint = " — check the base URL includes the version path, e.g. /v1"
        return ProviderTestResponse(
            ok=False,
            status_code=result.status_code,
            detail=f"the provider answered {result.status_code}{hint}",
            latency_ms=latency,
        )

    ids = [
        entry.get("id")
        for entry in (result.payload or {}).get("data", [])
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    ]
    return ProviderTestResponse(
        ok=True,
        status_code=result.status_code,
        detail=f"reachable; the provider offers {len(ids)} model(s)",
        model_count=len(ids),
        sample=[entry for entry in ids[:5] if entry],
        latency_ms=latency,
    )


# -- models -----------------------------------------------------------------


@router.get("/models", response_model=Page[ModelAdminResponse])
async def list_models(
    admin: AdminUserDep,
    session: SessionDep,
    page: PageDep,
    include_inactive: bool = True,
    q: str = "",
    provider_id: uuid.UUID | None = None,
) -> Page[ModelAdminResponse]:
    """Every model, including inactive ones — an operator needs to see those.

    ``q`` matches the model's own name or the upstream one it maps to. It is a
    substring match, deliberately: an operator looking for a model half-knows
    its name, and a prefix match on a catalogue full of
    ``meta-llama/Llama-3.3-70B-Instruct`` finds nothing.
    """
    stmt = (
        select(ModelDef)
        .options(selectinload(ModelDef.prices), selectinload(ModelDef.provider))
        .order_by(ModelDef.name)
    )
    if not include_inactive:
        stmt = stmt.where(ModelDef.is_active.is_(True))
    if provider_id is not None:
        stmt = stmt.where(ModelDef.provider_id == provider_id)
    if needle := q.strip():
        stmt = stmt.where(_matches(needle, ModelDef.name, ModelDef.upstream_model))

    total = await count_of(session, stmt)
    models = (await session.execute(page.apply(stmt))).scalars().all()
    ids = [model.id for model in models]
    grants = await _grants_by_model(session, ids)
    user_grants = await _user_grants_by_model(session, ids)
    return page.page(
        [
            _model_response(
                model, sorted(grants.get(model.id, [])), sorted(user_grants.get(model.id, []))
            )
            for model in models
        ],
        total,
    )


@router.post("/models", response_model=ModelAdminResponse, status_code=status.HTTP_201_CREATED)
async def create_model(
    payload: ModelCreateRequest, admin: AdminUserDep, session: SessionDep
) -> ModelAdminResponse:
    """Add a model to the catalogue.

    It is invisible to callers until a group is granted access: absence of a
    ``group_model_access`` row means no access, with no global allow-all.
    """
    # Checked before insert so the failure is "no such provider" rather than a
    # foreign-key violation.
    await _load_provider(session, payload.provider_id)

    model = ModelDef(
        name=payload.name,
        upstream_model=payload.upstream_model,
        provider_id=payload.provider_id,
        kind=ModelKind(payload.kind),
        display_name=payload.display_name,
        description=payload.description,
        context_window=payload.context_window,
        max_output_tokens=payload.max_output_tokens,
        input_modalities=list(payload.input_modalities),
        output_modalities=list(payload.output_modalities),
        supported_features=list(payload.supported_features),
        is_active=payload.is_active,
    )
    session.add(model)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(f"A model named {payload.name!r} already exists.") from exc
    await session.refresh(model, attribute_names=["prices", "provider"])
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
    fields = payload.model_dump(exclude_unset=True)
    if (provider_id := fields.get("provider_id")) is not None:
        # Repointing at another endpoint is allowed and is how a migration off a
        # provider happens; pointing at one that does not exist is not.
        await _load_provider(session, provider_id)
    if (kind := fields.pop("kind", None)) is not None:
        model.kind = ModelKind(kind)
    for field, value in fields.items():
        setattr(model, field, value)
    await session.commit()
    await session.refresh(model, attribute_names=["prices", "provider"])
    grants = await _grants_by_model(session, [model.id])
    user_grants = await _user_grants_by_model(session, [model.id])
    return _model_response(
        model, sorted(grants.get(model.id, [])), sorted(user_grants.get(model.id, []))
    )


# -- discovery ---------------------------------------------------------------


def _suggested_name(upstream_model: str) -> str:
    """Strip a provider prefix: ``openai/gpt-4o-mini`` -> ``gpt-4o-mini``.

    Only a suggestion; the client-facing name is ours to choose and the import
    request can override it.
    """
    return upstream_model.rsplit("/", 1)[-1] or upstream_model


async def _catalogue_source(
    session: SessionDep, secrets: SecretBox, provider_id: uuid.UUID, url: str | None
) -> tuple[Provider, str, str | None]:
    """Where to fetch a catalogue from, and with which credential.

    Per provider since ADR 0027: "what does the provider offer" is only a
    meaningful question about a specific one. `url` overrides the endpoint for a
    provider whose catalogue lives somewhere other than `{base_url}/models`.
    """
    provider = await _load_provider(session, provider_id)
    catalogue_url = url or f"{provider.base_url}/models"
    api_key: str | None = None
    if provider.api_key_encrypted:
        try:
            api_key = secrets.decrypt(provider.api_key_encrypted)
        except Exception as exc:
            raise BadRequestError(
                f"provider {provider.name!r}: {exc}", code="provider_key_unreadable"
            ) from exc
    return provider, catalogue_url, api_key


@router.get("/models/discover", response_model=CatalogueDiscoveryResponse)
async def discover_models(
    admin: AdminUserDep,
    session: SessionDep,
    settings: SettingsDep,
    secrets: SecretsDep,
    http: ControlHttpDep,
    provider_id: uuid.UUID,
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
    provider, catalogue_url, api_key = await _catalogue_source(session, secrets, provider_id, url)

    try:
        payload = await fetch_catalogue(http, catalogue_url, api_key)
    except CatalogueUnavailable as exc:
        raise UpstreamUnavailableError(f"Could not read the provider catalogue: {exc}") from exc

    prices, unparsable = parse_catalogue(payload)
    by_upstream = {price.model_id: price for price in prices}

    # Only this provider's models. A model served by another provider is not
    # "missing upstream" here — it was never expected to be.
    ours = (
        (
            await session.execute(
                select(ModelDef).where(ModelDef.provider_id == provider.id).order_by(ModelDef.name)
            )
        )
        .scalars()
        .all()
    )
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
                kind=price.kind.value,
                input_modalities=list(price.input_modalities),
                output_modalities=list(price.output_modalities),
                supported_features=list(price.supported_features),
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
    secrets: SecretsDep,
    http: ControlHttpDep,
    provider_id: uuid.UUID,
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
    provider, catalogue_url, api_key = await _catalogue_source(session, secrets, provider_id, url)

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
            provider_id=provider.id,
            context_window=price.context_window,
            # From the catalogue, so an imported embedding model lands on the
            # right route without anyone editing it afterwards (ADR 0028).
            kind=price.kind,
            input_modalities=list(price.input_modalities),
            output_modalities=list(price.output_modalities),
            supported_features=list(price.supported_features),
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


@router.get("/models/{model_id}/prices", response_model=Page[PriceResponse])
async def list_prices(
    model_id: uuid.UUID, admin: AdminUserDep, session: SessionDep, page: PageDep
) -> Page[PriceResponse]:
    """Price history, newest first. Nothing here is ever mutated.

    Sliced in Python rather than in SQL: the rows are already loaded with the
    model, and a price history is short. The shape matches the other listings
    so a client does not have to care which.
    """
    model = await _load_model(session, model_id)
    ordered = sorted(model.prices, key=lambda price: price.effective_from, reverse=True)
    return page.slice([response for price in ordered if (response := _price_response(price))])


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
        per_image=payload.per_image,
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


@router.get("/groups", response_model=Page[GroupAdminResponse])
async def list_groups(
    admin: AdminUserDep, session: SessionDep, page: PageDep, q: str = ""
) -> Page[GroupAdminResponse]:
    stmt = select(Group).order_by(Group.name)
    if needle := q.strip():
        stmt = stmt.where(_matches(needle, Group.name, Group.description))
    total = await count_of(session, stmt)
    groups = (await session.execute(page.apply(stmt))).scalars().all()
    ids = [group.id for group in groups]

    counts: dict[uuid.UUID, int] = _pairs(
        (
            await session.execute(
                select(Membership.group_id, func.count(Membership.user_id))
                .where(Membership.group_id.in_(ids))
                .group_by(Membership.group_id)
            )
        ).all()
    )
    access_rows = (
        await session.execute(
            select(GroupModelAccess.group_id, ModelDef.name)
            .join(ModelDef, ModelDef.id == GroupModelAccess.model_id)
            .where(GroupModelAccess.group_id.in_(ids))
        )
    ).all()
    models: dict[uuid.UUID, list[str]] = {}
    for group_id, model_name in access_rows:
        models.setdefault(group_id, []).append(model_name)

    return page.page(
        [
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
        ],
        total,
    )


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


@router.put("/users/{user_id}/models/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
async def grant_user_model_access(
    user_id: uuid.UUID, model_id: uuid.UUID, admin: AdminUserDep, session: SessionDep
) -> None:
    """Let one person use a model, over and above their groups. Idempotent.

    Access is the **union** of group and personal grants (ADR 0027), so this adds
    and never subtracts: it cannot be used to take away something a group grants.
    """
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise NotFoundError(f"No user with id {user_id}.")
    await _load_model(session, model_id)

    existing = (
        await session.execute(
            select(UserModelAccess).where(
                UserModelAccess.user_id == user_id,
                UserModelAccess.model_id == model_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(UserModelAccess(user_id=user_id, model_id=model_id))
        await session.commit()


@router.delete("/users/{user_id}/models/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_user_model_access(
    user_id: uuid.UUID, model_id: uuid.UUID, admin: AdminUserDep, session: SessionDep
) -> None:
    """Remove a personal grant. Idempotent.

    The person may still reach the model through a group: this removes the
    personal grant only, because there is no such thing as a denial here.
    """
    await session.execute(
        delete(UserModelAccess).where(
            UserModelAccess.user_id == user_id,
            UserModelAccess.model_id == model_id,
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


@router.get("/limits", response_model=Page[LimitRuleResponse])
async def list_limits(
    admin: AdminUserDep, session: SessionDep, quota: QuotaDep, page: PageDep
) -> Page[LimitRuleResponse]:
    """Every rule, with how much of it is used right now.

    ``current_value`` comes from the live counters, so it already accounts for any
    reset. It is absent — not zero — when the counter store cannot be reached.

    Paginated after the counters are read, not before: the same rules are
    already loaded whole to evaluate a request, so reading a page of them from
    the database would save nothing the request path does not pay anyway.
    """
    rules = await _load_rules(session)
    current = await quota.current_values(rules)
    return page.slice([_limit_response(rule, current.get(rule.id)) for rule in rules])


@router.post("/limits", response_model=LimitRuleResponse, status_code=status.HTTP_201_CREATED)
async def create_limit(
    payload: LimitRuleCreateRequest, admin: AdminUserDep, session: SessionDep, quota: QuotaDep
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
    # Start the counter from the spend already in the ledger for this window. A
    # monthly rule created on the 20th otherwise begins at zero and the cap is
    # wrong for the rest of the month.
    await quota.seed_rule(rule)
    current = await quota.current_values([rule])
    return _limit_response(rule, current.get(rule.id))


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


@router.get("/limits/{rule_id}/resets", response_model=Page[QuotaResetResponse])
async def list_resets(
    rule_id: uuid.UUID, admin: AdminUserDep, session: SessionDep, page: PageDep
) -> Page[QuotaResetResponse]:
    """Who zeroed this rule, when, and why. Newest first.

    This one grows with time rather than with the organisation — nothing prunes
    it, by design, since it is the audit trail for overriding a budget.
    """
    await _load_rule(session, rule_id)
    stmt = (
        select(QuotaReset, User.email)
        .outerjoin(User, User.id == QuotaReset.created_by)
        .where(QuotaReset.rule_id == rule_id)
        .order_by(QuotaReset.effective_at.desc())
    )
    total = await count_of(session, stmt)
    rows = (await session.execute(page.apply(stmt))).all()
    return page.page(
        [
            QuotaResetResponse(
                id=reset.id,
                rule_id=reset.rule_id,
                effective_at=reset.effective_at,
                reason=reset.reason,
                created_by=reset.created_by,
                # Null once the account is erased. The reset row itself survives,
                # which is why created_by is ON DELETE SET NULL, not CASCADE.
                created_by_email=email,
            )
            for reset, email in rows
        ],
        total,
    )


@router.delete("/limits/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_limit(rule_id: uuid.UUID, admin: AdminUserDep, session: SessionDep) -> None:
    """Remove a rule outright.

    Safe to delete, unlike a model or a key: nothing in the ledger references a
    rule. Counters for it simply stop being read.
    """
    await session.execute(delete(LimitRule).where(LimitRule.id == rule_id))
    await session.commit()


# -- users ------------------------------------------------------------------


async def _user_responses(session: SessionDep, users: Sequence[User]) -> list[UserAdminResponse]:
    """Decorate user rows with the counts and names the console shows.

    Both lookups are restricted to the users being rendered. Reading every key
    and every group to render fifty rows is the sort of thing that is invisible
    until an organisation is large, which is the case pagination exists for.
    """
    ids = [user.id for user in users]
    key_counts: dict[uuid.UUID, int] = _pairs(
        (
            await session.execute(
                select(ApiKey.user_id, func.count(ApiKey.id))
                .where(ApiKey.revoked_at.is_(None), ApiKey.user_id.in_(ids))
                .group_by(ApiKey.user_id)
            )
        ).all()
    )
    billing_ids = [user.default_billing_group_id for user in users if user.default_billing_group_id]
    group_names: dict[uuid.UUID, str] = _pairs(
        (await session.execute(select(Group.id, Group.name).where(Group.id.in_(billing_ids)))).all()
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
            default_billing_group=(
                group_names.get(user.default_billing_group_id)
                if user.default_billing_group_id
                else None
            ),
            active_key_count=int(key_counts.get(user.id, 0)),
            last_login_at=user.last_login_at,
        )
        for user in users
    ]


@router.get("/users", response_model=Page[UserAdminResponse])
async def list_users(
    admin: AdminUserDep,
    session: SessionDep,
    page: PageDep,
    q: str = "",
    is_active: bool | None = None,
) -> Page[UserAdminResponse]:
    """Accounts, by email.

    ``q`` matches email, display name or the identity provider's subject. The
    subject is in there because that is all there is to search on for an
    account whose IdP does not release an email claim.
    """
    stmt = (
        select(User)
        .options(selectinload(User.memberships).selectinload(Membership.group))
        .order_by(User.email)
    )
    if needle := q.strip():
        stmt = stmt.where(_matches(needle, User.email, User.display_name, User.subject))
    if is_active is not None:
        stmt = stmt.where(User.is_active.is_(is_active))

    total = await count_of(session, stmt)
    users = (await session.execute(page.apply(stmt))).scalars().all()
    return page.page(await _user_responses(session, users), total)


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

    # Not by re-reading the listing and picking a row out of it: the listing is
    # a page now, and the user just edited may not be on the page.
    return (await _user_responses(session, [user]))[0]


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


# -- redaction ---------------------------------------------------------------
#
# Read-only, deliberately. Redaction is process configuration read at startup
# (ADR 0012), so there is nothing here an operator could change even if the
# route allowed it — see docs/redaction-scoping-plan.md for what making it
# configurable requires. Until then the console can at least answer "is it on,
# which engine, and is it answering", which it previously could not.


def _sanitised_endpoint(endpoint: str) -> str | None:
    """The detection endpoint, without any credential embedded in it.

    Nothing puts one there today, but a URL is exactly the kind of setting that
    grows a `user:pass@` when somebody puts the service behind basic auth, and
    this response is rendered in a browser.
    """
    if not endpoint:
        return None
    scheme, _, rest = endpoint.partition("://")
    if not rest:
        return endpoint
    _, at, host = rest.rpartition("@")
    return f"{scheme}://{host}" if at else endpoint


async def _redaction_service_health(
    http: Any, settings: Settings
) -> RedactionServiceHealth | None:
    """Ask the detection service what it is and whether it is there.

    Returns ``None`` for an engine that has no service to ask — ``noop``, or an
    in-process plugin. Never raises: "it is not answering, and here is why" is
    the useful answer, and an exception would replace it with a generic error on
    a page whose whole job is to say what is wrong.
    """
    endpoint = settings.redaction.endpoint
    if settings.redaction.engine != "http" or not endpoint:
        return None

    started = time.monotonic()
    try:
        response = await http.get(
            f"{endpoint.rstrip('/')}/healthz",
            timeout=settings.redaction.timeout_seconds,
        )
        latency = int((time.monotonic() - started) * 1000)
        if response.status_code >= 400:
            return RedactionServiceHealth(
                reachable=False,
                detail=f"{endpoint} answered HTTP {response.status_code}",
                latency_ms=latency,
            )
        body = response.json()
    except Exception as exc:
        return RedactionServiceHealth(
            reachable=False,
            detail=f"could not reach {endpoint}: {exc}",
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    return RedactionServiceHealth(
        reachable=True,
        detail=str(body.get("status") or "ok"),
        latency_ms=latency,
        engine=body.get("engine"),
        engine_version=body.get("engine_version"),
        languages=[str(item) for item in body.get("languages") or []],
        models={str(k): str(v) for k, v in (body.get("models") or {}).items()},
        degraded_languages=[str(item) for item in body.get("degraded_languages") or []],
        entities=[str(item) for item in body.get("entities") or []],
    )


async def _redaction_activity(session: SessionDep, window_seconds: int) -> RedactionActivity:
    """What redaction has actually done recently.

    Counted from the ledger rather than from a metric, so it survives a restart
    and agrees with the reports. `redaction_engine` is written per request, which
    is what makes "one engine or two in this window" answerable at all.
    """
    since = utcnow() - timedelta(seconds=window_seconds)
    redacting = UsageRecord.redaction_engine.is_not(None) & (
        UsageRecord.redaction_engine != "noop"
    )
    row = (
        await session.execute(
            select(
                func.count(UsageRecord.id),
                func.coalesce(func.sum(case((redacting, 1), else_=0)), 0),
                func.coalesce(func.sum(UsageRecord.redacted_entity_count), 0),
            ).where(
                UsageRecord.created_at >= since,
                UsageRecord.status != UsageStatus.IN_PROGRESS,
            )
        )
    ).one()
    engines = (
        (
            await session.execute(
                select(UsageRecord.redaction_engine)
                .where(UsageRecord.created_at >= since, redacting)
                .group_by(UsageRecord.redaction_engine)
                .order_by(UsageRecord.redaction_engine)
            )
        )
        .scalars()
        .all()
    )
    return RedactionActivity(
        window_seconds=window_seconds,
        requests=int(row[0] or 0),
        requests_redacted=int(row[1] or 0),
        entities_redacted=int(row[2] or 0),
        engines=[name for name in engines if name],
    )


def _redaction_warnings(
    config: RedactionSettings, engine: str, service: RedactionServiceHealth | None
) -> list[str]:
    """What is wrong with this configuration, in words.

    The point of the screen. A configuration dump tells an operator what the
    settings say; these tell them the settings are not achieving what they look
    like they achieve — which is the failure mode of a redaction layer, because
    detecting nothing looks exactly like finding nothing to detect.
    """
    notes: list[str] = []

    if engine == "noop":
        notes.append(
            "Redaction is not enabled: the engine is 'noop', so prompts reach the provider "
            "exactly as the caller sent them."
        )
        return notes

    if config.fail_open:
        notes.append(
            "fail_open is on, so a detection failure forwards the prompt unredacted instead "
            "of refusing the request. A redaction layer that silently stops redacting is "
            "worse than an outage."
        )

    if service is not None and not service.reachable:
        notes.append(
            "The detection service is not answering. "
            + (
                "With fail_open off, every request needing redaction is being refused."
                if not config.fail_open
                else "With fail_open on, prompts are currently going upstream unredacted."
            )
        )

    if service is not None and service.reachable:
        if service.languages and config.language not in service.languages:
            notes.append(
                f"Configured for language '{config.language}', which the service does not "
                f"serve — it offers {', '.join(service.languages)}. Detection will find "
                "little or nothing."
            )
        elif config.language in service.degraded_languages:
            notes.append(
                f"'{config.language}' is served without a named-entity model, so fewer "
                "entities are found than for a fully supported language."
            )

        if config.entity_types and service.entities:
            unknown = sorted(set(config.entity_types) - set(service.entities))
            if unknown:
                notes.append(
                    "These entity types are configured but the service does not detect them, "
                    f"so they are silently ignored: {', '.join(unknown)}."
                )

    if not config.restore_in_response:
        notes.append(
            "restore_in_response is off, so callers receive placeholders rather than the "
            "original values. Deliberate for some deployments; surprising in most."
        )

    return notes


@router.get("/redaction", response_model=RedactionStatusResponse)
async def redaction_status(
    admin: AdminUserDep,
    session: SessionDep,
    settings: SettingsDep,
    http: ControlHttpDep,
    request: Request,
    window_seconds: int = 86_400,
) -> RedactionStatusResponse:
    """The redaction layer as this worker is running it."""
    if not 60 <= window_seconds <= 366 * 86_400:
        raise BadRequestError("window_seconds must be between 60 and one year.")

    config = settings.redaction
    service = await _redaction_service_health(http, settings)
    # From the constructed redactor, not the setting: if the two ever disagreed,
    # reporting the setting would describe a deployment that does not exist.
    live: Redactor | None = getattr(request.app.state, "redactor", None)
    engine = getattr(live, "name", None) or config.engine

    return RedactionStatusResponse(
        engine=engine,
        enabled=engine != "noop",
        endpoint=_sanitised_endpoint(config.endpoint),
        installed_engines=redaction_registry.available(),
        fail_open=config.fail_open,
        restore_in_response=config.restore_in_response,
        language=config.language,
        score_threshold=config.score_threshold,
        entity_types=list(config.entity_types) if config.entity_types else None,
        timeout_seconds=config.timeout_seconds,
        cache_size=config.cache_size,
        placeholder_key_set=bool(config.placeholder_key.get_secret_value()),
        service=service,
        activity=await _redaction_activity(session, window_seconds),
        warnings=_redaction_warnings(config, engine, service),
    )
