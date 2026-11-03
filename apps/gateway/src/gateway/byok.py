"""Bring your own key: who a catalogue operation acts for, and what it may touch.

A user holding the ``byok`` capability manages **their own** providers and
models (their own API key) with the very operations an administrator uses on
the deployment's catalogue: create, list, get, update, delete, probe, catalogue
discovery and import, price rows. Those operations are written once, in
``routers/admin.py``, and take a :class:`CatalogueScope` that says whose rows
they act on. The same handler functions are registered twice: under
``/api/admin`` with an administrator's scope (owner ``None``: deployment rows
only) and under ``/api/me`` with the caller's own (their rows only). Duplicating
them would have been a second copy of the money- and credential-handling code,
which is the duplication this project avoids.

**Fail closed.** The scope is chosen by the dependency below from the request
path and defaults to the administrator's; an administrator's scope can never see
a private row (the admin routes list, read, edit and delete deployment rows
only: private rows are reached through ``/api/admin/byok`` and nowhere else),
and a private scope can never see anyone else's or a deployment's. Another
user's id is a 404, never a 403: whether it exists is not the caller's business.

What a user may *not* do, and why, is in :data:`MODEL_KINDS`, :func:`require_plugin`
and :func:`require_headers`: LLM providers only (plugin kind provider or router,
protocol openai or anthropic, never a search backend or the extractor, which are
deployment infrastructure), and chat, embedding, image and transcription
models. Aliases, routing and search plugins are out of scope. Document
extraction (``ocr``) and decision models are deployment features and are not
offered.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy import ColumnElement, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.admin_audit import record_audit
from gateway.capabilities import holds_byok
from gateway.deps import ManagementUserDep
from gateway.errors import BadRequestError, ByokNotAllowedError, PermissionError_
from gateway.models import MY_PREFIX, ModelDef, ModelKind, Provider, ProviderKind, User
from gateway.netguard import NetworkGuardError
from gateway.plugins import registry as plugin_registry
from gateway.plugins.base import ProviderKind as PluginKind
from gateway.plugins.base import protocol_of

#: Generous ceilings, so one account cannot fill the tables. Not a product
#: limit: nobody has 25 genuinely different endpoints.
MAX_PRIVATE_PROVIDERS = 25
MAX_PRIVATE_MODELS = 500

#: The kinds of model a user may add. Chat, plus the other kinds whose `/v1`
#: surface already meters through the one pipeline; `ocr` (the deployment's
#: extractor and per-page models), `decision` and `search` are deployment
#: features.
MODEL_KINDS: frozenset[ModelKind] = frozenset(
    {ModelKind.CHAT, ModelKind.EMBEDDING, ModelKind.IMAGE, ModelKind.TRANSCRIPTION}
)

#: Headers a private provider's ``extra_headers`` may not set: they would
#: interfere with the request's framing or the connection, or name the origin
#: server for a request the gateway is routing elsewhere.
_FORBIDDEN_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "upgrade",
        "expect",
        "te",
        "trailer",
        "proxy-authorization",
        "proxy-connection",
        "forwarded",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-real-ip",
    }
)

#: The path prefix that selects the caller's own scope.
SELF_PREFIX = "/api/me/"


@dataclass(frozen=True, slots=True)
class CatalogueScope:
    """Whose providers and models a catalogue operation acts on."""

    #: Who is acting.
    actor: User
    #: The owner of the rows in scope; ``None`` is the deployment's own catalogue.
    owner: User | None = None

    @property
    def private(self) -> bool:
        return self.owner is not None

    def provider_filter(self) -> ColumnElement[bool]:
        return _owned(Provider.owner_id, self.owner)

    def model_filter(self) -> ColumnElement[bool]:
        return _owned(ModelDef.owner_id, self.owner)

    def audit(self, session: AsyncSession, *args: Any, **kwargs: Any) -> None:
        """Audit-log an **administrator's** change; a user's own are not.

        The admin audit log records what administrators did to the
        deployment. A member editing their own provider is not an
        administrative act, and the log would otherwise carry one person's
        private endpoint names to everyone who reads it.
        """
        if self.owner is None:
            record_audit(session, self.actor, *args, **kwargs)


def _owned(column: Any, owner: User | None) -> ColumnElement[bool]:
    if owner is None:
        return column.is_(None)  # type: ignore[no-any-return]
    return column == owner.id  # type: ignore[no-any-return]


def deployment_filter(column: Any) -> ColumnElement[bool]:
    return _owned(column, None)


async def get_catalogue_scope(request: Request, user: ManagementUserDep) -> CatalogueScope:
    """The scope for this request: the caller's own under ``/api/me``, else admin.

    The admin branch is the default and is what every other path gets, so a
    handler registered somewhere unexpected is administrator-only, never
    self-service by accident.
    """
    if request.url.path.startswith(SELF_PREFIX):
        if not holds_byok(user):
            raise ByokNotAllowedError(
                "Bring-your-own-key is not enabled for this account. Ask an administrator "
                "to switch it on for one of your groups."
            )
        return CatalogueScope(actor=user, owner=user)
    if not user.is_admin:
        raise PermissionError_("Administrator access is required.")
    return CatalogueScope(actor=user, owner=None)


CatalogueScopeDep = Annotated[CatalogueScope, Depends(get_catalogue_scope)]


def require_plugin(plugin_name: str | None, kind: str | None = None) -> None:
    """Refuse a provider type a user may not add (LLM providers only)."""
    try:
        plugin = plugin_registry.resolve(plugin_name)
    except plugin_registry.UnknownPluginError as exc:
        raise BadRequestError(str(exc), code="unknown_plugin") from exc
    if plugin.kind not in (PluginKind.PROVIDER, PluginKind.ROUTER) or protocol_of(plugin) not in (
        "openai",
        "anthropic",
    ):
        raise BadRequestError(
            f"The {plugin.label!r} type cannot be added as your own provider: only language "
            "model providers (OpenAI-compatible or Anthropic) can.",
            code="byok_plugin_not_allowed",
        )
    if kind is not None and kind not in (ProviderKind.PROVIDER.value, ProviderKind.ROUTER.value):
        raise BadRequestError(
            "Your own providers are of kind 'provider' or 'router'.",
            code="byok_kind_not_allowed",
        )


def require_headers(headers: dict[str, str]) -> None:
    bad = sorted(name for name in headers if name.lower() in _FORBIDDEN_HEADERS)
    if bad:
        raise BadRequestError(
            f"These headers cannot be set on your own provider: {', '.join(bad)}.",
            code="byok_header_not_allowed",
        )


def require_model_kind(kind: ModelKind) -> None:
    if kind not in MODEL_KINDS:
        allowed = ", ".join(sorted(k.value for k in MODEL_KINDS))
        raise BadRequestError(
            f"A {kind.value} model cannot be added as your own. Allowed kinds: {allowed}.",
            code="byok_kind_not_allowed",
        )


def refuse_my_prefix(name: str) -> None:
    """``my/`` is the API's prefix for a private model, so no stored name may start with it."""
    if name.startswith(MY_PREFIX):
        raise BadRequestError(
            f"A model name cannot start with {MY_PREFIX!r}: that prefix addresses a user's own "
            "models on the API.",
            code="reserved_model_name",
        )


async def check_private_url(providers: Any, base_url: str) -> None:
    """Validate a private provider's base URL against the network guard (400)."""
    try:
        await providers.validate_private_url(base_url)
    except NetworkGuardError as exc:
        raise BadRequestError(str(exc), code="provider_url_blocked") from exc


async def enforce_ceiling(session: AsyncSession, scope: CatalogueScope, *, models: bool) -> None:
    """A private owner may hold only so many rows (see the constants)."""
    if scope.owner is None:
        return
    table = ModelDef if models else Provider
    count = await session.scalar(
        select(func.count()).select_from(table).where(table.owner_id == scope.owner.id)
    )
    limit = MAX_PRIVATE_MODELS if models else MAX_PRIVATE_PROVIDERS
    if (count or 0) >= limit:
        raise BadRequestError(
            f"You already have {limit} {'models' if models else 'providers'}, the most one "
            "account may add.",
            code="byok_limit_reached",
        )


async def delete_owned_catalogue(session: AsyncSession, owner_id: uuid.UUID) -> tuple[int, int]:
    """Delete a user's private models and then their providers; ``(models, providers)``.

    The erasure path calls this before it deletes the user. In this order, and
    explicitly, because ``models.provider_id`` is RESTRICT and PostgreSQL gives
    no order between two ON DELETE CASCADEs from the user row. Prices and
    anything else hanging off a model go with it by ON DELETE; the ledger keeps
    its rows (``model_id`` is SET NULL and the name is denormalised).
    """
    models = await session.execute(delete(ModelDef).where(ModelDef.owner_id == owner_id))
    providers = await session.execute(delete(Provider).where(Provider.owner_id == owner_id))
    return (
        int(getattr(models, "rowcount", 0) or 0),
        int(getattr(providers, "rowcount", 0) or 0),
    )


__all__ = [
    "MAX_PRIVATE_MODELS",
    "MAX_PRIVATE_PROVIDERS",
    "MODEL_KINDS",
    "CatalogueScope",
    "CatalogueScopeDep",
    "check_private_url",
    "delete_owned_catalogue",
    "deployment_filter",
    "enforce_ceiling",
    "get_catalogue_scope",
    "refuse_my_prefix",
    "require_headers",
    "require_model_kind",
    "require_plugin",
]
