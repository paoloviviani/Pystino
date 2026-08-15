"""Request-scoped dependencies: authentication and shared services."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from gateway.accounting import TokenEstimator
from gateway.config import Settings
from gateway.errors import AuthenticationError, PermissionError_
from gateway.models import ApiKey, Group, User
from gateway.providers import ProviderRegistry
from gateway.quota import QuotaEngine, QuotaSubject
from gateway.redaction import Redactor
from gateway.secrets import SecretBox
from gateway.security import (
    extract_prefix,
    parse_authorization_header,
    verify_api_key,
)
from gateway.types import utcnow

logger = logging.getLogger(__name__)

# last_used_at is a convenience for humans auditing keys, not part of any
# decision, so it is written at most this often to avoid a write per request.
_LAST_USED_RESOLUTION = timedelta(seconds=60)


@dataclass(slots=True)
class Principal:
    """An authenticated caller and the group its usage is billed to."""

    user: User
    billing_group: Group
    api_key: ApiKey | None = None

    @property
    def quota_subject(self) -> QuotaSubject:
        return QuotaSubject(user_id=self.user.id, group_id=self.billing_group.id)


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_session_factory(request: Request) -> object:
    return request.app.state.session_factory


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """A session for the lifetime of one request.

    The streaming path deliberately does **not** use this: it opens its own short
    sessions, because a request-scoped session would stay open for the whole
    generation.
    """
    factory = request.app.state.session_factory
    async with factory() as session:
        yield session


def get_quota_engine(request: Request) -> QuotaEngine:
    engine: QuotaEngine = request.app.state.quota_engine
    return engine


def get_redactor(request: Request) -> Redactor:
    redactor: Redactor = request.app.state.redactor
    return redactor


def get_control_http(request: Request) -> Any:
    """The control-plane HTTP client, with a finite read timeout.

    Distinct from the upstream client, which has ``read=None`` so long streams
    survive. Anything that is not a model stream — OIDC discovery, fetching a
    provider catalogue — must use this one, or a hung dependency hangs the request
    forever.
    """
    return request.app.state.control_http


def get_secrets(request: Request) -> SecretBox:
    """The box that encrypts provider credentials at rest (ADR 0027)."""
    secrets: SecretBox = request.app.state.secrets
    return secrets


def get_providers(request: Request) -> ProviderRegistry:
    """The per-provider upstream clients.

    Replaces the single shared upstream: which endpoint a request reaches now
    depends on the model it named (ADR 0027).
    """
    registry: ProviderRegistry = request.app.state.providers
    return registry


def get_estimator(request: Request) -> TokenEstimator:
    estimator: TokenEstimator = request.app.state.token_estimator
    return estimator


async def resolve_api_key(session: AsyncSession, secret: str) -> ApiKey:
    """Look up and verify a presented key.

    Every failure path raises the same error with the same message: distinguishing
    "no such key" from "revoked key" from "wrong key" would tell an attacker which
    of their guesses was structurally correct.
    """
    prefix = extract_prefix(secret)
    if prefix is None:
        raise AuthenticationError("Invalid API key provided.")

    stmt = (
        select(ApiKey)
        .where(ApiKey.prefix == prefix)
        .options(
            selectinload(ApiKey.user).selectinload(User.memberships),
            selectinload(ApiKey.billing_group),
        )
    )
    api_key = (await session.execute(stmt)).scalar_one_or_none()
    if api_key is None:
        raise AuthenticationError("Invalid API key provided.")

    if not verify_api_key(secret, api_key.key_hash):
        raise AuthenticationError("Invalid API key provided.")

    if not api_key.is_usable():
        raise AuthenticationError("Invalid API key provided.")

    if not api_key.user.is_active:
        raise AuthenticationError("Invalid API key provided.")

    return api_key


def resolve_billing_group(api_key: ApiKey) -> Group:
    """Decide which group pays, and check the caller may still charge it.

    A key may pin a group; otherwise the user's current default applies. Either
    way membership is re-checked here rather than trusted from when the key was
    minted, so leaving a group immediately stops you billing it.
    """
    user = api_key.user
    group = api_key.billing_group or user.default_billing_group

    if group is None:
        raise PermissionError_(
            "No billing group is set for this key or user. Set a default billing "
            "group in the management API before making requests."
        )

    if not group.is_active:
        raise PermissionError_(f"Billing group {group.name!r} is disabled.")

    if group.id not in user.group_ids():
        raise PermissionError_(f"You are not a member of billing group {group.name!r}.")

    return group


async def _touch_last_used(session: AsyncSession, api_key: ApiKey) -> None:
    now = utcnow()
    if api_key.last_used_at is not None and now - api_key.last_used_at < _LAST_USED_RESOLUTION:
        return
    try:
        await session.execute(
            update(ApiKey).where(ApiKey.id == api_key.id).values(last_used_at=now)
        )
        await session.commit()
        api_key.last_used_at = now
    except Exception:
        # Never fail a request over bookkeeping.
        await session.rollback()
        logger.debug("could not update last_used_at", exc_info=True)


async def get_principal(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Principal:
    """Authenticate a ``/v1`` caller via API key."""
    secret = parse_authorization_header(request.headers.get("authorization"))
    if not secret:
        # Some clients send the key in this header instead.
        secret = request.headers.get("api-key")
    if not secret:
        raise AuthenticationError(
            "You didn't provide an API key. Supply it in an Authorization header: "
            "'Authorization: Bearer <key>'."
        )

    api_key = await resolve_api_key(session, secret)
    group = resolve_billing_group(api_key)
    await _touch_last_used(session, api_key)
    return Principal(user=api_key.user, billing_group=group, api_key=api_key)


async def load_user_for_management(session: AsyncSession, user_id: uuid.UUID) -> User:
    stmt = select(User).where(User.id == user_id).options(selectinload(User.memberships))
    user = (await session.execute(stmt)).scalar_one_or_none()
    if user is None or not user.is_active:
        raise AuthenticationError("Session is no longer valid.")
    return user


async def get_management_user(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    """Authenticate a management-API caller from the session cookie.

    Separate from :func:`get_principal` on purpose: the management API is for
    humans in a browser and has no billing group, while ``/v1`` is for programs
    and always does.
    """
    from gateway.oidc import OIDCError, verify_session_token

    settings: Settings = request.app.state.settings
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise AuthenticationError("Not authenticated. Sign in at /auth/login.")

    try:
        user_id = verify_session_token(token, secret=settings.session_secret.get_secret_value())
    except OIDCError as exc:
        raise AuthenticationError("Session is not valid or has expired.") from exc

    return await load_user_for_management(session, user_id)


async def get_admin_user(
    user: Annotated[User, Depends(get_management_user)],
) -> User:
    """Require an administrator.

    Separate from :func:`get_management_user` so that every administrative route
    is opt-in: forgetting the dependency fails closed at review time, because the
    route simply will not compile into an admin router without it.
    """
    if not user.is_admin:
        raise PermissionError_("Administrator access is required.")
    return user


SessionDep = Annotated[AsyncSession, Depends(get_session)]
ManagementUserDep = Annotated[User, Depends(get_management_user)]
AdminUserDep = Annotated[User, Depends(get_admin_user)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
PrincipalDep = Annotated[Principal, Depends(get_principal)]
QuotaDep = Annotated[QuotaEngine, Depends(get_quota_engine)]
RedactorDep = Annotated[Redactor, Depends(get_redactor)]
ProvidersDep = Annotated[ProviderRegistry, Depends(get_providers)]
SecretsDep = Annotated[SecretBox, Depends(get_secrets)]
ControlHttpDep = Annotated[Any, Depends(get_control_http)]
EstimatorDep = Annotated[TokenEstimator, Depends(get_estimator)]
