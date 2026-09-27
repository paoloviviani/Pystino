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

import contextlib
import logging
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import PlainTextResponse
from llmp_shared import EntitySpan, PlaceholderMap
from sqlalchemy import ColumnElement, Row, case, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, selectinload

from gateway import identity_policy
from gateway.accounting.cost import select_price
from gateway.config import EffectivePolicy, RedactionPolicy, RedactionSettings, Settings
from gateway.deployment_state import mark_bootstrap_consumed
from gateway.deps import (
    AdminUserDep,
    ControlHttpDep,
    ProvidersDep,
    QuotaDep,
    SecretsDep,
    SessionDep,
    SettingsDep,
)
from gateway.directory.authelia_users import (
    UsersFileError,
    UsersFileLockedError,
)
from gateway.directory.engine import add_manual_memberships, ensure_bundled_default_group
from gateway.directory.service import bundled_users_file
from gateway.email_config import effective_smtp
from gateway.email_normalize import is_trusted_email
from gateway.erasure import attempt_erasure, preview_chat_erasure, queue_erasure
from gateway.errors import (
    BadRequestError,
    ContentBlockedError,
    GatewayError,
    ServiceUnavailableError,
    UpstreamUnavailableError,
)
from gateway.identity_events import record_event
from gateway.identity_registry import (
    active_bundled_provider,
)
from gateway.identity_registry import (
    list_providers as list_provider_records,
)
from gateway.mail import MailDeliveryError, send_mail_async
from gateway.merge import (
    MERGE_RULES,
    MergeNotFound,
    MergeRefused,
    MergeRuleKind,
    compute_merge_preview,
    disable_dropped_bundled_login,
    merge_users,
)
from gateway.models import (
    ApiKey,
    Base,
    BillingMode,
    ChatErasure,
    ChatErasureStatus,
    DirectoryEntry,
    Group,
    GroupModelAccess,
    GroupSource,
    IdentityEventAction,
    IdentityEventActor,
    IdentityProvider,
    IdpAuthorizationCode,
    LimitMetric,
    LimitRule,
    LimitScope,
    LocalCredential,
    Membership,
    MembershipSource,
    ModelDef,
    ModelKind,
    ModelPrice,
    OIDCPolicyConfig,
    PriceSource,
    Provider,
    ProviderKind,
    QuotaReset,
    RedactionConfig,
    RedactionRule,
    RedactionScope,
    RefreshCredential,
    UsageRecord,
    UsageSource,
    UsageStatus,
    User,
    UserIdentity,
    UserModelAccess,
)
from gateway.oidc import PENDING_USER_ISSUER, other_active_admin_exists
from gateway.oidc_policy import environment_policy
from gateway.pagination import Page, PageDep, count_of
from gateway.periods import PeriodKind
from gateway.plugins import registry as plugin_registry
from gateway.pricing import (
    ANONYMOUS_UNPARSABLE,
    LITELLM_CATALOGUE_URL,
    CataloguePrice,
    CatalogueUnavailable,
    fetch_catalogue,
    kinds_by_id,
    parse_catalogue,
    parse_litellm_catalogue,
    parse_openrouter_catalogue,
    tags_in_payload,
)
from gateway.pricing import (
    # Aliased: the two routes below take a `fill_missing_prices` query parameter,
    # and a function of the same name in the module scope would read as if the
    # parameter were calling itself.
    fill_missing_prices as fill_prices_from_community,
)
from gateway.providers import ProviderConfigurationError
from gateway.redaction import Redactor
from gateway.redaction import registry as redaction_registry
from gateway.redaction.http import apply_spans
from gateway.redaction.resolver import RedactionResolver, build_for, current_engine
from gateway.reporting import (
    GroupBy,
    GroupByParam,
    ReportFilter,
    build_report,
    report_to_csv,
    resolve_period,
)
from gateway.schemas import (
    BundledUserCreatedResponse,
    BundledUserCreateRequest,
    CatalogueDiscoveryResponse,
    CatalogueDriftRow,
    CatalogueTagsResponse,
    ChatSharedResource,
    DeletePreviewResponse,
    DeleteUserRequest,
    DeleteUserResponse,
    DiscoveredModel,
    EmailSettingsResponse,
    EmailTestRequest,
    EmailTestResponse,
    GroupAdminResponse,
    GroupCreateRequest,
    GroupMemberAddRequest,
    GroupSearchBackendRequest,
    GroupUsageRow,
    IdentityProviderResponse,
    IdentityRef,
    LimitRuleCreateRequest,
    LimitRuleResetRequest,
    LimitRuleResponse,
    LimitRuleUpdateRequest,
    MergePreviewResponse,
    MergeRequest,
    MergeResponse,
    ModelAdminResponse,
    ModelCreateRequest,
    ModelImportRequest,
    ModelImportResponse,
    ModelImportResult,
    ModelUpdateRequest,
    OidcMappingRule,
    OidcPolicyChange,
    OidcPolicyResponse,
    OidcPolicyUpdateRequest,
    PasswordResetResponse,
    PendingErasuresResponse,
    PriceCreateRequest,
    PriceResponse,
    ProviderCreateRequest,
    ProviderPluginResponse,
    ProviderResponse,
    ProviderTestResponse,
    ProviderUpdateRequest,
    QuotaResetResponse,
    RedactionActivity,
    RedactionConfigChange,
    RedactionEngineOption,
    RedactionEngineRequest,
    RedactionPreviewRequest,
    RedactionPreviewResponse,
    RedactionPreviewSpan,
    RedactionRuleCreateRequest,
    RedactionRuleResponse,
    RedactionRuleUpdateRequest,
    RedactionScopeName,
    RedactionServiceHealth,
    RedactionStatusResponse,
    SearchBackendDeleteResponse,
    SignInCreateRequest,
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


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["administration"])


class ConflictError(GatewayError):
    status_code = status.HTTP_409_CONFLICT
    error_type = "invalid_request_error"
    code = "already_exists"


class LastAdminError(GatewayError):
    """ADR 0093 §5.5: refuses whatever would leave zero active administrators,
    self included. Distinct from `ConflictError`'s `already_exists` so a
    console can tell "this address is taken" from "this would lock everyone
    out" without parsing the message."""

    status_code = status.HTTP_409_CONFLICT
    error_type = "invalid_request_error"
    code = "last_admin"


class NotFoundError(GatewayError):
    status_code = status.HTTP_404_NOT_FOUND
    error_type = "invalid_request_error"
    code = "not_found"


async def _unpriced_counts(session: SessionDep) -> dict[uuid.UUID, int]:
    """Active models with no price row, per provider.

    An unpriced model bills zero and, more importantly, *reserves* zero — so a
    cost ceiling never trips for it. That is a hole in any billing mode, and a
    sharp one in `provider_reported`, where the counterparty's figure arrives too
    late to admit on.

    Models behind an internal provider are excluded, deliberately: their
    unpricedness is the design, not an oversight — the deployment's own
    extractor charges nothing because there is no counterparty to pay, and
    `plugins/extractor.py` says so at source. Warning about it would put the
    one chip on the providers screen that looks exactly like a billing hole
    while being the opposite, which is how the row came to be noticed as
    clutter in the first place. `_model_counts` stays complete on purpose: it
    is the blast radius of deactivating or deleting a provider, and the
    extractor's single model is blast radius all the same.
    """
    stmt = (
        select(ModelDef.provider_id, func.count(ModelDef.id))
        .join(Provider, Provider.id == ModelDef.provider_id)
        .outerjoin(ModelPrice, ModelPrice.model_id == ModelDef.id)
        .where(
            ModelDef.is_active.is_(True),
            ModelPrice.id.is_(None),
            Provider.kind != ProviderKind.INTERNAL,
        )
        .group_by(ModelDef.provider_id)
    )
    return _pairs((await session.execute(stmt)).all())


async def _unpriced_count(session: SessionDep, provider_id: uuid.UUID) -> int:
    return (await _unpriced_counts(session)).get(provider_id, 0)


def _plugin_kind(name: str | None) -> str | None:
    """What the named plugin says it is, or None if it is not installed.

    Reported alongside the configured `kind` so a mismatch is visible: naming
    the Cortecs plugin while leaving `kind` as `provider` is a configuration a
    console can point at rather than a silent inconsistency.
    """
    try:
        return str(plugin_registry.resolve(name).kind.value)
    except plugin_registry.UnknownPluginError:
        return None


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
        per_page=price.per_page,
        per_search=price.per_search,
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
        # Same fact, second use: screens that present models as choices read
        # the kind to leave infrastructure out of the catalogue, exactly as
        # `kind` lets them set search tiers aside.
        provider_kind=model.provider.kind.value,
        kind=model.kind.value,
        display_name=model.display_name,
        description=model.description,
        is_active=model.is_active,
        is_public=model.is_public,
        context_window=model.context_window,
        max_output_tokens=model.max_output_tokens,
        max_input_tokens=model.max_input_tokens,
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


def _provider_response(provider: Provider, model_count: int, unpriced: int = 0) -> ProviderResponse:
    return ProviderResponse(
        id=provider.id,
        name=provider.name,
        description=provider.description,
        base_url=provider.base_url,
        api_key_hint=provider.api_key_hint,
        has_api_key=bool(provider.api_key_encrypted),
        extra_headers=dict(provider.extra_headers or {}),
        is_active=provider.is_active,
        plugin=provider.plugin,
        kind=provider.kind.value,
        billing_mode=provider.billing_mode.value,
        prefix=provider.prefix,
        unpriced_model_count=unpriced,
        plugin_kind=_plugin_kind(provider.plugin),
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
    unpriced = await _unpriced_counts(session)
    return page.page(
        [
            _provider_response(provider, counts.get(provider.id, 0), unpriced.get(provider.id, 0))
            for provider in providers
        ],
        total,
    )


@router.get("/provider-plugins", response_model=list[ProviderPluginResponse])
async def list_provider_plugins(admin: AdminUserDep) -> list[ProviderPluginResponse]:
    """The provider types this deployment can offer.

    Read from the registry, so installing a plugin makes it selectable without a
    console release. Not paginated: this is a handful of installed packages, not
    a listing that grows with use.
    """
    return [ProviderPluginResponse.model_validate(entry) for entry in plugin_registry.describe()]


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

    The endpoint may be omitted when the chosen plugin knows it (Cortecs does);
    an operator re-typing a URL the plugin already knows is a typo with extra
    steps. A type with no default — the generic OpenAI-compatible one, whose
    endpoints range from a cloud API to a laptop's Ollama — still requires one.
    """
    base_url = payload.base_url
    if base_url is None:
        plugin = plugin_registry.resolve(payload.plugin)
        base_url = getattr(plugin, "default_base_url", None)
        if base_url is None:
            raise BadRequestError(
                f"The {plugin.label!r} type has no default endpoint — give the base URL."
            )

    provider = Provider(
        name=payload.name,
        description=payload.description,
        base_url=base_url.rstrip("/"),
        extra_headers=payload.extra_headers,
        is_active=payload.is_active,
        plugin=payload.plugin,
        kind=ProviderKind(payload.kind),
        billing_mode=BillingMode(payload.billing_mode),
        prefix=payload.prefix,
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

    # A search backend gets its grant anchor here: one model row, named after
    # the backend, that the access grants hang on and that /v1/search resolves
    # (ADR 0071). Tiers are body fields now, not models, so there is nothing
    # to import — the backend exists, therefore it may be granted.
    if provider.kind == ProviderKind.SEARCH:
        session.add(
            ModelDef(
                name=provider.name,
                upstream_model="search",
                provider_id=provider.id,
                kind=ModelKind.SEARCH,
                input_modalities=["text"],
                output_modalities=["text"],
                supported_features=[],
            )
        )
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            # The name comes from the payload, not the row: the rollback just
            # expired every loaded instance, and an f-string that refreshed
            # one would raise DetachedInstanceError from inside its own error
            # handler.
            raise ConflictError(
                f"A model named {payload.name!r} already exists, so the "
                "backend's grant anchor could not be created."
            ) from exc

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

    # The prefix is not a stored-and-forgotten field: whatever it now is, the
    # provider's models must match it. A prefix that only reached future
    # imports would leave the catalogue half-disambiguated — some rows named
    # `deep`, some `linkup-deep` — and an operator reading that list cannot
    # tell which rule is in force. Setting or changing renames what is here
    # (old prefix off, new prefix on); clearing strips it. A target name that
    # is taken refuses the whole rename, because half a rename is the one
    # outcome worse than the old names: it would be neither the catalogue the
    # operator asked for nor the one they had.
    if "prefix" in fields:
        new_prefix = fields.pop("prefix") or ""
        models = (
            (await session.execute(select(ModelDef).where(ModelDef.provider_id == provider.id)))
            .scalars()
            .all()
        )
        renames: list[tuple[ModelDef, str]] = []
        for model in models:
            stripped = model.name
            if provider.prefix and model.name.startswith(provider.prefix):
                stripped = model.name[len(provider.prefix) :]
            renames.append((model, f"{new_prefix}{stripped}"))
        others = (await session.execute(select(ModelDef))).scalars().all()
        taken = {model.name for model in others if model.provider_id != provider.id}
        collisions = sorted({target for _, target in renames if target in taken})
        if collisions:
            raise BadRequestError(
                "Renaming with this prefix would collide with models of other "
                f"providers: {', '.join(collisions)}. Choose a different prefix "
                "or free those names."
            )
        # Longest first. A prefix that grows the names ("deep" ->
        # "linkup-deep") would otherwise collide with the sibling that holds
        # "linkup-deep" until it moves itself — and the shrink direction needs
        # the same order, the long name stepping down before the short one
        # claims its old place. Names are unique, so this ordering is total.
        # Flushed per row, because the unit of work does not preserve list
        # order: a single commit may emit the UPDATEs in any order it likes,
        # and a unique constraint is checked the moment a row is written.
        renames.sort(key=lambda pair: len(pair[0].name), reverse=True)
        for model, target in renames:
            model.name = target
            await session.flush()
        provider.prefix = new_prefix

    if (base_url := fields.pop("base_url", None)) is not None:
        provider.base_url = base_url.rstrip("/")
    if (kind := fields.pop("kind", None)) is not None:
        provider.kind = ProviderKind(kind)
    if (mode := fields.pop("billing_mode", None)) is not None:
        provider.billing_mode = BillingMode(mode)
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
    return _provider_response(
        provider, counts.get(provider.id, 0), await _unpriced_count(session, provider.id)
    )


@router.delete("/providers/{provider_id}", response_model=SearchBackendDeleteResponse)
async def delete_provider(
    provider_id: uuid.UUID,
    admin: AdminUserDep,
    session: SessionDep,
    providers: ProvidersDep,
) -> Response | SearchBackendDeleteResponse:
    """Remove a provider.

    For an inference provider, refused while any model points at it: the
    database would refuse anyway — the foreign key is ON DELETE RESTRICT — but
    a 409 naming the count is a far better answer than an integrity error, and
    cascading would leave historical spend attributed to a model that can no
    longer be explained.

    For a **search backend** the answer is the opposite, deliberately (ADR
    0071): a backend and its tiers are one concept on the search screen, and a
    half-deleted backend — tiers orphaned, policies pointing at a row that no
    longer resolves — is worse than an atomic delete. So in one transaction the
    tiers die with the backend (prices and access grants among them, exactly as
    ``delete_model`` does it; the usage ledger is conspicuously untouched),
    groups whose unified-search policy named one of those tiers have the policy
    cleared — named in the response, so the console can say so rather than
    letting the administrator discover it as a 404 later — and the provider row
    goes last. Historical spend stays readable the way a model delete leaves
    it: ``usage_records.model_id`` is ON DELETE SET NULL and every row carries
    the denormalised name.
    """
    provider = await _load_provider(session, provider_id)

    if provider.kind != ProviderKind.SEARCH:
        counts = await _model_counts(session)
        if (count := counts.get(provider_id, 0)) > 0:
            raise ConflictError(
                f"{count} model(s) still use this provider. Repoint or remove them, or "
                "deactivate the provider instead."
            )

        await session.execute(delete(Provider).where(Provider.id == provider_id))
        await session.commit()
        await providers.forget(provider_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # On a search backend every model row is a tier — the grant anchor and its
    # depth variants (ADR 0071) — so "the provider's models" and "the tiers"
    # coincide here, which is what lets the delete be total.
    tier_rows = (
        await session.execute(
            select(ModelDef.id, ModelDef.name).where(ModelDef.provider_id == provider_id)
        )
    ).all()
    tier_ids = [row.id for row in tier_rows]
    # Annotated for mypy: an inline conditional's bare-list branch gives strict
    # mode nothing to infer a row type from.
    pointed: Sequence[Row[tuple[uuid.UUID, str]]] = []
    if tier_ids:
        pointed = (
            await session.execute(
                select(Group.id, Group.name).where(Group.search_model_id.in_(tier_ids))
            )
        ).all()
    cleared = sorted(name for (_group_id, name) in pointed)

    # The cascade, in one transaction: the policy first (it names a tier that is
    # about to stop existing), then the tiers' children, then the tiers, then
    # the backend. Any failure rolls the whole thing back — a deletion that
    # cleared the policy but kept the backend would be its own half-deleted
    # state, the exact shape this route exists to prevent.
    if pointed:
        await session.execute(
            update(Group)
            .where(Group.id.in_([group_id for (group_id, _name) in pointed]))
            .values(search_model_id=None)
        )
    if tier_ids:
        await session.execute(delete(ModelPrice).where(ModelPrice.model_id.in_(tier_ids)))
        await session.execute(
            delete(GroupModelAccess).where(GroupModelAccess.model_id.in_(tier_ids))
        )
        await session.execute(delete(UserModelAccess).where(UserModelAccess.model_id.in_(tier_ids)))
        await session.execute(delete(ModelDef).where(ModelDef.provider_id == provider_id))
    await session.execute(delete(Provider).where(Provider.id == provider_id))
    await session.commit()
    await providers.forget(provider_id)

    return SearchBackendDeleteResponse(tiers_deleted=len(tier_ids), cleared_groups=cleared)


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

    It is invisible to callers until a group is granted access or the model is
    marked public (ADR 0045): absence of a ``group_model_access`` row and a
    ``false`` ``is_public`` mean no access, with no other allow-all.
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
        max_input_tokens=payload.max_input_tokens,
        input_modalities=list(payload.input_modalities),
        output_modalities=list(payload.output_modalities),
        supported_features=list(payload.supported_features),
        is_active=payload.is_active,
        is_public=payload.is_public,
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


@router.delete("/models/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model(model_id: uuid.UUID, admin: AdminUserDep, session: SessionDep) -> None:
    """Remove a model from the catalogue outright, not merely deactivate it.

    Deactivation is the tool for "out of service"; it still shows the row, which
    is exactly what an operator pruning a long catalogue import wants gone.
    Delete is safe against the ledger by construction, not by caution:
    ``usage_records.model_id`` is ON DELETE SET NULL and every row carries the
    denormalised ``model_name``, so historical spend stays readable and its
    attribution to users and groups is untouched. Prices and access grants die
    with the model — they are meaningless without it.
    """
    await _load_model(session, model_id)
    # A model a search policy points at cannot go quietly either: the column
    # is ON DELETE SET NULL, which would silently unconfigure those groups'
    # unified search. Name them instead, so the administrator re-points or
    # clears the policies first.
    pointed = (
        await session.execute(select(Group.name).where(Group.search_model_id == model_id))
    ).all()
    if pointed:
        names = ", ".join(sorted(f"{name!r}" for (name,) in pointed))
        raise ConflictError(
            f"Groups {names} search through this backend. Point their search "
            "policies elsewhere, or clear them, before deleting it.",
        )
    # Explicit child deletes rather than ORM cascade: the same outcome either
    # way, but these name what goes, and the usage rows are conspicuously not
    # among them.
    await session.execute(delete(ModelPrice).where(ModelPrice.model_id == model_id))
    await session.execute(delete(GroupModelAccess).where(GroupModelAccess.model_id == model_id))
    await session.execute(delete(UserModelAccess).where(UserModelAccess.model_id == model_id))
    await session.execute(delete(ModelDef).where(ModelDef.id == model_id))
    await session.commit()


def _suggested_name(upstream_model: str) -> str:
    """Strip a provider prefix: ``openai/gpt-4o-mini`` -> ``gpt-4o-mini``.

    Only a suggestion; the client-facing name is ours to choose and the import
    request can override it.
    """
    return upstream_model.rsplit("/", 1)[-1] or upstream_model


def _catalogue_parser(provider: Provider) -> Callable[[Any], tuple[list[Any], list[str]]]:
    """The catalogue parser this provider's plugin implies.

    The generic parser reads per-million prices; OpenRouter publishes
    per-token USD strings that the generic keys would silently misread as
    per-million — an error of exactly a million, in the dangerous direction.
    The plugin name is the routing signal because the plugin is where
    counterparty knowledge lives (ADR 0032).
    """
    if provider.plugin == "openrouter":
        return parse_openrouter_catalogue
    return parse_catalogue


async def _catalogue_source(
    session: SessionDep,
    secrets: SecretBox,
    provider_id: uuid.UUID,
    url: str | None,
    tag: str | None = None,
) -> tuple[Provider, str, str | None]:
    """Where to fetch a catalogue from, and with which credential.

    Per provider since ADR 0027: "what does the provider offer" is only a
    meaningful question about a specific one. `url` overrides the endpoint for a
    provider whose catalogue lives somewhere other than `{base_url}/models`.

    Always the provider's own endpoint. The community price file is a *filler*
    for prices the provider leaves out (`fill_missing_prices` below), never a
    source for the model list: the provider is the only authority on what it
    offers, and reading the list from a community file answered that question
    with a third party's opinion (ADR 0053).
    """
    provider = await _load_provider(session, provider_id)
    catalogue_url = url or f"{provider.base_url}/models"
    if tag:
        # Cortecs' catalogue **defaults to `tag=Instruct`**, so an unfiltered
        # request is a filtered one — which is how eleven embedding models and
        # three OCR models sat in that endpoint while this gateway reported
        # that it offered none. Passed through rather than interpreted: the
        # vocabulary is the counterparty's, and a fixed list here would go
        # stale the first time they add one.
        joiner = "&" if "?" in catalogue_url else "?"
        catalogue_url = f"{catalogue_url}{joiner}tag={quote(tag)}"
    api_key: str | None = None
    if provider.api_key_encrypted:
        try:
            api_key = secrets.decrypt(provider.api_key_encrypted)
        except Exception as exc:
            raise BadRequestError(
                f"provider {provider.name!r}: {exc}", code="provider_key_unreadable"
            ) from exc
    return provider, catalogue_url, api_key


async def _catalogue_with_prices(
    # `Any`, as `ControlHttpDep` itself is: the control-plane client is passed
    # through rather than constructed here.
    http: Any,
    provider: Provider,
    catalogue_url: str,
    api_key: str | None,
    *,
    fill_missing: bool,
) -> tuple[dict[str, CataloguePrice], list[str], set[str], dict[str, ModelKind]]:
    """The provider's catalogue, optionally with community prices in the gaps.

    Returns ``(prices by upstream id, unpriced ids, ids filled from the
    community file)``.

    Both fetches are the operator's explicit request, so a failure of either is
    reported rather than absorbed: ticking the fill and silently getting the
    unpriced rows back would look identical to a provider that publishes
    prices for nothing, and the operator would draw the wrong conclusion about
    their provider. Failing says which source could not be read.
    """
    # A plugin that *is* the thing being served knows what it offers, and
    # asking the network would mean asking a service with no such endpoint.
    # Pressing Discover on the local extractor used to answer "could not fetch
    # http://extractor:8080/models: 404" — an error naming a URL the operator
    # never typed, for a provider that cannot fail in that way.
    payload = plugin_registry.resolve(provider.plugin).builtin_catalogue()
    if payload is None:
        try:
            payload = await fetch_catalogue(http, catalogue_url, api_key)
        except CatalogueUnavailable as exc:
            raise UpstreamUnavailableError(f"Could not read the provider catalogue: {exc}") from exc

    published, unpriced = _catalogue_parser(provider)(payload)

    filled: set[str] = set()
    if fill_missing and unpriced:
        try:
            community_payload = await fetch_catalogue(http, LITELLM_CATALOGUE_URL, None)
        except CatalogueUnavailable as exc:
            raise UpstreamUnavailableError(
                f"Could not read the community price catalogue: {exc}"
            ) from exc
        # The provider row's plugin is the LiteLLM tag, so an Anthropic provider
        # is filled from Anthropic's rows and not from all 3,500.
        community, _ = parse_litellm_catalogue(
            community_payload, providers=[provider.plugin] if provider.plugin else None
        )
        prices, unpriced, filled = fill_prices_from_community(published, unpriced, community)
    else:
        prices = list(published)

    return {price.model_id: price for price in prices}, unpriced, filled, kinds_by_id(payload)


@router.get("/models/discover", response_model=CatalogueDiscoveryResponse)
async def discover_models(
    admin: AdminUserDep,
    session: SessionDep,
    settings: SettingsDep,
    secrets: SecretsDep,
    http: ControlHttpDep,
    provider_id: uuid.UUID,
    url: str | None = None,
    fill_missing_prices: bool = False,
    tag: str | None = None,
) -> CatalogueDiscoveryResponse:
    """Compare the provider's catalogue with ours.

    Nothing is created. The catalogue stays an allowlist an administrator curates
    — auto-adopting whatever a provider publishes would let their release notes
    silently change what users can spend money on. What this removes is the
    tedium of finding out *what* is on offer.

    Reports drift in both directions. Models we serve that the provider no longer
    offers are the more dangerous half: they keep appearing in ``/v1/models`` and
    fail only when someone calls them.

    ``fill_missing_prices`` consults the community file for models the provider
    listed and left unpriced (ADR 0053). Every row says which of the two
    supplied its figures, because an operator adopting them is entitled to know
    that before clicking Import, not afterwards.
    """
    provider, catalogue_url, api_key = await _catalogue_source(
        session, secrets, provider_id, url, tag
    )
    by_upstream, unpriced, filled, kinds = await _catalogue_with_prices(
        http, provider, catalogue_url, api_key, fill_missing=fill_missing_prices
    )

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

    available: list[DiscoveredModel] = []
    for upstream_id, price in sorted(by_upstream.items()):
        if upstream_id in our_upstream_ids:
            continue
        # ADR 0054 removed the currency block: a USD-priced model imports with
        # its USD prices and converts at admission.
        available.append(
            DiscoveredModel(
                upstream_model=upstream_id,
                suggested_name=f"{provider.prefix}{_suggested_name(upstream_id)}",
                input_per_mtok=price.input_per_mtok,
                output_per_mtok=price.output_per_mtok,
                per_page=price.per_page,
                currency=price.currency,
                context_window=price.context_window,
                # Deliberately not carried from the price candidate: the input
                # cap is the operator's claim about the upstream, and no
                # catalogue reports one — a discovered model is always imported
                # with this null, whatever the community file says.
                max_input_tokens=None,
                kind=price.kind.value,
                input_modalities=list(price.input_modalities),
                output_modalities=list(price.output_modalities),
                supported_features=list(price.supported_features),
                blocked_reason=None,
                price_source="community" if upstream_id in filled else "provider",
            )
        )

    # Listed by the provider, priced by nobody. Shown rather than hidden: with
    # the fill off, these *are* the answer to "what does this provider offer",
    # and hiding them is what made an unticked discovery against OpenAI look
    # like an endpoint with no models at all. A search tier is importable all
    # the same — its meter is a request count (`LimitMetric.OWN_SEARCH_REQUESTS`),
    # not a token count, so there is nothing to price and nothing to give away:
    # what it costs is bounded by request ceilings, not by a rate. Every other
    # unpriced kind still says so and stays refused — an unpriced chat model
    # serves happily and records a cost of zero, which is a quiet way to give
    # away money.
    for upstream_id in sorted(set(unpriced) - ANONYMOUS_UNPARSABLE):
        if upstream_id in our_upstream_ids or upstream_id in by_upstream:
            continue
        kind = kinds.get(upstream_id, ModelKind.CHAT)
        available.append(
            DiscoveredModel(
                upstream_model=upstream_id,
                suggested_name=f"{provider.prefix}{_suggested_name(upstream_id)}",
                input_per_mtok=None,
                output_per_mtok=None,
                currency=None,
                context_window=None,
                # What the catalogue says it is, even with no price to read:
                # an unpriced OCR model showed as `chat` because a row built
                # from an id alone has no kind and the default is chat.
                kind=kind.value,
                input_modalities=["text"] if kind == ModelKind.SEARCH else [],
                output_modalities=["text"] if kind == ModelKind.SEARCH else [],
                blocked_reason=None
                if kind == ModelKind.SEARCH
                else (
                    "the provider publishes no price for this model"
                    if fill_missing_prices
                    else "the provider publishes no price — tick “fill missing prices” or "
                    "add the model by hand"
                ),
                price_source=None,
            )
        )

    # Offered upstream means listed, priced or not: a model the provider still
    # serves but has stopped pricing is not "no longer offered", and calling it
    # that would send someone hunting for a withdrawal that never happened.
    #
    # Drift is a whole-catalogue question even when `available` above is
    # scoped to a tag: `tag=Instruct` narrows what is *importable*, not what
    # the provider still serves, so judging "missing" against the tag-scoped
    # fetch reported every embedding and OCR model this gateway serves as
    # withdrawn the moment an operator picked a tag that was not theirs — they
    # had not gone anywhere, they were filed under a tag nobody asked for.
    plugin = plugin_registry.resolve(provider.plugin)
    catalogue_tag_all = getattr(plugin, "catalogue_tag_all", None)
    sentinel = catalogue_tag_all() if callable(catalogue_tag_all) else None

    offered: set[str] | None
    if tag is None or tag == sentinel or plugin.builtin_catalogue() is not None:
        # No tag was asked for, the operator already asked for "everything"
        # directly, or the plugin answers from itself (which has no tags to
        # slice by at all): the fetch already made above is the whole
        # catalogue, so asking again would be the same request twice.
        offered = set(by_upstream) | (set(unpriced) - ANONYMOUS_UNPARSABLE)
    elif sentinel is None:
        # A free-text-tag provider: a tag-scoped fetch cannot tell "withdrawn"
        # from "filed under a tag it does not name here", so guessing would
        # only trade one false report (everything missing) for another (a
        # genuine withdrawal hidden while a tag is set). Reporting no drift
        # under a partial view is the honest answer — clearing the tag still
        # catches a real withdrawal.
        offered = None
    else:
        base_catalogue_url = url or f"{provider.base_url}/models"
        joiner = "&" if "?" in base_catalogue_url else "?"
        full_catalogue_url = f"{base_catalogue_url}{joiner}tag={quote(sentinel)}"
        full_by_upstream, full_unpriced, _, _ = await _catalogue_with_prices(
            http, provider, full_catalogue_url, api_key, fill_missing=False
        )
        offered = set(full_by_upstream) | (set(full_unpriced) - ANONYMOUS_UNPARSABLE)

    catalogued: list[CatalogueDriftRow] = []
    missing: list[CatalogueDriftRow] = []
    for model in ours:
        row = CatalogueDriftRow(
            id=model.id,
            name=model.name,
            upstream_model=model.upstream_model,
            is_active=model.is_active,
        )
        # `offered is None` is "cannot judge", not "cannot find" — see above;
        # it is not a missing-upstream row, it is a row this discovery run is
        # simply silent about.
        if offered is None or model.upstream_model in offered:
            catalogued.append(row)
        else:
            missing.append(row)

    return CatalogueDiscoveryResponse(
        # What was actually read. For a provider whose plugin answers from
        # itself, naming an endpoint nothing fetched is a small lie in a field
        # whose whole job is saying where the answer came from.
        provider_url=(
            catalogue_url if plugin.builtin_catalogue() is None else f"built in ({provider.plugin})"
        ),
        # The size of whichever set the drift judgement above was actually
        # checked against — the provider's whole catalogue when a tag is
        # active and `catalogue_tag_all` answered it, otherwise the same
        # tag-scoped fetch `available` was built from. Never the tag-scoped
        # count while claiming a whole-catalogue judgement was made.
        provider_model_count=(
            len(offered)
            if offered is not None
            else len(set(by_upstream) | (set(unpriced) - ANONYMOUS_UNPARSABLE))
        ),
        available=available,
        catalogued=catalogued,
        missing_upstream=missing,
        # Only the entries with no id at all remain genuinely unparsable; the
        # rest are now reported as rows an operator can see and act on.
        unparsable=[entry for entry in unpriced if entry in ANONYMOUS_UNPARSABLE],
    )


@router.get("/models/tags", response_model=CatalogueTagsResponse)
async def list_catalogue_tags(
    admin: AdminUserDep,
    session: SessionDep,
    secrets: SecretsDep,
    http: ControlHttpDep,
    provider_id: uuid.UUID,
) -> CatalogueTagsResponse:
    """The tags this provider slices its catalogue by, for a dropdown.

    Ask the counterparty live rather than compile a list here: the vocabulary
    is theirs, and whoever compiles owns the staleness — the whole reason the
    discovery dialog's tag field is per-provider at all. The fetch is an
    ``everything`` request named by the plugin (``catalogue_tag_all``), because
    the spelling of "everything" is measured counterparty knowledge: Cortecs
    answers ``tag=all`` with its whole catalogue and ``tag=All`` with an empty
    one, so a guessed spelling would report a vocabulary of none.

    ``supported: false`` for a provider whose plugin names no such fetch. The
    console keeps free text for those — a dropdown of one default option would
    be a worse version of the box it replaced.
    """
    provider = await _load_provider(session, provider_id)
    plugin = plugin_registry.resolve(provider.plugin)
    catalogue_url = f"{provider.base_url}/models"

    catalogue_tag_all = getattr(plugin, "catalogue_tag_all", None)
    sentinel = catalogue_tag_all() if callable(catalogue_tag_all) else None
    if sentinel is None:
        return CatalogueTagsResponse(provider_url=catalogue_url, supported=False, tags=[])

    catalogue_url = f"{catalogue_url}?tag={quote(sentinel)}"
    api_key: str | None = None
    if provider.api_key_encrypted:
        try:
            api_key = secrets.decrypt(provider.api_key_encrypted)
        except Exception as exc:
            raise BadRequestError(
                f"provider {provider.name!r}: {exc}", code="provider_key_unreadable"
            ) from exc

    # Same order as `_catalogue_with_prices`: a plugin that *is* the thing
    # being served answers from itself, and asking the network would mean
    # asking a service with no such endpoint.
    payload = plugin.builtin_catalogue()
    if payload is None:
        try:
            payload = await fetch_catalogue(http, catalogue_url, api_key)
        except CatalogueUnavailable as exc:
            raise UpstreamUnavailableError(f"Could not read the provider catalogue: {exc}") from exc
    return CatalogueTagsResponse(
        provider_url=catalogue_url, supported=True, tags=tags_in_payload(payload)
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
    fill_missing_prices: bool = False,
    tag: str | None = None,
) -> ModelImportResponse:
    """Adopt selected upstream models, with their published prices.

    Explicitly enumerated, never "import everything": the allowlist decision stays
    with a person, and this only removes the retyping.

    **Access is not granted.** A newly imported model is invisible until a group is
    granted it, because absence of a ``group_model_access`` row means no access and
    that invariant is worth more than one saved click.

    A model priced in another currency is **skipped entirely** rather than created
    without a price. An unpriced model serves happily and records a cost of zero,
    which is a quiet way to give away money — with one exception the meter, not
    the catalogue, decides: a **search tier** imports without a price, because
    its meter counts requests and no rate multiplies it. What bounds it is the
    request ceiling, granted per group like any other access.

    ``fill_missing_prices`` must match what the operator was shown: the price
    written here is stamped with who supplied it (`catalogue` or `community`),
    so the append-only history answers "where did this figure come from"
    without needing to remember which checkbox was ticked (ADR 0053).
    """
    provider, catalogue_url, api_key = await _catalogue_source(
        session, secrets, provider_id, url, tag
    )
    by_upstream, _unpriced, filled, _kinds = await _catalogue_with_prices(
        http, provider, catalogue_url, api_key, fill_missing=fill_missing_prices
    )

    existing = (await session.execute(select(ModelDef))).scalars().all()
    taken_names = {model.name for model in existing}
    taken_upstream = {model.upstream_model for model in existing}

    results: list[ModelImportResult] = []
    for item in payload.models:
        # The provider's prefix is what makes "deep" from two vendors two
        # models; the import is where it earns its keep.
        suggested = f"{provider.prefix}{_suggested_name(item.upstream_model)}"
        name = item.name or suggested

        # Duplicate checks run before any insert — an import that reached the
        # database's constraint instead of these reports was a 500 where the
        # response owed the operator a per-item reason. The upstream check
        # comes first (it names the resource rather than a clashing label),
        # and it is global: a priced model's upstream id is what the drift
        # reports key on. The name check is universal and last.
        price = by_upstream.get(item.upstream_model)
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

        if price is None:
            # Search backends no longer import tiers: the anchor row is created
            # with the provider, and a tier is a body field now (ADR 0071).
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
        # ADR 0054 removes the currency refusal the import used to carry: a
        # USD-priced model imports with its USD prices, and conversion happens
        # at admission and settle using the day's rate. What is still skipped
        # is a model with NO usable price at all — an unpriced model serves
        # happily and records a cost of zero, which is a quiet way to give
        # away money.
        if price.currency == "":
            results.append(
                ModelImportResult(
                    upstream_model=item.upstream_model,
                    name=name,
                    imported=False,
                    priced=False,
                    reason="the catalogue carries no usable price for this model",
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
        # Who supplied this figure, kept on the row itself. Until ADR 0053 every
        # catalogue import wrote `cortecs` regardless of provider or source, so a
        # community price for an OpenAI model was recorded as Cortecs' own.
        from_community = item.upstream_model in filled
        session.add(
            ModelPrice(
                model_id=model.id,
                input_per_mtok=price.input_per_mtok,
                output_per_mtok=price.output_per_mtok,
                cache_read_per_mtok=price.cache_read_per_mtok,
                cache_write_per_mtok=price.cache_write_per_mtok,
                # An OCR model's whole price is here; the token rates are zero.
                per_page=price.per_page,
                currency=price.currency,
                effective_from=utcnow(),
                source=PriceSource.COMMUNITY if from_community else PriceSource.CATALOGUE,
            )
        )
        taken_names.add(name)
        taken_upstream.add(item.upstream_model)
        results.append(
            ModelImportResult(
                upstream_model=item.upstream_model,
                name=name,
                imported=True,
                priced=True,
                price_source="community" if from_community else "provider",
            )
        )

    await session.commit()
    return ModelImportResponse(results=results)


# Declared here, after /models/discover and /models/import, and that placement is
# load-bearing: FastAPI matches in declaration order, so a `/models/{model_id}`
# above them would swallow both and answer a discovery request with "422: value
# is not a valid uuid" — a route that exists, reached by nobody.
@router.get("/models/{model_id}", response_model=ModelAdminResponse)
async def get_model(
    model_id: uuid.UUID, admin: AdminUserDep, session: SessionDep
) -> ModelAdminResponse:
    """One model, with its grants and current price.

    The listing carries the same shape, so this exists for the one thing a
    listing cannot serve: a deep link. The console's model page is addressable —
    a bookmark, a reload, a link pasted into a ticket — and searching the
    catalogue by name to rebuild a page whose id is right there would be both
    slower and wrong, because the name is editable and the id is not.
    """
    model = await _load_model(session, model_id)
    grants = await _grants_by_model(session, [model.id])
    user_grants = await _user_grants_by_model(session, [model.id])
    return _model_response(
        model, sorted(grants.get(model.id, [])), sorted(user_grants.get(model.id, []))
    )


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
        # Refused rather than converted: an exchange rate applied silently
        # produces invoices that look right and are wrong.
        raise BadRequestError(
            f"This gateway bills in {settings.billing_currency}, so a price in "
            f"{currency} would have to be converted. Convert it yourself, or change "
            "GATEWAY_BILLING_CURRENCY.",
            code="price_currency_mismatch",
        )

    price = ModelPrice(
        model_id=model.id,
        input_per_mtok=payload.input_per_mtok,
        output_per_mtok=payload.output_per_mtok,
        cache_read_per_mtok=payload.cache_read_per_mtok,
        cache_write_per_mtok=payload.cache_write_per_mtok,
        per_image=payload.per_image,
        per_page=payload.per_page,
        per_search=payload.per_search,
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

    backend_ids = {group.search_model_id for group in groups if group.search_model_id is not None}
    backends: dict[uuid.UUID, str] = {}
    if backend_ids:
        backends = _pairs(
            (
                await session.execute(
                    select(ModelDef.id, ModelDef.name).where(ModelDef.id.in_(backend_ids))
                )
            ).all()
        )

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
                search_backend=(
                    backends.get(group.search_model_id)
                    if group.search_model_id is not None
                    else None
                ),
            )
            for group in groups
        ],
        total,
    )


@router.post("/groups", response_model=GroupAdminResponse, status_code=status.HTTP_201_CREATED)
async def create_group(
    payload: GroupCreateRequest, admin: AdminUserDep, session: SessionDep
) -> GroupAdminResponse:
    """Create a manual group (ADR 0050).

    Manual is not a label of convenience: a manual group is the one kind whose
    membership this screen may edit, because nobody's identity provider will
    reconcile it away.
    """
    name = payload.name.strip()
    if not name:
        raise BadRequestError("A group needs a name.")
    existing = await session.execute(select(Group).where(Group.name == name))
    if existing.scalar_one_or_none() is not None:
        raise BadRequestError(f"A group named {name!r} already exists.", code="group_exists")
    group = Group(
        name=name,
        description=payload.description or None,
        source=GroupSource.MANUAL,
    )
    session.add(group)
    await session.commit()
    return GroupAdminResponse(
        id=group.id,
        name=group.name,
        description=group.description,
        source=group.source.value,
        is_active=group.is_active,
        member_count=0,
        models=[],
        search_backend=None,
    )


@router.delete("/groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(group_id: uuid.UUID, admin: AdminUserDep, session: SessionDep) -> None:
    """Delete a group.

    Model access rows die with it (cascade); members lose it, and anyone whose
    default billing group it was falls back to choosing at request time. A
    quota or redaction rule scoped to this group keeps its scope id and goes
    inert — the same trade deleting a user makes (ADR 0048). A group the IdP
    owns is *not* refused here, because refusing cannot stop the IdP from
    recreating it at the next login; the deletion lasts exactly as long as
    nobody from that group signs in, and the dialog says so.
    """
    group = (await session.execute(select(Group).where(Group.id == group_id))).scalar_one_or_none()
    if group is None:
        raise NotFoundError(f"No group with id {group_id}.")
    await session.delete(group)
    await session.commit()


async def _load_group_for_membership(group_id: uuid.UUID, session: SessionDep) -> Group:
    """The group a membership change is aimed at.

    This used to refuse an OIDC-sourced group, and the reason it gave was
    sound at the time: "a member added here would vanish at the next sign-in —
    access granted silently, then revoked the same way, with nobody the wiser
    for either". That premise is what ADR 0057 removed. A membership added
    here is recorded as an administrator's (``MembershipSource.MANUAL``) and no
    login touches it, so the refusal now prevents nothing and blocks the thing
    it was protecting: putting somebody into a directory's group when the
    directory does not name them.

    Removal is allowed on any membership for the same reason it always was —
    it takes effect immediately. A membership the directory granted may of
    course come back at that person's next sign-in, which is what the group
    listing's source badge is for.
    """
    group = (await session.execute(select(Group).where(Group.id == group_id))).scalar_one_or_none()
    if group is None:
        raise NotFoundError(f"No group with id {group_id}.")
    return group


@router.get("/groups/{group_id}/members", response_model=Page[UserAdminResponse])
async def list_group_members(
    group_id: uuid.UUID,
    admin: AdminUserDep,
    session: SessionDep,
    page: PageDep,
    q: str = "",
) -> Page[UserAdminResponse]:
    """The members of one group, searched by email, name or subject."""
    stmt = (
        select(User)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.group_id == group_id)
        .order_by(User.email)
    )
    if needle := q.strip():
        # `username` is in here because it is the name an administrator
        # created the account under, and searching for it was the reported
        # failure: an account made as `chat@local` in Keycloak was findable
        # only as `chat@example.org`, its email (ADR 0062 follow-up).
        stmt = stmt.where(
            _matches(needle, User.email, User.display_name, User.username, User.subject)
        )
    total = await count_of(session, stmt)
    users = (await session.execute(page.apply(stmt))).scalars().all()
    return page.page(await _user_responses(session, list(users), membership_in=group_id), total)


@router.post(
    "/groups/{group_id}/members",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def add_group_member(
    group_id: uuid.UUID,
    payload: GroupMemberAddRequest,
    admin: AdminUserDep,
    session: SessionDep,
) -> None:
    group = await _load_group_for_membership(group_id, session)
    user = await session.get(User, payload.user_id)
    if user is None:
        raise NotFoundError(f"No user with id {payload.user_id}.")

    existing = await session.execute(
        select(Membership).where(Membership.group_id == group_id, Membership.user_id == user.id)
    )
    if existing.scalar_one_or_none() is not None:
        raise BadRequestError(
            f"{user.email or user.subject} is already a member of {group.name!r}."
        )

    # A user's first group becomes their default billing group, mirroring the
    # same rule in provision_user (ADR 0057) and create_user — extended here to
    # the one path that lacked it. Read before `session.add` below: `user.memberships`
    # is `lazy="selectin"` and was loaded before this membership existed, so it
    # still answers "did they have any group before this one" even though the
    # new row is about to be added in the same session (ADR 0057's stale-collection
    # trap, sidestepped rather than hit).
    is_first_group = user.default_billing_group_id is None and not user.memberships

    # An administrator's grant, and recorded as one: no login will undo it,
    # even for a user the directory manages (ADR 0057).
    session.add(Membership(user_id=user.id, group_id=group.id, source=MembershipSource.MANUAL))
    if is_first_group:
        user.default_billing_group_id = group.id
    await session.commit()


@router.delete(
    "/groups/{group_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_group_member(
    group_id: uuid.UUID, user_id: uuid.UUID, admin: AdminUserDep, session: SessionDep
) -> None:
    group = await _load_group_for_membership(group_id, session)
    user = await session.get(User, user_id)
    if user is None:
        raise NotFoundError(f"No user with id {user_id}.")

    membership = (
        await session.execute(
            select(Membership).where(Membership.group_id == group_id, Membership.user_id == user.id)
        )
    ).scalar_one_or_none()
    if membership is None:
        raise NotFoundError(f"{user.email or user.subject} is not a member of {group.name!r}.")

    await session.delete(membership)
    # The default billing group is a promise to bill somewhere the user
    # belongs; a removed membership revokes the promise, and the login path's
    # own rule (clear first, adopt-after) is applied by hand here.
    if user.default_billing_group_id == group_id:
        user.default_billing_group_id = None
    await session.commit()


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
    # A grant that a search policy points at cannot go quietly: the group's
    # unified searches would start 404ing with no administrator action
    # recording why. Point the policy elsewhere — or clear it — first.
    policy = (
        await session.execute(
            select(Group.id, Group.name).where(
                Group.id == group_id, Group.search_model_id == model_id
            )
        )
    ).first()
    if policy is not None:
        raise ConflictError(
            f"Group {policy.name!r} searches through this backend. Point its "
            "search policy at another backend, or clear it, before revoking "
            "access.",
        )
    await session.execute(
        delete(GroupModelAccess).where(
            GroupModelAccess.group_id == group_id,
            GroupModelAccess.model_id == model_id,
        )
    )
    await session.commit()


@router.put("/groups/{group_id}/search-backend", status_code=status.HTTP_204_NO_CONTENT)
async def set_group_search_backend(
    group_id: uuid.UUID,
    payload: GroupSearchBackendRequest,
    admin: AdminUserDep,
    session: SessionDep,
) -> None:
    """Point a group's unified-search policy at a backend, or clear it.

    Setting requires the group to already be granted the backend: a policy
    the group may not use would 404 every unified search, which is a
    misconfiguration, not a permission. The grant is made on the Models
    screen first; this screen only chooses among granted backends.
    """
    group = (await session.execute(select(Group).where(Group.id == group_id))).scalar_one_or_none()
    if group is None:
        raise NotFoundError(f"No group with id {group_id}.")
    if payload.model_id is None:
        group.search_model_id = None
        await session.commit()
        return
    model = await _load_model(session, payload.model_id)
    if model.kind != ModelKind.SEARCH:
        raise BadRequestError(
            f"{model.name!r} is a {model.kind.value} model, not a search backend.",
            code="wrong_model_kind",
        )
    granted = (
        await session.execute(
            select(GroupModelAccess).where(
                GroupModelAccess.group_id == group_id,
                GroupModelAccess.model_id == model.id,
            )
        )
    ).scalar_one_or_none()
    if granted is None:
        raise ConflictError(
            f"Group {group.name!r} is not granted {model.name!r}. Grant the "
            "backend to the group on the Models screen first.",
        )
    group.search_model_id = model.id
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

#: The profile fields the console may write, and the only ones whose edits
#: are recorded in ``users.admin_edited_fields`` so sign-in stops re-asserting
#: them. ``issuer``/``subject`` are absent on purpose: they are the login
#: identity, and no route may re-key a person out from under their ledger.
_PROFILE_FIELDS = ("email", "display_name", "username")


async def _user_responses(
    session: SessionDep, users: Sequence[User], *, membership_in: uuid.UUID | None = None
) -> list[UserAdminResponse]:
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
    # One lookup for the whole page. "Can this person sign in without the IdP"
    # is asked on every row render, and a query per row is exactly the shape
    # test_query_counts.py exists to prevent.
    with_password: set[uuid.UUID] = {
        user_id
        for (user_id,) in (
            await session.execute(
                select(LocalCredential.user_id).where(LocalCredential.user_id.in_(ids))
            )
        ).all()
    }

    # One lookup for the page, same reason as the password set above: a query
    # per row is the shape test_query_counts.py exists to prevent.
    # Who granted the membership being listed, when it is a group being listed.
    # One query for the page, like the two above.
    granted_here: set[uuid.UUID] = set()
    if membership_in is not None:
        granted_here = {
            user_id
            for (user_id,) in (
                await session.execute(
                    select(Membership.user_id).where(
                        Membership.group_id == membership_in,
                        Membership.user_id.in_(ids),
                        Membership.source == MembershipSource.MANUAL,
                    )
                )
            ).all()
        }

    linked: dict[uuid.UUID, list[str]] = {}
    for user_id, issuer in (
        await session.execute(
            select(UserIdentity.user_id, UserIdentity.issuer)
            .where(UserIdentity.user_id.in_(ids))
            .order_by(UserIdentity.issuer)
        )
    ).all():
        linked.setdefault(user_id, []).append(issuer)

    return [
        UserAdminResponse(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            username=user.username,
            issuer=user.issuer,
            subject=user.subject,
            is_active=user.is_active,
            is_admin=user.is_admin,
            has_password=user.id in with_password,
            linked_identities=linked.get(user.id, []),
            membership_source=(
                None if membership_in is None else ("manual" if user.id in granted_here else "oidc")
            ),
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


async def _bundled_provider(session: AsyncSession) -> IdentityProvider:
    """The one enabled row, refusing anything but the bundled Authelia.

    There is no id to take here the way `directory.py`'s routes take one: an
    operator on any other kind gets a clear refusal rather than a 404 that
    reads like a typo'd id.
    """
    row = await active_bundled_provider(session)
    if row is None:
        raise BadRequestError(
            "This deployment's identity provider is not the bundled Authelia."
        )
    return row


@router.post(
    "/users", response_model=BundledUserCreatedResponse, status_code=status.HTTP_201_CREATED
)
async def create_user(
    payload: BundledUserCreateRequest,
    admin: AdminUserDep,
    session: SessionDep,
    secrets: SecretsDep,
) -> BundledUserCreatedResponse:
    """Add a person to the bundled directory (ADR 0093 §8.1/§8.2).

    In order: the Authelia entry (so its own validation and "already exists"
    refusal run before anything gateway-side is written), a **pending**
    gateway user (§3.1 — the real ``(issuer, subject)`` arrives at their
    first sign-in, via `bind_bundled_login`), the requested console groups as
    manual memberships, and a `directory_entries` row bound to that user
    *in advance* — the reverse of every other directory kind, where a row
    exists unbound until a login claims it. If any gateway-side step fails,
    the Authelia entry is removed again: a login with no account behind it
    is worse than the create simply having failed outright.
    """
    provider = await _bundled_provider(session)
    users_file = bundled_users_file(provider, secrets)

    try:
        _authelia_user, password = users_file.create(
            payload.login, payload.email, payload.display_name
        )
    except UsersFileLockedError as exc:
        raise ServiceUnavailableError(str(exc)) from exc
    except UsersFileError as exc:
        raise BadRequestError(str(exc), code="authelia_users") from exc

    try:
        normalized_email, _ = is_trusted_email(payload.email)
        user = User(
            issuer=PENDING_USER_ISSUER,
            subject=str(uuid.uuid4()),
            email=payload.email,
            email_normalized=normalized_email,
            display_name=payload.display_name or None,
            username=payload.login,
            # The administrator just set it; it must not revert at this
            # person's first login the way an ordinary directory claim would
            # (§3.1's own reasoning for the column, applied at creation
            # rather than at a later PATCH).
            admin_edited_fields=["email"],
        )
        session.add(user)
        await session.flush()
        # `add_manual_memberships` reads `user.memberships` synchronously; a
        # brand-new row has never loaded that relationship, and touching it
        # unloaded here would be an implicit lazy load outside the greenlet
        # context asyncpg/aiosqlite need for one — MissingGreenlet, not a
        # wrong answer, but a crash on every create.
        await session.refresh(user, attribute_names=["memberships"])

        await add_manual_memberships(session, user, payload.groups)
        # An operator who picked no groups still gets a person who can bill
        # something (ADR 0093 to-do item 1) — the same `users` group a
        # bundled sign-in's own token used to carry before the redesign made
        # groups console-authoritative.
        await ensure_bundled_default_group(session, user)

        session.add(
            DirectoryEntry(
                provider_id=provider.id,
                external_id=payload.login,
                username=payload.login,
                email=payload.email,
                user_id=user.id,
            )
        )
        await record_event(
            session,
            actor_type=IdentityEventActor.USER,
            actor_user_id=admin.id,
            actor_label=admin.email or "",
            action=IdentityEventAction.USER_CREATE,
            target_user_id=user.id,
            target_label=payload.email,
        )
        await record_event(
            session,
            actor_type=IdentityEventActor.USER,
            actor_user_id=admin.id,
            actor_label=admin.email or "",
            action=IdentityEventAction.LOGIN_CREATE,
            target_user_id=user.id,
            target_label=payload.login,
        )
        await session.commit()
    except IntegrityError as exc:
        # A directory_entries row already bound to this login (a second
        # admin racing the same name — the file's own "already exists" check
        # ran first and passed, so this is the gateway-side race that ran
        # after it) — same compensation, a clearer refusal than the bare
        # constraint message.
        await session.rollback()
        with contextlib.suppress(UsersFileError):
            users_file.delete(payload.login)
        raise ConflictError(f"{payload.login!r} already exists.") from exc
    except Exception:
        await session.rollback()
        # The file is in whatever state it was in before this route ran;
        # the gateway-side failure is the error worth raising, not a
        # cleanup that could not undo a create that never happened.
        with contextlib.suppress(UsersFileError):
            users_file.delete(payload.login)
        raise

    await session.refresh(user, attribute_names=["memberships"])
    response = (await _user_responses(session, [user]))[0]
    return BundledUserCreatedResponse(**response.model_dump(), password=password)


@router.post(
    "/users/{user_id}/sign-in",
    response_model=BundledUserCreatedResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_sign_in(
    user_id: uuid.UUID,
    payload: SignInCreateRequest,
    admin: AdminUserDep,
    session: SessionDep,
    secrets: SecretsDep,
) -> BundledUserCreatedResponse:
    """A bundled login for an existing gateway user who has none (§8.1):
    the after-a-switch and after-break-glass case, where the person already
    has a chat history and memberships under this gateway id, just no way to
    reach it while the bundled Authelia is what's configured.
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

    provider = await _bundled_provider(session)
    users_file = bundled_users_file(provider, secrets)

    email = user.email or ""
    try:
        _authelia_user, password = users_file.create(payload.login, email, user.display_name or "")
    except UsersFileLockedError as exc:
        raise ServiceUnavailableError(str(exc)) from exc
    except UsersFileError as exc:
        raise BadRequestError(str(exc), code="authelia_users") from exc

    try:
        session.add(
            DirectoryEntry(
                provider_id=provider.id,
                external_id=payload.login,
                username=payload.login,
                email=email,
                user_id=user.id,
            )
        )
        # This person may already have memberships from before a switch to
        # the bundled Authelia — or may be a break-glass pending user with
        # none at all; either way, this is one of the to-do's named call
        # sites, so the same gap-filler runs here too.
        await ensure_bundled_default_group(session, user)
        await record_event(
            session,
            actor_type=IdentityEventActor.USER,
            actor_user_id=admin.id,
            actor_label=admin.email or "",
            action=IdentityEventAction.LOGIN_CREATE,
            target_user_id=user.id,
            target_label=payload.login,
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        with contextlib.suppress(UsersFileError):
            users_file.delete(payload.login)
        raise ConflictError(f"{payload.login!r} already exists.") from exc
    except Exception:
        await session.rollback()
        with contextlib.suppress(UsersFileError):
            users_file.delete(payload.login)
        raise

    response = (await _user_responses(session, [user]))[0]
    return BundledUserCreatedResponse(**response.model_dump(), password=password)


@router.post("/users/{user_id}/reset-password", response_model=PasswordResetResponse)
async def reset_password(
    user_id: uuid.UUID,
    admin: AdminUserDep,
    session: SessionDep,
    secrets: SecretsDep,
) -> PasswordResetResponse:
    """Mint a fresh one-time password for the user's bundled login (§8.1).

    Works with or without SMTP configured, by decision (§8.4): the
    admin-issued reset is the only reset this deployment has without it, and
    stays available with it, since a lost password is not always a person
    who still has their mailbox.
    """
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise NotFoundError(f"No user with id {user_id}.")

    provider = await _bundled_provider(session)
    entry = (
        await session.execute(
            select(DirectoryEntry).where(
                DirectoryEntry.provider_id == provider.id, DirectoryEntry.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if entry is None:
        raise NotFoundError("This user has no bundled login to reset.")

    users_file = bundled_users_file(provider, secrets)
    try:
        password = users_file.reset_password(entry.external_id)
    except UsersFileLockedError as exc:
        raise ServiceUnavailableError(str(exc)) from exc
    except UsersFileError as exc:
        raise BadRequestError(str(exc), code="authelia_users") from exc

    await record_event(
        session,
        actor_type=IdentityEventActor.USER,
        actor_user_id=admin.id,
        actor_label=admin.email or "",
        action=IdentityEventAction.PASSWORD_RESET,
        target_user_id=user.id,
        target_label=user.email or "",
    )
    await session.commit()
    return PasswordResetResponse(password=password)


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
        # `username` is in here because it is the name an administrator
        # created the account under, and searching for it was the reported
        # failure: an account made as `chat@local` in Keycloak was findable
        # only as `chat@example.org`, its email (ADR 0062 follow-up).
        stmt = stmt.where(
            _matches(needle, User.email, User.display_name, User.username, User.subject)
        )
    if is_active is not None:
        stmt = stmt.where(User.is_active.is_(is_active))

    total = await count_of(session, stmt)
    users = (await session.execute(page.apply(stmt))).scalars().all()
    return page.page(await _user_responses(session, users), total)


async def _bundled_entry_for(
    session: AsyncSession, user: User
) -> tuple[IdentityProvider, DirectoryEntry] | None:
    """This user's entry under the *currently enabled* bundled provider, and
    that provider itself — never a stale entry left over from a provider row
    an IdP switch disabled (ADR 0093 §2 keeps every old row, never deletes
    it). Returned together, rather than making a caller re-fetch the
    provider it just implied, so there is no second query that could in
    principle answer differently and nothing to `assert` about it staying
    the same between the two.
    """
    provider = await active_bundled_provider(session)
    if provider is None:
        return None
    entry = (
        await session.execute(
            select(DirectoryEntry).where(
                DirectoryEntry.provider_id == provider.id, DirectoryEntry.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    return (provider, entry) if entry is not None else None


async def _revoke_sessions_and_credentials(
    session: AsyncSession, admin: User, user: User
) -> None:
    """ADR 0093 §9.1's gateway-side half of a disable: ends every session and
    credential this person already holds, not just requests from here on.

    Personal API keys (`minted_by IS NULL`) are the one thing kept: they are
    refused while the user is inactive (the existing check in `deps.py`),
    and re-enabling brings them back, which nothing minted here ever does —
    a session, an authorization code and a minted key are all short-lived by
    design, so there is no "bring it back" for any of them to mean.
    """
    user.sessions_valid_after = utcnow()
    await session.execute(delete(RefreshCredential).where(RefreshCredential.user_id == user.id))
    await session.execute(
        delete(IdpAuthorizationCode).where(IdpAuthorizationCode.user_id == user.id)
    )
    await session.execute(
        delete(ApiKey).where(ApiKey.user_id == user.id, ApiKey.minted_by.is_not(None))
    )
    await record_event(
        session,
        actor_type=IdentityEventActor.USER,
        actor_user_id=admin.id,
        actor_label=admin.email or "",
        action=IdentityEventAction.SESSIONS_REVOKE,
        target_user_id=user.id,
        target_label=user.email or "",
    )


async def _sync_bundled_login(
    session: AsyncSession, admin: User, secrets: SecretBox, user: User, *, disabled: bool
) -> str | None:
    """Sets Authelia's own `disabled` flag to match, auditing either
    outcome. Returns the failure reason, or `None` on success or on nothing
    bundled to sync (not itself a failure).

    Deliberately idempotent and side-effect-free when there is nothing to
    change: called on *every* PATCH that names `is_active`, whether or not
    the value actually changed this time, so a PATCH resending the same
    state is how a previous Authelia-side failure gets retried — the
    console's "run the action again" is this exact call, not a special
    retry path.
    """
    found = await _bundled_entry_for(session, user)
    if found is None:
        return None
    provider, entry = found
    users_file = bundled_users_file(provider, secrets)
    action = IdentityEventAction.LOGIN_DISABLE if disabled else IdentityEventAction.LOGIN_ENABLE
    try:
        users_file.update(entry.external_id, disabled=disabled)
    except UsersFileError as exc:
        # The gateway-side state (is_active, sessions_valid_after, the
        # deletes on a disable) already stands, committed or about to be by
        # this same request: `is_active` alone still refuses the person at
        # their next `/v1` call even if Authelia's own flag never gets set.
        # A login file the gateway cannot currently reach must not undo a
        # decision that has already happened — it only means Authelia's
        # side of it is out of sync until a retry (or the next PATCH)
        # reaches the file again.
        logger.warning(
            "could not set Authelia disabled=%s for %s: %s", disabled, entry.external_id, exc
        )
        await record_event(
            session,
            actor_type=IdentityEventActor.USER,
            actor_user_id=admin.id,
            actor_label=admin.email or "",
            action=action,
            target_user_id=user.id,
            target_label=entry.external_id,
            detail={"result": "failed"},
        )
        return str(exc)
    await record_event(
        session,
        actor_type=IdentityEventActor.USER,
        actor_user_id=admin.id,
        actor_label=admin.email or "",
        action=action,
        target_user_id=user.id,
        target_label=entry.external_id,
    )
    return None


@router.patch("/users/{user_id}", response_model=UserAdminResponse)
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdateRequest,
    admin: AdminUserDep,
    session: SessionDep,
    settings: SettingsDep,
    secrets: SecretsDep,
) -> UserAdminResponse:
    """Deactivate a user, make one an administrator, or edit their profile.

    **The console is authoritative for admin, for every account** (ADR 0069):
    the flag is written here and nothing strips it at the next login, because
    provisioning no longer derives it from any claim or group. The machinery
    this replaces — granting a manual membership of an admin group so a login
    would not undo the flag — existed only because login used to *own* the
    flag; with the directory out of the authorisation business the workaround
    has nothing left to work around, and a group named "administrators" stops
    being the thing that decides anything.

    **The console becomes authoritative for any profile field it writes.**
    ``email``, ``display_name`` and ``username`` used to be refresh-only: a
    login re-asserted whatever the token carried, so a correction an
    administrator made here reverted at that person's next sign-in. Each edit
    is now recorded in ``users.admin_edited_fields``, and provisioning skips a
    listed field — the directory keeps syncing everything the console has not
    touched, which is the same split ADR 0057 drew for memberships. Clearing a
    field records the edit too: "no display name" is a decision, and the
    directory disagreeing would undo it. For a *local* account none of this
    matters (the local door touches no profile field), but a local account a
    directory later adopts (ADR 0056) keeps its administrator's values for
    exactly this reason.

    A directory's revocation of *authentication* still lands — a disabled
    account is refused on every path — but its group claims never touch roles.
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
    # ADR 0093 §5.4: an invite is not a grant. A pending user (§3.1) has never
    # signed in, so nothing has verified they are who the console typed in —
    # the only way to become admin ahead of that is break-glass, which is a
    # host-side recovery, not this route.
    if fields.get("is_admin") and user.issuer == PENDING_USER_ISSUER:
        raise BadRequestError(
            "This account has not signed in yet. Grant admin after their first sign-in."
        )
    was_admin, was_active = user.is_admin, user.is_active
    currently_active_admin = was_admin and was_active and user.issuer != PENDING_USER_ISSUER
    loses_admin_or_active = fields.get("is_admin") is False or fields.get("is_active") is False
    # The guard (ADR 0093 §5.5), self included: it is checked before anything
    # is written, so a refused PATCH changes nothing at all, not "everything
    # except the one field that would have locked everyone out".
    if (
        currently_active_admin
        and loses_admin_or_active
        and not await other_active_admin_exists(session, excluding=user.id)
    ):
        await record_event(
            session,
            actor_type=IdentityEventActor.USER,
            actor_user_id=admin.id,
            actor_label=admin.email or "",
            action=IdentityEventAction.ADMIN_REFUSED_LAST,
            target_user_id=user.id,
            target_label=user.email or "",
            reason="would leave no active administrator",
        )
        await session.commit()
        raise LastAdminError(
            "This would leave no active administrator. Make another account an "
            "administrator first."
        )

    for field, value in fields.items():
        setattr(user, field, value)
    # Provenance (ADR 0088): what the console sets is the console's, so a
    # provider deciding admin by claim can never undo it — and a console
    # reactivation clears a directory deactivation.
    if "is_admin" in fields:
        user.admin_source = "manual"
    if "is_active" in fields:
        user.deactivated_by = None if fields["is_active"] else "manual"
    # A new list every time: SQLAlchemy does not see in-place mutation of a
    # JSON attribute, and an append the unit-of-work never flushes would make
    # the override exist only until the request ended — the edit would revert
    # at the next login exactly as if the column were not there.
    if edited := [field for field in _PROFILE_FIELDS if field in fields]:
        user.admin_edited_fields = list(dict.fromkeys([*(user.admin_edited_fields or []), *edited]))

    # The audit trail (ADR 0093 §3.1): recorded from what actually changed,
    # not from the request shape, so a PATCH that sets `is_admin` to the value
    # it already held writes nothing.
    if "is_admin" in fields and fields["is_admin"] != was_admin:
        admin_action = (
            IdentityEventAction.ADMIN_GRANT
            if fields["is_admin"]
            else IdentityEventAction.ADMIN_REVOKE
        )
        await record_event(
            session,
            actor_type=IdentityEventActor.USER,
            actor_user_id=admin.id,
            actor_label=admin.email or "",
            action=admin_action,
            target_user_id=user.id,
            target_label=user.email or "",
        )
        if fields["is_admin"]:
            await mark_bootstrap_consumed(session)
    authelia_sync_error: str | None = None
    if "is_active" in fields:
        target_active = fields["is_active"]
        if target_active != was_active:
            active_action = (
                IdentityEventAction.USER_ENABLE
                if target_active
                else IdentityEventAction.USER_DISABLE
            )
            await record_event(
                session,
                actor_type=IdentityEventActor.USER,
                actor_user_id=admin.id,
                actor_label=admin.email or "",
                action=active_action,
                target_user_id=user.id,
                target_label=user.email or "",
            )
            if not target_active:
                await _revoke_sessions_and_credentials(session, admin, user)
        # Attempted every time `is_active` is named, changed or not: this is
        # what makes a previous Authelia-side failure retryable by sending
        # the same PATCH again, with no other field having to change first.
        authelia_sync_error = await _sync_bundled_login(
            session, admin, secrets, user, disabled=not target_active
        )
    await session.commit()

    # Not by re-reading the listing and picking a row out of it: the listing is
    # a page now, and the user just edited may not be on the page.
    response = (await _user_responses(session, [user]))[0]
    if authelia_sync_error is not None:
        verb = "disabled" if fields["is_active"] is False else "enabled"
        response = response.model_copy(
            update={
                "authelia_sync": "failed",
                "authelia_sync_message": (
                    f"The account is {verb} here, but its Authelia login could not be "
                    f"updated: {authelia_sync_error}. Retry, or run the action again."
                ),
            }
        )
    return response


def _confirm_matches(user: User, confirm: str) -> bool:
    """§7.1: "confirm must equal the source's email, or its id when it has
    none." Compared exactly, not casefolded -- an operator copies this from
    the preview they are looking at, so there is no directory-address
    normalisation question here the way there is for an automatic match."""
    expected = user.email or str(user.id)
    return confirm == expected


@router.get("/users/{user_id}/merge-preview", response_model=MergePreviewResponse)
async def merge_preview(
    user_id: uuid.UUID, into: uuid.UUID, admin: AdminUserDep, session: SessionDep
) -> MergePreviewResponse:
    try:
        preview = await compute_merge_preview(
            session, source_id=user_id, target_id=into, actor_id=admin.id
        )
    except MergeNotFound as exc:
        raise NotFoundError(str(exc)) from exc
    except MergeRefused as exc:
        raise BadRequestError(str(exc)) from exc
    return MergePreviewResponse(
        source_id=preview.source_id,
        target_id=preview.target_id,
        counts=preview.counts,
        identities_moving=[
            IdentityRef(issuer=i.issuer, subject=i.subject) for i in preview.identities_moving
        ],
        identities_dropped=[
            IdentityRef(issuer=i.issuer, subject=i.subject) for i in preview.identities_dropped
        ],
        resulting_is_admin=preview.resulting_is_admin,
        bundled_logins_disabled=preview.bundled_logins_disabled,
        duplicate_rules_dropped=preview.duplicate_rules_dropped,
        chat_note=preview.chat_note,
    )


@router.post("/users/{user_id}/merge", response_model=MergeResponse)
async def merge_user(
    user_id: uuid.UUID,
    body: MergeRequest,
    admin: AdminUserDep,
    session: SessionDep,
    secrets: SecretsDep,
) -> MergeResponse:
    """Irreversible (ADR 0093 §7.1): moves every table `gateway.merge`
    knows about onto `body.into`, deletes `user_id`, and stamps the target's
    `merged_at`/`sessions_valid_after` so both people's console sessions end
    cleanly. The typed confirmation is checked against the source *before*
    the transaction starts, on the same row the preview read, so a stale
    preview (the source's email changed since) fails here rather than
    confirming the wrong person.
    """
    source = await session.get(User, user_id)
    if source is None:
        raise NotFoundError(f"No user with id {user_id}.")
    if not _confirm_matches(source, body.confirm):
        raise BadRequestError(
            "Type the source account's email (or its id, if it has none) to confirm."
        )

    try:
        summary = await merge_users(
            session,
            source_id=user_id,
            target_id=body.into,
            actor_id=admin.id,
            actor_label=admin.email or "",
            reason=body.reason,
        )
    except MergeNotFound as exc:
        await session.rollback()
        raise NotFoundError(str(exc)) from exc
    except MergeRefused as exc:
        await session.rollback()
        raise BadRequestError(str(exc)) from exc
    await session.commit()

    if summary.login_to_disable is not None:
        provider_id, external_id = summary.login_to_disable
        await disable_dropped_bundled_login(
            session,
            secrets,
            provider_id=provider_id,
            external_id=external_id,
            target_id=summary.target_id,
            actor_id=admin.id,
            actor_label=admin.email or "",
        )

    return MergeResponse(
        target_id=summary.target_id,
        counts=summary.counts,
        identities_dropped=[
            IdentityRef(issuer=i.issuer, subject=i.subject) for i in summary.identities_dropped
        ],
        bundled_logins_disabled=summary.bundled_logins_disabled,
        duplicate_rules_dropped=summary.duplicate_rules_dropped,
    )


#: A plain delete's own reading of the merge registry (ADR 0093 §9.2): every
#: table whose rule means "this row is this person's and disappears with
#: them" -- as opposed to `REASSIGN`/`SCOPE_ID_USER`, which describe a
#: *merge*'s "move it to someone else" and have nothing to say about a solo
#: delete, where rows simply survive, orphaned (`SET NULL`) or inert
#: (`scope_id` unresolvable). One registry, so a table added to it for the
#: merge guard is counted here too without a second list to keep in step.
_DELETE_REMOVES = frozenset(
    {
        MergeRuleKind.DELETE,
        MergeRuleKind.KEEP_TARGET_DELETE_SOURCE,
        MergeRuleKind.UNION_MEMBERSHIPS,
        MergeRuleKind.UNION_MODEL_ACCESS,
        MergeRuleKind.IDENTITIES,
    }
)


async def _gateway_delete_counts(session: AsyncSession, user_id: uuid.UUID) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rule in MERGE_RULES:
        if rule.kind not in _DELETE_REMOVES:
            continue
        table = Base.metadata.tables[rule.table]
        stmt = select(func.count()).select_from(table).where(table.c[rule.column] == user_id)
        counts[rule.table] = (await session.execute(stmt)).scalar_one()
    return counts


def _chat_shared_resources(chat_preview: dict[str, Any] | None) -> list[ChatSharedResource]:
    """The chat preview's own `shared` list, typed -- an unreachable chat (or
    one running before this list existed) reports none, which is exactly
    "nothing known to be shared" rather than a distinct case to handle."""
    raw = (chat_preview or {}).get("shared")
    if not isinstance(raw, list):
        return []
    resources: list[ChatSharedResource] = []
    for item in raw:
        if isinstance(item, dict):
            resources.append(ChatSharedResource(**item))
    return resources


async def _user_identities_for_erasure(
    session: AsyncSession, user: User
) -> list[dict[str, str]]:
    """Every ``(issuer, subject)`` naming this person, the primary pair
    included -- what the chat's own resolution needs, since an account it
    never finished keying to ``gatewayUserId`` is only findable this way."""
    linked = (
        await session.execute(select(UserIdentity).where(UserIdentity.user_id == user.id))
    ).scalars().all()
    return [{"issuer": user.issuer, "subject": user.subject}] + [
        {"issuer": row.issuer, "subject": row.subject} for row in linked
    ]


@router.get("/users/{user_id}/delete-preview", response_model=DeletePreviewResponse)
async def delete_preview(
    user_id: uuid.UUID,
    admin: AdminUserDep,
    session: SessionDep,
    settings: SettingsDep,
    http: ControlHttpDep,
) -> DeletePreviewResponse:
    user = await session.get(User, user_id)
    if user is None:
        raise NotFoundError(f"No user with id {user_id}.")

    gateway_counts = await _gateway_delete_counts(session, user.id)
    bundled = await _bundled_entry_for(session, user)
    identities = await _user_identities_for_erasure(session, user)

    chat_preview = await preview_chat_erasure(
        settings, http, gateway_user_id=user.id, identities=identities
    )
    chat_counts = chat_preview.get("counts") if chat_preview else None
    legacy = chat_preview.get("unattributed_legacy_shares") if chat_preview else 0
    shared = _chat_shared_resources(chat_preview)

    return DeletePreviewResponse(
        user_id=user.id,
        gateway_counts=gateway_counts,
        bundled_login=bundled[1].external_id if bundled else None,
        chat_counts=chat_counts if isinstance(chat_counts, dict) else None,
        chat_reachable=chat_preview is not None,
        shared=shared,
        shared_with_others=bool(shared) or bool(legacy),
        chat_unattributed_legacy_shares=int(legacy or 0),
    )


@router.delete("/users/{user_id}", response_model=DeleteUserResponse)
async def delete_user(
    user_id: uuid.UUID,
    admin: AdminUserDep,
    session: SessionDep,
    settings: SettingsDep,
    http: ControlHttpDep,
    secrets: SecretsDep,
    body: DeleteUserRequest = DeleteUserRequest(),
) -> DeleteUserResponse:
    """Delete an account everywhere (ADR 0093 §9.2, §9.3): the gateway's own
    rows in one transaction with the ``chat_erasures`` row that records the
    chat still owes an answer, then — after that commits — the bound
    Authelia login, then one inline try at the chat itself. Anything short
    of that inline try succeeding is not a failure of this request: the
    retry loop has the row, and it does not give up.

    What dies with the gateway row: keys, memberships, refresh credentials
    and the local password — all ``CASCADE``. What survives on purpose:

    * **The ledger.** ``usage_records.user_id`` is ``ON DELETE SET NULL``, so
      historical spend is never lost — the rows keep their amounts and groups,
      losing only this account's name in the per-user breakdown. The same
      trade deleting an API key makes, at account scale.
    * **Their rules.** Quota rules and redaction rules scoped to this user
      keep their ``scope_id`` and simply stop matching anyone. They render as
      inert in their listings, and deleting them is the administrator's next
      decision, not a side effect of this one.
    """
    if user_id == admin.id:
        raise BadRequestError(
            "You cannot delete the account you are signed in with. Sign in as "
            "another administrator first."
        )

    user = await session.get(User, user_id)
    if user is None:
        raise NotFoundError(f"No user with id {user_id}.")

    # The self-delete rule above already makes this unreachable — the caller
    # is themselves an active administrator and never the target, so deleting
    # one admin always leaves at least one. It is added anyway (ADR 0093
    # §5.5): the guard has one implementation, shared with PATCH, the CLI, and
    # the rule-driven revocations, rather than "this route doesn't need it".
    if (
        user.is_admin
        and user.is_active
        and user.issuer != PENDING_USER_ISSUER
        and not await other_active_admin_exists(session, excluding=user.id)
    ):
        await record_event(
            session,
            actor_type=IdentityEventActor.USER,
            actor_user_id=admin.id,
            actor_label=admin.email or "",
            action=IdentityEventAction.ADMIN_REFUSED_LAST,
            target_user_id=user.id,
            target_label=user.email or "",
            reason="would leave no active administrator",
        )
        await session.commit()
        raise LastAdminError(
            "This would leave no active administrator. Make another account an "
            "administrator first."
        )

    published = await _published_by(session, user_id)
    if published:
        # Not a block on erasure — that would be a compliance bug rather than a
        # safeguard — but a refusal that names what is in the way (ADR 0066).
        # Their bases and agents CASCADE, correctly, since they are that
        # person's content. A *published* one is different: an unbounded and
        # unknowable set of people rely on it, and removing it as a side effect
        # of an unrelated administrative act is how a deployment loses a corpus
        # nobody realised was one person's.
        #
        # Named rather than counted, deliberately. "2 resources" sends an
        # administrator hunting through a listing where somebody else's
        # published base appears under its own name with nothing marking it as
        # theirs.
        raise BadRequestError(
            "This account has published "
            + ", ".join(published)
            + " to everyone. Unpublish or transfer to the deployment first, then "
            "delete the account — erasing it now would take them away from "
            "everybody using them."
        )

    if not settings.chat.erasure_url or not settings.chat.erasure_token.get_secret_value():
        # A URL with no token is exactly as unusable as no URL at all: every
        # call would be refused with 401, forever, by design — so this is
        # the same refusal, not a distinct one, however the deployment ended
        # up in the state. cerea-deploy sets the URL unconditionally on any
        # preset that runs a local chat; the token is what an unconfigured
        # deployment (an .env from before `./configure` minted one) lacks.
        raise BadRequestError("this deployment's chat is not reachable from the gateway")

    identities = await _user_identities_for_erasure(session, user)
    chat_preview = await preview_chat_erasure(
        settings, http, gateway_user_id=user.id, identities=identities
    )
    shared = bool(_chat_shared_resources(chat_preview)) or bool(
        (chat_preview or {}).get("unattributed_legacy_shares")
    )
    if shared and not body.confirm_shared_loss:
        raise BadRequestError(
            "This account has content shared with others. Confirm you understand it will "
            "disappear for them (confirm_shared_loss) to proceed."
        )

    bundled = await _bundled_entry_for(session, user)

    await record_event(
        session,
        actor_type=IdentityEventActor.USER,
        actor_user_id=admin.id,
        actor_label=admin.email or "",
        action=IdentityEventAction.USER_DELETE,
        target_user_id=user.id,
        target_label=user.email or "",
    )
    await session.delete(user)
    erasure = await queue_erasure(session, gateway_user_id=user.id, identities=identities)
    await session.commit()

    if bundled is not None:
        provider, entry = bundled
        users_file = bundled_users_file(provider, secrets)
        try:
            users_file.delete(entry.external_id)
        except UsersFileError as exc:
            if "last user" in str(exc):
                with contextlib.suppress(UsersFileError):
                    users_file.update(entry.external_id, disabled=True)
            else:
                logger.warning(
                    "could not remove bundled login %r on delete: %s", entry.external_id, exc
                )

    done = await attempt_erasure(session, settings, http, erasure)
    return DeleteUserResponse(erasure_id=erasure.id, chat_erasure_done=done)


@router.get("/erasures/pending", response_model=PendingErasuresResponse)
async def pending_erasures(admin: AdminUserDep, session: SessionDep) -> PendingErasuresResponse:
    """The Users page's own banner (ADR 0093 §9.3): "N erasures waiting for
    the chat", shown while this is above zero."""
    count = await session.scalar(
        select(func.count())
        .select_from(ChatErasure)
        .where(ChatErasure.status == ChatErasureStatus.PENDING)
    )
    return PendingErasuresResponse(pending=count or 0)


async def _published_by(session: AsyncSession, user_id: uuid.UUID) -> list[str]:
    """Resources this account owns that are shared with everyone, by name.

    Only the `everyone` grants. A base shared with three *named* colleagues
    still cascades away: those people are identifiable and can be told, so only
    the unbounded case is refused. That line is where it is on purpose —
    widening this to "any share blocks erasure" would leave an administrator
    unable to complete an erasure request at all, which is the failure this
    guard must not become.
    """
    # The knowledge bases moved to the chat (ADR 0070), so no resource of
    # this gateway's is shared-with-everyone any more; a chat project's
    # shares live in the chat, which does its own erasure. The machinery is
    # kept because `chat_project` shares still pass through here, and an
    # `everyone` grant on one would belong in this answer.
    return []


# -- identity policy (ADR 0048) ------------------------------------------------


async def _oidc_policy_response(request: Request, session: SessionDep) -> OidcPolicyResponse:
    """The policy in force, plus the record of the newest decision."""
    resolver = getattr(request.app.state, "oidc_policy", None)
    policy = (
        resolver.policy
        if resolver is not None
        else environment_policy(request.app.state.settings.oidc)
    )
    stored = await _latest_oidc_config(session)
    changed_by: str | None = None
    if stored is not None and stored.created_by is not None:
        changed_by = await session.scalar(select(User.email).where(User.id == stored.created_by))
    return OidcPolicyResponse(
        auto_provision=policy.auto_provision,
        unknown_user_policy=policy.unknown_user_policy,
        groups_claim=policy.groups_claim,
        group_mappings=[
            OidcMappingRule(idp=idp, local=local) for idp, local in policy.group_mappings.items()
        ],
        source=policy.source,
        sources=policy.sources,
        configured=(
            OidcPolicyChange(
                reason=stored.reason,
                changed_at=stored.created_at,
                changed_by=changed_by,
            )
            if stored is not None
            else None
        ),
        propagation_seconds=(resolver.refresh_seconds if resolver is not None else 0.0),
    )


async def _latest_oidc_config(session: SessionDep) -> OIDCPolicyConfig | None:
    return (
        await session.execute(
            select(OIDCPolicyConfig).order_by(OIDCPolicyConfig.created_at.desc()).limit(1)
        )
    ).scalar_one_or_none()


@router.get("/oidc/policy", response_model=OidcPolicyResponse)
async def get_oidc_policy_view(
    admin: AdminUserDep, session: SessionDep, request: Request
) -> OidcPolicyResponse:
    """The identity policy in force, and the newest decision behind it."""
    return await _oidc_policy_response(request, session)


# -- email configuration (ADR 0051) -------------------------------------------


@router.get("/email", response_model=EmailSettingsResponse)
async def get_email_settings(
    admin: AdminUserDep, session: SessionDep, request: Request
) -> EmailSettingsResponse:
    """The mail configuration in force — the row's, or the environment's.

    Read-only (ADR 0093 §1, §14): mail is one setting now (``GATEWAY_SMTP__*``),
    so there is nothing here for an administrator to edit. The fold over an
    old console-set ``EmailSettings`` row stays, for a deployment upgrading
    from before this stage; nothing writes a new one.
    """
    effective = await effective_smtp(session, request.app.state.settings, request.app.state.secrets)
    return EmailSettingsResponse(
        host=effective.host,
        port=effective.port,
        username=effective.username,
        from_address=effective.from_address,
        has_password=bool(effective.password),
        source=effective.source,
        enabled=effective.enabled,
    )


@router.post("/email/test", response_model=EmailTestResponse)
async def test_email_settings(
    payload: EmailTestRequest, admin: AdminUserDep, session: SessionDep, request: Request
) -> EmailTestResponse:
    """Send a test email with the configuration in force, and say what happened.

    The point of this endpoint is the error: a wrong host, a refused TLS
    handshake or a rejected login is exactly the thing an operator needs to see
    *before* a person is sitting on a reset page wondering where the mail went.
    The message says who asked for it, so a test mail is never mistaken for a
    real one.
    """
    effective = await effective_smtp(session, request.app.state.settings, request.app.state.secrets)
    if not effective.usable:
        return EmailTestResponse(
            ok=False,
            detail="Email is not configured: set the SMTP host and a From address first.",
        )
    try:
        await send_mail_async(
            effective.to_smtp_settings(),
            payload.to.strip(),
            "Pystino — test email",
            "This is a test message from the Pystino gateway, sent from the "
            "Settings screen. If you are reading it, the mail configuration "
            "works.",
        )
    except MailDeliveryError as exc:
        return EmailTestResponse(ok=False, detail=str(exc))
    return EmailTestResponse(ok=True, detail="The message was handed to the mail server.")


# -- identity providers (ADR 0051) ---------------------------------------------


def _idp_response(record: Any, *, user_count: int = 0) -> IdentityProviderResponse:
    return IdentityProviderResponse(
        id=record.id,
        name=record.name,
        issuer=record.issuer,
        client_id=record.client_id,
        has_client_secret=bool(record.client_secret),
        scopes=list(record.scopes),
        groups_claim=record.groups_claim,
        fetch_userinfo=record.fetch_userinfo,
        group_mappings=[
            OidcMappingRule(idp=idp, local=local) for idp, local in record.group_mappings.items()
        ],
        link_by_email=record.link_by_email,
        group_sync=record.group_sync.value,
        is_enabled=record.is_enabled,
        source=record.source,
        internal_base_url=record.internal_base_url,
        logout_url=record.logout_url,
        default_logout_url=identity_policy.default_logout_url(record.kind, record.issuer),
        kind=record.kind,
        group_source=record.group_source,
        admin_source=record.admin_source,
        admin_claim=record.admin_claim,
        admin_values=list(record.admin_values),
        subject_claim=record.subject_claim,
        sync_adapter=record.sync_adapter,
        sync_interval_minutes=record.sync_interval_minutes,
        sync_deprovision=record.sync_deprovision,
        sync_create_users=record.sync_create_users,
        sync_confirmed=record.sync_confirmed,
        capabilities=identity_policy.capabilities(record.kind).as_dict(),
        user_count=user_count,
    )


@router.get("/identity-providers", response_model=list[IdentityProviderResponse])
async def list_identity_providers(
    admin: AdminUserDep, session: SessionDep, request: Request
) -> list[IdentityProviderResponse]:
    """Every configured identity provider, rows and any environment fallback.

    Read-only (ADR 0093 §14): the provider row is a projection of the
    environment now, re-seeded at every start, so there is nothing here for an
    administrator to create, edit or delete. ``./configure`` is where this
    changes.
    """
    settings: Settings = request.app.state.settings
    records = await list_provider_records(
        session, settings, request.app.state.secrets, enabled_only=False
    )
    by_issuer = select(User.issuer, func.count(User.id)).group_by(User.issuer)
    counts: dict[str, int] = dict((await session.execute(by_issuer)).tuples().all())
    return [_idp_response(record, user_count=counts.get(record.issuer, 0)) for record in records]


@router.put("/oidc/policy", response_model=OidcPolicyResponse)
async def set_oidc_policy(
    payload: OidcPolicyUpdateRequest,
    admin: AdminUserDep,
    session: SessionDep,
    request: Request,
    settings: SettingsDep,
) -> OidcPolicyResponse:
    """Record a new identity-policy decision.

    Append-only, newest row wins, and every unset field stores ``null`` —
    "the environment decides this one" — so a change to one knob never has to
    know the deployment's other answers. The change reaches every worker
    within the poll interval; the answering worker refreshes immediately so a
    console that saves and re-reads sees its own decision.
    """
    fields = payload.model_dump(exclude_unset=True)

    unknown = fields.get("unknown_user_policy")
    if unknown is not None and unknown not in ("refuse", "create_inactive"):
        raise BadRequestError("unknown_user_policy must be 'refuse' or 'create_inactive'.")
    if unknown is not None:
        # Refused rather than stored-never-applied: a knob that does nothing
        # because of another knob's value reads as a bug, not as a policy. The
        # check is against the policy that would be in force *after* this row
        # — per-field saves are the normal case, so "the previous decision
        # already turned provisioning off" is a valid state, not an error.
        policy_in_force = getattr(request.app.state, "oidc_policy", None)
        default_after = (
            policy_in_force.policy.auto_provision if policy_in_force is not None else True
        )
        auto_after = fields.get("auto_provision", default_after)
        if auto_after:
            raise BadRequestError(
                "unknown_user_policy only applies when automatic provisioning "
                "is off. Turn provisioning off, or drop the unknown-user policy."
            )

    groups_claim = fields.get("groups_claim")
    if groups_claim is not None and not str(groups_claim).strip():
        raise BadRequestError("groups_claim must name a claim (e.g. 'groups').")

    mappings: list[list[str]] | None = None
    if payload.group_mappings is not None:
        mappings = []
        seen_idp: set[str] = set()
        for rule in payload.group_mappings:
            idp, local = rule.idp.strip(), rule.local.strip()
            if not idp or not local:
                raise BadRequestError("Mapping rules need both an IdP name and a local name.")
            if idp in seen_idp:
                # Two rows naming the same IdP group cannot both apply; storing
                # one silently would make the form disagree with the policy.
                raise BadRequestError(f"Group '{idp}' is mapped more than once.")
            seen_idp.add(idp)
            mappings.append([idp, local])

    row = OIDCPolicyConfig(
        auto_provision=fields.get("auto_provision"),
        unknown_user_policy=unknown,
        groups_claim=groups_claim,
        group_mappings=mappings,
        reason=payload.reason,
        created_by=admin.id,
    )
    session.add(row)
    await session.commit()

    # This worker picks the decision up now; the others within the poll
    # interval. Best effort: a failed refresh leaves the poll to catch up.
    resolver = getattr(request.app.state, "oidc_policy", None)
    if resolver is not None:
        try:
            await resolver.refresh_once()
        except Exception:
            # The ten-second poll catches up; a failed refresh here must not
            # turn a saved decision into an error the console reports.
            logger.warning("oidc policy refresh after save failed", exc_info=True)
    return await _oidc_policy_response(request, session)


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
        metering_enabled=settings.accounting.enabled,
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


def _engine_redacts(engine: str) -> bool:
    """Whether the named engine removes anything at all.

    Asked of the registry rather than compared against the string "noop". The
    difference matters now that engines are installable: a third-party engine
    that redacts nothing would otherwise report as enabled, and "enabled" is the
    single word this whole screen exists to get right. An unknown name is assumed
    to redact, because assuming the opposite would show a working layer as off.
    """
    for info in redaction_registry.describe():
        if info.name == engine:
            return info.redacts
    return True


async def _close_quietly(redactor: Redactor) -> None:
    """Release an engine built only to prove it could be built.

    The construction check and the service probe both need a real engine, and the
    HTTP one opens a connection pool in its constructor. Leaking one per rejected
    PUT is a slow leak on an admin route, which is the kind that goes unnoticed
    for a year.
    """
    closer = getattr(redactor, "aclose", None)
    if closer is None:
        return
    try:
        await closer()
    except Exception:
        logger.warning("could not close a probe redaction engine", exc_info=True)


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
    http: Any, settings: Settings, engine: str | None = None
) -> RedactionServiceHealth | None:
    """Ask the detection service what it is and whether it is there.

    Returns ``None`` for an engine that has no service to ask — ``noop``, or an
    in-process plugin. Never raises: "it is not answering, and here is why" is
    the useful answer, and an exception would replace it with a generic error on
    a page whose whole job is to say what is wrong.
    """
    # The *effective* engine, which since ADR 0033 may be an admin's choice
    # rather than the environment's. Asking the service about a configuration
    # that is not running is how a screen ends up reassuring about the wrong
    # deployment.
    endpoint = settings.redaction.endpoint
    if (engine or settings.redaction.engine) != "http" or not endpoint:
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
        pattern_entities=[str(item) for item in body.get("pattern_entities") or []],
        model_entities=[str(item) for item in body.get("model_entities") or []],
        family_partition=bool(body.get("family_partition", False)),
    )


def _effective_detector_entities(
    service: RedactionServiceHealth | None,
    *,
    pattern_matching: bool | None,
    ner: bool | None,
) -> RedactionServiceHealth | None:
    """Narrow a reachable detector report to its enabled recognizer families.

    The service reports every installed label; the gateway knows which families
    its console configuration enabled. Rule building reads `entities`, so
    returning the installed superset here would offer PERSON while NER is off.
    Detection services that predate the family partition leave the report alone.
    """
    if service is None or not service.reachable:
        return service
    if not service.family_partition:
        return service
    patterns_enabled = pattern_matching is not False
    ner_enabled = ner is not False
    if patterns_enabled and ner_enabled:
        return service
    selected: list[str] = []
    if patterns_enabled:
        selected.extend(service.pattern_entities)
    if ner_enabled:
        selected.extend(service.model_entities)
    return service.model_copy(update={"entities": sorted(set(selected))})


async def _redaction_activity(session: SessionDep, window_seconds: int) -> RedactionActivity:
    """What redaction has actually done recently.

    Counted from the ledger rather than from a metric, so it survives a restart
    and agrees with the reports. `redaction_engine` is written per request, which
    is what makes "one engine or two in this window" answerable at all.
    """
    since = utcnow() - timedelta(seconds=window_seconds)
    redacting = UsageRecord.redaction_engine.is_not(None) & (UsageRecord.redaction_engine != "noop")
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
    config: RedactionSettings,
    engine: str,
    service: RedactionServiceHealth | None,
    *,
    policy_types: list[str] | None = None,
    presidio_pattern_matching: bool | None = None,
    presidio_ner: bool | None = None,
) -> list[str]:
    """What is wrong with this configuration, in words.

    The point of the screen. A configuration dump tells an operator what the
    settings say; these tell them the settings are not achieving what they look
    like they achieve — which is the failure mode of a redaction layer, because
    detecting nothing looks exactly like finding nothing to detect.

    ``policy_types`` is the effective policy's enumeration (None when the
    default mode is on and the set is not enumerable): merged with the env
    var's types, because a rule set on the screen must be checked as hard as
    one set in the environment.
    """
    notes: list[str] = []

    if not _engine_redacts(engine):
        notes.append(
            f"Redaction is not enabled: the engine is '{engine}', so prompts reach the "
            "provider exactly as the caller sent them."
        )
        return notes

    if presidio_pattern_matching is False and presidio_ner is False:
        notes.append(
            "Both Presidio pattern matching and named-entity recognition are off, so the "
            "detection service returns no findings."
        )

    if (
        service is not None
        and service.reachable
        and not service.family_partition
        and (presidio_pattern_matching is False or presidio_ner is False)
    ):
        # A pre-partition detector ignores the request flags. Leaving the
        # entity list unfiltered is the safe direction, but the switches would
        # otherwise look effective while changing nothing.
        notes.append(
            "The detection service predates independently selectable families, so a "
            "switched-off family is still running. Update the detector before "
            "relying on these switches."
        )

    if config.fail_open:
        # Worth a warning at all because a redaction layer that silently stops
        # redacting is worse than an outage: nothing else on the screen says the
        # prompts of the last hour went out in the clear.
        notes.append(
            "fail_open is on, so a detection failure forwards the prompt unredacted "
            "instead of refusing the request."
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
                f"The service does not serve language '{config.language}'; it offers "
                f"{', '.join(service.languages)}, so detection finds little or nothing."
            )
        elif config.language in service.degraded_languages:
            notes.append(
                f"'{config.language}' is served without a named-entity model, so fewer "
                "entities are found than for a fully supported language."
            )

        if (config.entity_types or policy_types) and service.entities:
            # Both sources of truth: the env enumeration and the effective
            # policy's. Patterns are excluded by construction — detected_types
            # carries only entity types, patterns run in the gateway itself.
            # Upper-cased for the same reason the policy stores them that way:
            # entity labels are case-free.
            unknown = sorted(
                {name.upper() for name in (config.entity_types or []) + (policy_types or [])}
                - set(service.entities)
            )
            if unknown:
                notes.append(
                    "Entity types the policy protects but the service does not detect — "
                    f"those rules are inert and the values reach providers unprotected: "
                    f"{', '.join(unknown)}."
                )

    if not config.restore_in_response:
        # Deliberate for some deployments and surprising in most, which is why it
        # is a warning rather than a line in the configuration dump.
        notes.append(
            "restore_in_response is off, so callers receive placeholders rather than "
            "the original values."
        )

    return notes


def _engine_options(
    settings: Settings, active: str, service: RedactionServiceHealth | None
) -> list[RedactionEngineOption]:
    """Every installed engine, with what stops each one being enabled.

    Computed server-side for the same reason the warnings are: the console must
    not offer a button the PATCH would refuse, and the rule that decides is the
    engine's own constructor plus the environment it would run in. Working that
    out in the browser would mean the browser knowing which settings each engine
    needs, which is exactly the vendor knowledge a registry exists to hold.
    """
    options: list[RedactionEngineOption] = []
    for info in redaction_registry.describe():
        blocked: str | None = None
        if info.needs_endpoint and not settings.redaction.endpoint:
            blocked = (
                "No detection endpoint is configured. Set GATEWAY_REDACTION__ENDPOINT and restart."
            )
        elif info.needs_endpoint and not settings.redaction.placeholder_key.get_secret_value():
            # The stability caveat stays in the message: choosing a throwaway
            # value here silently unlinks every placeholder in an old transcript
            # from the one the next request produces.
            blocked = (
                "No placeholder key is configured. Set GATEWAY_REDACTION__PLACEHOLDER_KEY "
                "and restart; it must stay stable for as long as the transcripts it "
                "labelled are kept."
            )
        # The unreachable check applies only to an engine that is *not* already
        # running: the health of the one in force is reported separately, and
        # refusing to re-enable a currently-broken engine would leave an operator
        # unable to switch away from it and back during an incident.
        elif (
            info.needs_endpoint
            and info.name != active
            and service is not None
            and not service.reachable
        ):
            blocked = f"The detection service is not answering: {service.detail}"
        options.append(
            RedactionEngineOption(
                name=info.name,
                label=info.label,
                description=info.description,
                needs_endpoint=info.needs_endpoint,
                redacts=info.redacts,
                is_active=info.name == active,
                # The engine's own reachability answer — an unreachable
                # detection service is exactly why an operator might want to
                # switch away and needs to see why re-enabling would fail.
                blocked_reason=blocked,
            )
        )
    return options


async def _redaction_response(
    *,
    session: AsyncSession,
    settings: Settings,
    request: Request,
    http: Any,
    window_seconds: int,
) -> RedactionStatusResponse:
    """The whole status document, shared by the GET and the PATCH.

    The PATCH returns the same shape so the console never has to guess what its
    own change produced — and so a change that was accepted but has not reached
    the other workers yet is visible as ``propagation_seconds`` rather than as a
    screen that looks wrong.
    """
    config = settings.redaction
    resolver: RedactionResolver | None = getattr(request.app.state, "redaction", None)
    # From the constructed redactor, not the setting and not the stored row: if
    # they ever disagree, reporting anything else describes a deployment that
    # does not exist.
    live: Redactor | None = getattr(request.app.state, "redactor", None)
    engine = getattr(live, "name", None) or config.engine

    service = await _redaction_service_health(http, settings, engine)
    stored = await current_engine(session)
    changed_by: str | None = None
    if stored is not None and stored.created_by is not None:
        changed_by = (
            await session.execute(select(User.email).where(User.id == stored.created_by))
        ).scalar_one_or_none()

    # A stored null leaves the deployment default in force. Resolved once so the
    # status, the narrowed detector report and the warnings cannot disagree.
    pattern_matching = (
        stored.presidio_pattern_matching
        if stored is not None and stored.presidio_pattern_matching is not None
        else config.presidio_pattern_matching
    )
    ner = (
        stored.presidio_ner
        if stored is not None and stored.presidio_ner is not None
        else config.presidio_ner
    )
    service = _effective_detector_entities(service, pattern_matching=pattern_matching, ner=ner)

    return RedactionStatusResponse(
        engine=engine,
        enabled=_engine_redacts(engine),
        endpoint=_sanitised_endpoint(config.endpoint),
        installed_engines=redaction_registry.available(),
        engines=_engine_options(settings, engine, service),
        source=resolver.source if resolver is not None else "environment",
        configured=(
            RedactionConfigChange(
                engine=stored.engine,
                presidio_pattern_matching=stored.presidio_pattern_matching,
                presidio_ner=stored.presidio_ner,
                reason=stored.reason,
                changed_at=stored.created_at,
                changed_by=changed_by,
            )
            if stored is not None
            else None
        ),
        propagation_seconds=(resolver.refresh_seconds if resolver is not None else 0.0),
        fail_open=config.fail_open,
        restore_in_response=config.restore_in_response,
        language=config.language,
        score_threshold=config.score_threshold,
        presidio_pattern_matching=pattern_matching,
        presidio_ner=ner,
        entity_types=list(config.entity_types) if config.entity_types else None,
        # From the resolver, not from the environment: the policy in force may be
        # a stored one, and reporting the setting instead would describe a
        # deployment that does not exist — the same rule `engine` follows above.
        policy=resolver.policy if resolver is not None else config.policy,
        policy_source=resolver.policy_source if resolver is not None else "environment",
        timeout_seconds=config.timeout_seconds,
        cache_size=config.cache_size,
        placeholder_key_set=bool(config.placeholder_key.get_secret_value()),
        service=service,
        activity=await _redaction_activity(session, window_seconds),
        # The effective policy, not the env var: since ADR 0037 the rules an
        # admin sets on this very screen are the policy in force, and a
        # mismatch check that read only `entity_types` would bless a policy
        # the screen itself made unusable.
        warnings=_redaction_warnings(
            config,
            engine,
            service,
            policy_types=resolver.policy.detected_types() if resolver else None,
            presidio_pattern_matching=pattern_matching,
            presidio_ner=ner,
        ),
    )


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
    return await _redaction_response(
        session=session,
        settings=settings,
        request=request,
        http=http,
        window_seconds=window_seconds,
    )


@router.put("/redaction/engine", response_model=RedactionStatusResponse)
async def set_redaction_engine(
    payload: RedactionEngineRequest,
    admin: AdminUserDep,
    session: SessionDep,
    settings: SettingsDep,
    http: ControlHttpDep,
    request: Request,
) -> RedactionStatusResponse:
    """Enable one installed engine, or switch the layer off by naming ``noop``.

    PUT rather than PATCH: there is one field and it is set outright, and a
    partial update of a single value is a distinction without a difference.

    Four refusals, in this order, and each of them prevents a *silent* failure
    rather than a noisy one:

    1. **An engine that is not installed.** Named with what is, never substituted.
       The registry's own rule, for its own reason: a gateway that believes
       redaction is on when it is not is the worst available outcome.
    2. **An engine the environment cannot satisfy** — a detection engine with no
       endpoint, or no placeholder key. Refused before the row is written rather
       than after, so the database never holds a configuration that cannot run.
       Saving it would mean every worker logging a construction failure on its
       next poll and continuing with the old engine, which looks like the change
       not working and reads like a bug.
    3. **A detection engine whose service is not answering.** Test before save,
       the same rule providers follow. Skipped when it is already the engine in
       force, or an operator could not switch away from a broken engine and back.
    4. **Switching the layer off without saying why.** The one change that makes
       the system quietly stop protecting anything, so it is the one that has to
       be typed out. Recorded on the row, permanently.

    The redactor for *this* worker is replaced before returning, so the operator's
    own next request sees the change. Other workers pick it up within
    ``propagation_seconds``.
    """
    engine = payload.engine.strip()
    info = {entry.name: entry for entry in redaction_registry.describe()}.get(engine)
    if info is None:
        # Third-party engines register under the 'llmp.redactors' entry-point
        # group (ADR 0026). That is how a name gets
        # into the installed list, and not something an operator acts on here.
        raise BadRequestError(
            f"unknown redaction engine {engine!r}. Installed: "
            f"{', '.join(redaction_registry.available())}."
        )

    # A reason is *accepted* and kept, and no longer demanded. It used to be
    # required for the one switch that stops redaction entirely, on the grounds
    # that a later review would read it. Asked to drop it, and the argument for
    # dropping it is better than the argument that put it there: a required
    # field on the only path that turns protection off is friction exactly
    # where an operator is already being told, in red, what the change does —
    # and a sentence typed to get past a dialog is not an audit trail. What
    # actually survives the change is still recorded without it: the engine,
    # who switched it, and when.
    reason = payload.reason.strip()

    # Proves the engine can actually be built in this environment, using the same
    # code path the resolver will use. Refusing here is the difference between an
    # error the operator sees and a construction failure in a log they do not.
    try:
        candidate = build_for(
            settings.redaction,
            engine,
            presidio_pattern_matching=payload.presidio_pattern_matching,
            presidio_ner=payload.presidio_ner,
        )
    except (redaction_registry.UnknownEngineError, ValueError) as exc:
        raise BadRequestError(
            f"{engine!r} cannot run with this deployment's configuration: {exc}"
        ) from exc

    live: Redactor | None = getattr(request.app.state, "redactor", None)
    already_running = getattr(live, "name", None) == engine
    if info.needs_endpoint and not already_running:
        probe = await _redaction_service_health(http, settings, engine)
        if probe is not None and not probe.reachable:
            await _close_quietly(candidate)
            # Enabling it anyway would fail every request needing redaction, or,
            # with fail_open on, forward every prompt unredacted.
            raise ConflictError(
                f"the detection service is not answering, so {engine!r} was not "
                f"enabled: {probe.detail}."
            )

    session.add(
        RedactionConfig(
            engine=engine,
            presidio_pattern_matching=payload.presidio_pattern_matching,
            presidio_ner=payload.presidio_ner,
            reason=reason,
            created_by=admin.id,
        )
    )
    await session.commit()

    # This worker, immediately. The resolver would get there within its poll
    # interval anyway, but an operator who just changed the engine should not have
    # to reload twice to see it, and `refresh` is what keeps the row id in step so
    # the next poll does not rebuild again.
    await _close_quietly(candidate)
    if (resolver := getattr(request.app.state, "redaction", None)) is not None:
        await resolver.refresh()

    logger.warning(
        "redaction engine set to %r by %s (%s)",
        engine,
        admin.email or admin.id,
        reason or "no reason given",
    )
    return await _redaction_response(
        session=session,
        settings=settings,
        request=request,
        http=http,
        window_seconds=86_400,
    )


# -- scoped redaction rules --------------------------------------------------
#
# One policy per subject (ADR 0038). Everything dangerous about this table is
# handled by `RedactionPolicy.combine` rather than here: a scope can only
# tighten, by construction, so a rule saved wrongly is inert rather than an
# incident. What is left for the API is the two things construction cannot do —
# refuse a rule for a subject that does not exist, and refuse a second rule for a
# subject that already has one.


#: What the catch-all scope is called on screen. A constant because it names no
#: row: there is one subject and it is every request.
EVERYTHING_LABEL = "Every request"


def _subject_columns(scope: RedactionScope) -> tuple[Any, Any]:
    """``(id column, label column)`` for the table a scope points at.

    ``scope_id`` is not a foreign key — it names a row in one of five tables —
    so this is the only place that knows which. Typed loosely on purpose: the
    five columns are of four different ORM classes and one is a ``coalesce``,
    and a union type over them would be noise around a lookup table.
    """
    match scope:
        case RedactionScope.ALL:
            # Unreachable: every caller checks for the catch-all first, because
            # it names no row. Raised rather than returned-as-None so a new
            # caller that forgets fails here instead of querying `None`.
            raise ValueError("the catch-all scope has no subject table")
        case RedactionScope.PROVIDER:
            return Provider.id, Provider.name
        case RedactionScope.MODEL:
            return ModelDef.id, ModelDef.name
        case RedactionScope.GROUP:
            return Group.id, Group.name
        case RedactionScope.USER:
            # Email is nullable — an IdP need not release one — and the subject
            # claim always exists, so a user row can always be named.
            return User.id, func.coalesce(User.email, User.subject)
        case RedactionScope.API_KEY:
            # The prefix, never the key: it is the only part of a key that is
            # safe to print and is what the console already labels keys by.
            return ApiKey.id, ApiKey.prefix


#: What to call each subject in a refusal. "No such group" is a message an
#: operator can act on; "no such scope_id" is not.
_SUBJECT_NOUNS = {
    RedactionScope.ALL: "catch-all",
    RedactionScope.PROVIDER: "provider",
    RedactionScope.MODEL: "model",
    RedactionScope.GROUP: "group",
    RedactionScope.USER: "user",
    RedactionScope.API_KEY: "API key",
}


async def _subject_labels(
    session: SessionDep, rules: Sequence[RedactionRule]
) -> dict[tuple[RedactionScope, uuid.UUID | None], str]:
    """A human name for every rule's subject, in one query per scope kind.

    Per scope kind rather than per row: a page of fifty rules spans at most five
    tables, and the alternative is fifty round trips to render one screen — the
    shape of thing that is invisible until an organisation is large, which is
    what pagination exists for.
    """
    by_scope: dict[RedactionScope, set[uuid.UUID]] = {}
    for rule in rules:
        # The catch-all names no row in any table, so there is nothing to look
        # up and its label is a constant.
        if rule.scope_id is not None:
            by_scope.setdefault(rule.scope, set()).add(rule.scope_id)

    labels: dict[tuple[RedactionScope, uuid.UUID | None], str] = {}
    if any(rule.scope is RedactionScope.ALL for rule in rules):
        labels[(RedactionScope.ALL, None)] = EVERYTHING_LABEL
    for scope, ids in by_scope.items():
        id_column, label_column = _subject_columns(scope)
        rows = (
            await session.execute(select(id_column, label_column).where(id_column.in_(ids)))
        ).all()
        for subject_id, label in rows:
            labels[(scope, subject_id)] = str(label)
    return labels


async def _check_subject(
    session: SessionDep, scope: RedactionScope, scope_id: uuid.UUID | None
) -> str:
    """The subject's label, or a refusal naming what kind of thing is missing.

    Checked before the row is written, the same rule ``_load_provider`` follows
    for a real foreign key. Without it a typo produces a rule that matches
    nothing, and a redaction rule that silently matches nothing is precisely the
    failure this feature exists to prevent — it looks identical, on the screen,
    to one that is working.

    The catch-all is the one scope with nothing to check: its subject is every
    request. Both halves of the pairing are refused here, because a catch-all
    rule carrying an id and a scoped rule carrying none are each a request that
    means something the caller did not intend, and the database CHECK would
    otherwise report it as a constraint name.
    """
    if scope is RedactionScope.ALL:
        if scope_id is not None:
            raise BadRequestError(
                "the catch-all scope applies to every request, so it takes no subject.",
                code="unexpected_subject",
            )
        return EVERYTHING_LABEL
    if scope_id is None:
        raise BadRequestError(
            f"a {_SUBJECT_NOUNS[scope]}-scoped rule needs the id of the "
            f"{_SUBJECT_NOUNS[scope]} it applies to.",
            code="missing_subject",
        )
    id_column, label_column = _subject_columns(scope)
    row = (
        await session.execute(select(id_column, label_column).where(id_column == scope_id))
    ).first()
    if row is None:
        # 404, matching `POST /api/admin/limits` two hundred lines up, which
        # answers the identical question about the identical kind of id. Two
        # adjacent admin routes disagreeing about the status for "no such group"
        # is the sort of thing a client works around once and then relies on.
        raise NotFoundError(f"No {_SUBJECT_NOUNS[scope]} with id {scope_id}.")
    return str(row[1])


def _rule_response(
    rule: RedactionRule, label: str | None, created_by_email: str | None = None
) -> RedactionRuleResponse:
    return RedactionRuleResponse(
        id=rule.id,
        name=rule.name,
        scope=rule.scope.value,
        scope_id=rule.scope_id,
        subject_label=label,
        # A row whose stored policy no longer parses is not silently replaced
        # with an empty one here: the listing is where an operator would find out
        # that the resolver is ignoring it, so it must raise rather than lie.
        policy=RedactionPolicy.model_validate(rule.policy or {}),
        is_active=rule.is_active,
        reason=rule.reason,
        created_by=rule.created_by,
        created_by_email=created_by_email,
        created_at=rule.created_at,
        updated_at=rule.updated_at,
    )


async def _load_redaction_rule(session: SessionDep, rule_id: uuid.UUID) -> RedactionRule:
    rule = (
        await session.execute(select(RedactionRule).where(RedactionRule.id == rule_id))
    ).scalar_one_or_none()
    if rule is None:
        raise NotFoundError(f"No redaction rule with id {rule_id}.")
    return rule


async def _refresh_resolver(request: Request) -> None:
    """Make this worker see a rule change immediately.

    The poll would get there within ``propagation_seconds`` anyway, but an
    operator who just saved a rule and then tried a preview would otherwise see
    the *old* answer and reasonably conclude the save had failed.
    """
    if (resolver := getattr(request.app.state, "redaction", None)) is not None:
        await resolver.refresh()


@router.get("/redaction/rules", response_model=Page[RedactionRuleResponse])
async def list_redaction_rules(
    admin: AdminUserDep,
    session: SessionDep,
    page: PageDep,
    scope: RedactionScopeName | None = None,
    is_active: bool | None = None,
) -> Page[RedactionRuleResponse]:
    """Every scoped rule, with the name of the thing each one attaches to.

    Paginated in the database, unlike the quota listing: there are no live
    counters to read first, so there is nothing to gain by loading them all.
    """
    stmt = select(RedactionRule, User.email).outerjoin(User, User.id == RedactionRule.created_by)
    if scope is not None:
        stmt = stmt.where(RedactionRule.scope == RedactionScope(scope))
    if is_active is not None:
        stmt = stmt.where(RedactionRule.is_active.is_(is_active))
    stmt = stmt.order_by(RedactionRule.scope, RedactionRule.created_at.desc())

    total = await count_of(session, stmt)
    rows = (await session.execute(page.apply(stmt))).all()
    labels = await _subject_labels(session, [rule for rule, _ in rows])
    return page.page(
        [
            _rule_response(rule, labels.get((rule.scope, rule.scope_id)), email)
            for rule, email in rows
        ],
        total,
    )


@router.post(
    "/redaction/rules",
    response_model=RedactionRuleResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_redaction_rule(
    payload: RedactionRuleCreateRequest,
    admin: AdminUserDep,
    session: SessionDep,
    request: Request,
) -> RedactionRuleResponse:
    """Attach a policy to one provider, model, group, user or key.

    Two refusals, and neither is about safety — the fold already guarantees that
    a scope cannot weaken anything. Both are about a rule that would look right
    and do nothing:

    * **a subject that does not exist**, which produces a rule matching no
      request, indistinguishable on screen from one that matches every request;
    * **a second rule for a subject that already has one**, which the unique
      index refuses anyway. Caught here so the operator is told to edit the
      existing rule rather than reading a database constraint's name.
    """
    scope = RedactionScope(payload.scope)
    label = await _check_subject(session, scope, payload.scope_id)

    existing = (
        await session.execute(
            select(RedactionRule.id).where(
                RedactionRule.scope == scope, RedactionRule.scope_id == payload.scope_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError(
            f"A redaction rule already exists for that {_SUBJECT_NOUNS[scope]} "
            f"({label}). Edit it instead of adding a second one."
        )

    rule = RedactionRule(
        name=payload.name,
        scope=scope,
        scope_id=payload.scope_id,
        policy=payload.policy.model_dump(mode="json"),
        is_active=payload.is_active,
        reason=payload.reason,
        created_by=admin.id,
    )
    session.add(rule)
    try:
        await session.commit()
    except IntegrityError as exc:
        # The pre-check above lost a race with another admin. Same message: the
        # operator's next action is the same either way.
        await session.rollback()
        raise ConflictError(
            f"A redaction rule already exists for that {_SUBJECT_NOUNS[scope]} "
            f"({label}). Edit it instead of adding a second one."
        ) from exc
    await session.refresh(rule)
    await _refresh_resolver(request)

    logger.warning(
        "redaction rule for %s %s created by %s: default=%s, %d entity rule(s)",
        scope.value,
        payload.scope_id,
        admin.email or admin.id,
        payload.policy.default_mode,
        len(payload.policy.entities),
    )
    return _rule_response(rule, label, admin.email)


@router.patch("/redaction/rules/{rule_id}", response_model=RedactionRuleResponse)
async def update_redaction_rule(
    rule_id: uuid.UUID,
    payload: RedactionRuleUpdateRequest,
    admin: AdminUserDep,
    session: SessionDep,
    request: Request,
) -> RedactionRuleResponse:
    """Change a rule's policy, name, reason, active flag — or its subject.

    A subject change runs the same checks creation does, plus one: the subject
    must be free, with this rule itself excluded from that search. It used to be
    refused outright, on the argument that re-pointing makes two subjects'
    histories read as one — but those histories live in ``usage_records``, which
    stamps the scope and rule id onto every request at request time and so does
    not change when the row does, while the freeze left a rule whose subject had
    been deleted permanently unrepairable. See ``RedactionRuleUpdateRequest``.
    """
    rule = await _load_redaction_rule(session, rule_id)
    fields = payload.model_dump(exclude_unset=True, mode="json")

    # The subject is a pair, so it is decided together even when only one half
    # arrived: a new scope without a subject is legal only for the catch-all,
    # and a new subject under the old scope re-points within it. Absent means
    # unchanged, not null — which is what keeps a PATCH of the name alone from
    # revalidating a subject that may since have been deleted. A scope echoed
    # back unchanged with no subject alongside it also means unchanged: the
    # only rule that could survive having its subject cleared is the catch-all,
    # whose subject was already nothing.
    has_scope, has_subject = "scope" in fields, "scope_id" in fields
    new_scope = RedactionScope(fields.pop("scope")) if has_scope else rule.scope
    if has_subject:
        raw = fields.pop("scope_id")
        new_scope_id = uuid.UUID(raw) if raw is not None else None
    elif has_scope:
        new_scope_id = rule.scope_id if new_scope is rule.scope else None
    else:
        new_scope_id = rule.scope_id
    moved = (new_scope, new_scope_id) != (rule.scope, rule.scope_id)

    label: str | None = None
    if moved:
        # The same two refusals creation makes — no such subject, and a
        # catch-all carrying or lacking its subject — then the one creation
        # cannot make: the destination is occupied by a *different* rule.
        label = await _check_subject(session, new_scope, new_scope_id)
        conflicting = (
            await session.execute(
                select(RedactionRule.id, RedactionRule.name).where(
                    RedactionRule.scope == new_scope,
                    RedactionRule.scope_id == new_scope_id,
                    RedactionRule.id != rule.id,
                )
            )
        ).first()
        if conflicting is not None:
            name = conflicting.name or "unnamed rule"
            raise ConflictError(
                f"A redaction rule already exists for that {_SUBJECT_NOUNS[new_scope]} "
                f"({label}): {name}. Edit it, or choose another subject."
            )

    for field, value in fields.items():
        setattr(rule, field, value)
    # Read before the mutation below: the log's "from" half must name where the
    # rule was, and after ``setattr`` the ORM object only knows where it is.
    old_scope, old_scope_id = rule.scope, rule.scope_id
    if moved:
        rule.scope = new_scope
        rule.scope_id = new_scope_id
    try:
        await session.commit()
    except IntegrityError as exc:
        # The pre-check above lost a race with another admin; the unique index
        # is what caught it. Same refusal the create route gives — and
        # ``label`` is always set here, because an identity-index violation on
        # this table can only come from a subject move, and a move is what
        # computed it.
        await session.rollback()
        raise ConflictError(
            f"A redaction rule already exists for that {_SUBJECT_NOUNS[new_scope]} "
            f"({label}). Edit it, or choose another subject."
        ) from exc
    await session.refresh(rule)
    await _refresh_resolver(request)

    labels = await _subject_labels(session, [rule])
    email = (
        await session.execute(select(User.email).where(User.id == rule.created_by))
    ).scalar_one_or_none()
    if moved:
        logger.warning(
            "redaction rule %s re-pointed from %s %s to %s %s by %s; edited: %s",
            rule.id,
            old_scope.value,
            old_scope_id,
            new_scope.value,
            new_scope_id,
            admin.email or admin.id,
            ", ".join(sorted(fields)) or "nothing",
        )
    else:
        logger.warning(
            "redaction rule %s (%s %s) edited by %s: %s",
            rule.id,
            rule.scope.value,
            rule.scope_id,
            admin.email or admin.id,
            ", ".join(sorted(fields)) or "nothing",
        )
    return _rule_response(rule, labels.get((rule.scope, rule.scope_id)), email)


@router.delete("/redaction/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_redaction_rule(
    rule_id: uuid.UUID, admin: AdminUserDep, session: SessionDep, request: Request
) -> None:
    """Remove a rule outright.

    Safe to delete, unlike a model or a key: nothing in the ledger points *at* a
    rule. ``usage_records.redaction_rule_id`` is a stamp copied onto each
    request, not a foreign key, which is exactly so that deleting a rule cannot
    erase the record of what it did.
    """
    rule = await _load_redaction_rule(session, rule_id)
    await session.execute(delete(RedactionRule).where(RedactionRule.id == rule_id))
    await session.commit()
    await _refresh_resolver(request)
    logger.warning(
        "redaction rule %s (%s %s) deleted by %s",
        rule.id,
        rule.scope.value,
        rule.scope_id,
        admin.email or admin.id,
    )


# -- the preview box ---------------------------------------------------------


@runtime_checkable
class _DetectingRedactor(Protocol):
    """An engine that can report spans without rewriting anything.

    Not part of the ``Redactor`` protocol: the request path never needs the
    spans, only the rewritten messages, and widening the interface every engine
    must implement for the sake of one admin screen is the wrong trade. Asked
    for structurally instead, so ``noop`` — which detects nothing and says so —
    simply does not match.
    """

    async def detect(
        self, texts: list[str], *, policy: RedactionPolicy | None = ...
    ) -> list[list[EntitySpan]]: ...


def _policy_for_subject(
    resolver: RedactionResolver, scope: RedactionScope, scope_id: uuid.UUID | None
) -> EffectivePolicy:
    """The folded policy one subject's requests would run under.

    Spelled out per scope rather than built as ``{f"{scope}_id": id}``: the
    keyword names are part of the resolver's signature, and a mistyped one would
    be a preview that silently showed the deployment policy instead.
    """
    match scope:
        case RedactionScope.ALL:
            # Every request already carries the catch-all, so previewing "as"
            # it is just the base fold with no narrower subject.
            return resolver.policy_for()
        case RedactionScope.PROVIDER:
            return resolver.policy_for(provider_id=scope_id)
        case RedactionScope.MODEL:
            return resolver.policy_for(model_id=scope_id)
        case RedactionScope.GROUP:
            return resolver.policy_for(group_id=scope_id)
        case RedactionScope.USER:
            return resolver.policy_for(user_id=scope_id)
        case RedactionScope.API_KEY:
            return resolver.policy_for(api_key_id=scope_id)


@router.post("/redaction/preview", response_model=RedactionPreviewResponse)
async def preview_redaction(
    payload: RedactionPreviewRequest,
    admin: AdminUserDep,
    session: SessionDep,
    settings: SettingsDep,
    request: Request,
) -> RedactionPreviewResponse:
    """Run the real redaction path over a sample and report what it did.

    **Not a proxy to ``/detect``.** The spans alone answer "what does the
    detector find", which is not the question anyone has: the question is *what
    would the provider receive*, and that is the modes, the per-type thresholds,
    the allow-list and the overlap resolution — none of which live in the
    detector. So this calls ``apply_spans`` with the effective policy, over a
    throwaway ``PlaceholderMap``, exactly as a request would.

    Two things this route does deliberately differently from a request:

    * **A block is a 200 with ``blocked: true``**, never a 403. This is a
      preview; refusing it would make the tool look broken at the exact moment
      it is doing its job, and an operator checking whether a rule blocks needs
      the answer rather than an error.
    * **Nothing here is logged.** Not the sample, not the spans, not the
      rewrite. Within a week of shipping this box will contain the real prompt
      that came back wrong — that is what it is for — and a redaction inspector
      that writes unredacted personal data to the log file has defeated itself.
    """
    resolver: RedactionResolver | None = getattr(request.app.state, "redaction", None)
    # Typed as `object` rather than `Redactor` so the structural check below can
    # narrow it: an engine that can report spans is a wider interface than the
    # request path's, not a narrower one.
    redactor: object = getattr(request.app.state, "redactor", None)
    engine = getattr(redactor, "name", None) or settings.redaction.engine

    effective = EffectivePolicy(
        policy=resolver.policy if resolver is not None else settings.redaction.policy
    )
    if payload.scope is not None and payload.scope_id is not None:
        scope = RedactionScope(payload.scope)
        # Same check as creating a rule: previewing "as" a group that does not
        # exist would show the deployment policy and look like a rule that is
        # not taking effect.
        await _check_subject(session, scope, payload.scope_id)
        if resolver is not None:
            effective = _policy_for_subject(resolver, scope, payload.scope_id)

    if not isinstance(redactor, _DetectingRedactor):
        return RedactionPreviewResponse(
            engine=engine,
            scope=effective.scope,
            rule_id=effective.rule_id,
            policy=effective.policy,
            redacted_text=payload.text,
            note=(
                f"the {engine!r} engine detects nothing, so this sample would reach "
                "the provider exactly as it is written"
            ),
        )

    spans = (await redactor.detect([payload.text], policy=effective.policy))[0]
    described = [
        RedactionPreviewSpan(
            entity_type=span.entity_type,
            start=span.start,
            end=span.end,
            score=span.score,
            mode=effective.policy.mode_for(span.entity_type).value,
            threshold=effective.policy.threshold_for(
                span.entity_type, settings.redaction.score_threshold
            ),
            allow_listed=effective.policy.allows(span.slice_of(payload.text)),
        )
        for span in spans
    ]

    # A throwaway map: nothing here is restored into a response, and keeping it
    # would be keeping the real values of whatever the admin pasted.
    placeholders = PlaceholderMap()
    try:
        rewritten, count = apply_spans(
            payload.text,
            spans,
            key=settings.redaction.placeholder_key.get_secret_value().encode(),
            placeholders=placeholders,
            policy=effective.policy,
            default_threshold=settings.redaction.score_threshold,
            scope=effective.scope,
        )
    except ContentBlockedError as exc:
        return RedactionPreviewResponse(
            engine=engine,
            scope=effective.scope,
            rule_id=effective.rule_id,
            policy=effective.policy,
            spans=described,
            redacted_text=None,
            blocked=True,
            blocked_reason=str(exc),
        )

    return RedactionPreviewResponse(
        engine=engine,
        scope=effective.scope,
        rule_id=effective.rule_id,
        policy=effective.policy,
        spans=described,
        redacted_text=rewritten,
        entity_count=count,
    )
