"""Request-scoped dependencies: authentication and shared services."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from gateway.accounting import TokenEstimator
from gateway.config import Settings
from gateway.errors import AuthenticationError, PermissionError_
from gateway.identity_registry import list_providers
from gateway.models import ApiKey, Group, GroupSync, User
from gateway.oidc import OIDCClient, OIDCError, sync_user_from_claims
from gateway.oidc_policy import OIDCPolicy
from gateway.providers import ProviderRegistry
from gateway.quota import QuotaEngine, QuotaSubject
from gateway.redaction import Redactor
from gateway.secrets import SecretBox
from gateway.security import (
    extract_prefix,
    looks_like_jwt,
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


@dataclass(slots=True)
class AuthenticatedCaller:
    """A caller identified, and nothing more (ADR 0093 to-do item 2).

    ``Principal`` always carries a billing group because every metered
    ``/v1`` route needs one to charge and is right to refuse a caller who has
    none. ``/v1/me`` and ``/v1/me/identities`` answer "who is this" — a
    question a bundled-Authelia user with no membership at all can still
    ask, and answering it must not run into the same refusal a chat
    completion would. Kept as its own type rather than making
    ``Principal.billing_group`` optional: every other consumer of
    ``Principal`` (quotas, accounting, redaction scoping, ``x-bill-to``)
    reads that field assuming it resolved, and Optional-ing it there would
    trade one clear refusal for a null check scattered through all of them.
    """

    user: User
    api_key: ApiKey | None = None
    #: Same pin-or-default resolution as `Principal.billing_group`, just
    #: never refused: null for a caller with nothing billable, where
    #: `resolve_billing_group` would have raised instead.
    billing_group: Group | None = None


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_oidc_policy(request: Request) -> OIDCPolicy:
    """The identity policy in force on this worker (ADR 0048).

    Read off ``app.state`` rather than re-derived: the resolver owns the poll,
    and the login path must agree with what the admin screen reports.
    """
    policy = request.app.state.oidc_policy.policy
    assert isinstance(policy, OIDCPolicy)
    return policy


OidcPolicyDep = Annotated[OIDCPolicy, Depends(get_oidc_policy)]


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

    # `joinedload` for the two many-to-one relations, not `selectinload`.
    #
    # This runs on every single request, so its round trips are multiplied by
    # everything the gateway does. `selectinload` issues one SELECT per
    # relation — four in total here — where a join fetches the key, its user and
    # its billing group in one. Memberships stay `selectinload` because they are
    # to-many: joining them would multiply the key row by the membership count
    # and make the result set quadratic in a user's group count.
    #
    # Measured: four selects to authenticate became two. See
    # apps/gateway/tests/test_query_counts.py, which pins it.
    stmt = (
        select(ApiKey)
        .where(ApiKey.prefix == prefix)
        .options(
            joinedload(ApiKey.user).selectinload(User.memberships),
            joinedload(ApiKey.billing_group),
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


def resolve_billing_group(user: User, *, pinned: Group | None = None) -> Group:
    """Decide which group pays, and check the caller may still charge it.

    A key may pin a group; a bearer caller may ask for one per request with the
    ``x-bill-to`` header. Otherwise the user's current default applies. Every
    route through here re-checks membership rather than trusting it from when
    the key was minted or the token issued, so leaving a group immediately stops
    you billing it — which is also what makes the header safe to honour.

    Note what the header is *not*: a new capability. It can only name a group
    the caller is a member of right now, and any such group could already be
    billed by changing the default through ``PUT /api/me/default-billing-group``.
    It removes a round trip through the management API, nothing more. That is
    the whole security argument, and it rests on the membership check below
    rather than on anything the caller is trusted about.
    """
    group = pinned or user.default_billing_group

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


def resolve_billing_group_or_none(user: User, *, pinned: Group | None = None) -> Group | None:
    """Like :func:`resolve_billing_group`, but reports rather than refuses.

    ``/v1/me`` and ``/v1/me/identities`` (ADR 0093 to-do item 2) authenticate
    a caller with nothing billable at all and must still answer 200 — this
    is the same pin-or-default resolution, minus the raise, so a pinned
    ``x-bill-to`` group still reports back exactly as it would for a metered
    route (see ``test_v1_me.py``'s ``test_it_reports_the_group_the_request_chose``),
    and only the "nothing resolved" case turns into ``None`` instead of a 403.
    """
    group = pinned or user.default_billing_group
    if group is None or not group.is_active or group.id not in user.group_ids():
        return None
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


# Lowercase and with no vendor segment, matching `x-filename` and
# `x-request-id`, the two request headers this gateway already defines. The
# deliberate contrast is HuggingFace's `X-HF-Bill-To`, which chat-ui sends
# today: a client that had to name a product in order to say "bill this to that
# account" is a client welded to one server. The concept is not vendor-specific
# and neither is the spelling, so a second gateway could honour it unchanged.
BILL_TO_HEADER = "x-bill-to"


def requested_billing_group(request: Request) -> str | None:
    """The group this request asks to be billed to, if it asked.

    A name, not an id: the caller is a person's browser choosing from a list of
    group names, and ids appear nowhere in that conversation.
    """
    value = request.headers.get(BILL_TO_HEADER)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _pin_from_header(user: User, requested: str) -> Group:
    """Resolve a requested group name against this user's effective memberships.

    Resolved from the memberships rather than by querying groups by name, which
    is not an optimisation: a name that exists but is not one of this user's
    groups must be indistinguishable from a name that does not exist, or the
    header becomes a way to enumerate the deployment's groups.
    """
    for membership in user.memberships:
        group = membership.group
        if group is not None and group.name == requested:
            return group
    raise PermissionError_(f"You are not a member of billing group {requested!r}.")


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

    if looks_like_jwt(secret):
        return await _bearer_principal(request, session, secret)

    api_key = await resolve_api_key(session, secret)
    requested = requested_billing_group(request)
    if requested is not None and api_key.is_issued_key:
        # Refused, not ignored, and refused for *every* issued key rather than
        # only a pinned one. A key already carries its answer — the group it
        # pins, or the user's default at request time — and that is a property
        # of the credential an administrator issued. Letting a request override
        # it is a separate decision with its own blast radius, and it is not
        # the one being made here.
        #
        # Ignoring the header instead was the alternative and is worse: a caller
        # who asked to bill one group and was quietly billed another finds out
        # from an invoice.
        #
        # A *minted* credential is the other kind of row in that table and the
        # reason this is a property, not a shape test: the house IdP's access
        # tokens are opaque gwa keys stored exactly here (`routers/idp.py`),
        # so "authenticated with an OIDC token" and "authenticated with an API
        # key" cannot be told apart by looking at the bearer. They are told
        # apart by provenance — a minted credential is the proof of who is
        # calling and steers like the JWT it stands in for (same membership
        # check, below); a standing key bills what it was issued with.
        raise PermissionError_(
            "'x-bill-to' applies to callers authenticated with an OIDC token. An issued "
            "API key bills the group it pins, or your default; mint a key for the group "
            "you mean, or change your default billing group."
        )
    # Annotated because mypy reads the variable's type from the first branch:
    # a standing key may pin nothing (`billing_group` is nullable), and
    # `resolve_billing_group` takes None as "fall back to the default". Found
    # while verifying the Jina backend against a cold mypy cache — the live
    # checkout's warm one had been answering green.
    pinned: Group | None
    if requested is not None:
        pinned = _pin_from_header(api_key.user, requested)
    else:
        pinned = api_key.billing_group
    group = resolve_billing_group(api_key.user, pinned=pinned)
    await _touch_last_used(session, api_key)
    return Principal(user=api_key.user, billing_group=group, api_key=api_key)


async def get_optional_principal(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Principal | None:
    """Authenticate a ``/v1`` caller if it presented a credential; ``None`` if not.

    Exists for the one route that stays reachable with no credential at all —
    ``GET /v1/models`` (ADR 0081) — so an anonymous client can see the catalogue
    before it has a key. Everywhere else keeps using :func:`get_principal`, which
    refuses a missing credential outright. A credential that *is* presented and
    does not check out still 401s here, by falling through to the same function:
    presenting a bad key is not the same as presenting none, and silently
    downgrading a rejected key to "anonymous" would hide the failure from a
    caller who thinks they are authenticated.
    """
    secret = parse_authorization_header(request.headers.get("authorization"))
    if not secret:
        secret = request.headers.get("api-key")
    if not secret:
        return None
    return await get_principal(request, session)


async def _authenticate_bearer_user(request: Request, session: AsyncSession, token: str) -> User:
    """Validate an OIDC access token and return the synced user.

    Split out of ``_bearer_principal`` (ADR 0093 to-do item 2) so
    ``get_authenticated_caller`` can share every step of this — signature,
    issuer routing, the sync that reconciles memberships — without also
    resolving a billing group, which ``/v1/me`` must not require.
    """
    settings: Settings = request.app.state.settings
    if not settings.oidc.access_token_audience:
        # Deliberately the same message a bad key gets. A deployment that does
        # not accept tokens should not confirm to a prober that it has an
        # identity provider at all.
        raise AuthenticationError("Invalid API key provided.")

    # Several providers may be configured (ADR 0051). The unverified `iss`
    # claim picks which one verifies — routing, not trusting: the signature
    # check against the chosen provider's keys is what decides anything.
    client, provider = await _bearer_client(request, session, token)
    if client is None:
        raise AuthenticationError("Invalid API key provided.")

    try:
        claims = await client.validate_access_token(token)
        # The same policy gate the browser login answers to (ADR 0048), folded
        # with this provider's own group claim — the mappings are the global
        # policy's alone now (ADR 0093 §3.4), so there is nothing of the
        # provider's left to fold in for them.
        resolver = getattr(request.app.state, "oidc_policy", None)
        policy = resolver.policy if resolver is not None else None
        from dataclasses import replace as _dc_replace

        if policy is not None and provider is not None:
            policy = _dc_replace(policy, groups_claim=provider.groups_claim)
        user = await sync_user_from_claims(
            session,
            claims=claims,
            settings=settings.oidc,
            policy=policy,
            # Free: `_bearer_client` already resolved the row to check the
            # signature, so honouring its group policy here costs no query.
            # Without it this path would reconcile on every request for a
            # provider the operator set to leave groups alone.
            group_sync=provider.group_sync if provider is not None else GroupSync.EVERY_LOGIN,
            group_source=provider.group_source if provider is not None else "claim",
            group_mappings=policy.group_mappings if policy is not None else None,
        )
    except OIDCError as exc:
        # Logged in full, returned as one word: the reason a token failed is
        # a map of the validator for anyone holding a forged one. WARNING, not
        # INFO: the audience-mismatch case (a client with no audience mapper
        # naming this gateway) fails every chat login with nothing but this
        # line to say why, and it must be visible without log-level hunting.
        logger.warning("access token rejected: %s", exc)
        raise AuthenticationError("Invalid API key provided.") from exc

    if not user.is_active:
        raise AuthenticationError("Invalid API key provided.")

    await session.commit()
    return user


async def _bearer_principal(request: Request, session: AsyncSession, token: str) -> Principal:
    """Authenticate a ``/v1`` caller holding an OIDC access token.

    Reached only for a credential shaped like a JWT, so an API key never pays
    for the signature work and a token never costs a database lookup on a prefix
    it does not have.

    The resulting principal is indistinguishable from a key-authenticated one
    apart from ``api_key`` being None, which is the point: quotas, model access,
    redaction scoping and the ledger all read the user and the group, and none of
    them needs to know which credential arrived.
    """
    user = await _authenticate_bearer_user(request, session, token)

    # The per-request choice (ADR 0061). Resolved after the sync above, so a
    # group the directory granted on *this* login is already billable — and
    # against the effective memberships, so one an administrator granted by hand
    # is too. Reading the token's `groups` claim instead would have refused
    # exactly the group a manual grant just added, which is the mistake
    # ADR 0057 records for `is_admin` and the default.
    requested = requested_billing_group(request)
    pinned = _pin_from_header(user, requested) if requested else None
    return Principal(user=user, billing_group=resolve_billing_group(user, pinned=pinned))


async def get_authenticated_caller(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AuthenticatedCaller:
    """Authenticate a ``/v1`` caller the same way :func:`get_principal` does,
    but never resolve a billing group (ADR 0093 to-do item 2).

    Exists only for ``/v1/me`` and ``/v1/me/identities``: every other ``/v1``
    route needs a group to charge and keeps using :func:`get_principal`, which
    is right to refuse a caller with none. ``x-bill-to`` still resolves the
    same pinned group :func:`get_principal` would report — a client reading
    ``/v1/me`` to show "what would this request bill" must see the same
    answer a metered request honouring the header would act on — it just
    never turns "nothing resolved" into a 403 the way ``resolve_billing_group``
    does; :func:`resolve_billing_group_or_none` answers ``None`` instead.
    """
    secret = parse_authorization_header(request.headers.get("authorization"))
    if not secret:
        secret = request.headers.get("api-key")
    if not secret:
        raise AuthenticationError(
            "You didn't provide an API key. Supply it in an Authorization header: "
            "'Authorization: Bearer <key>'."
        )

    if looks_like_jwt(secret):
        user = await _authenticate_bearer_user(request, session, secret)
        requested = requested_billing_group(request)
        pinned = _pin_from_header(user, requested) if requested else None
        return AuthenticatedCaller(
            user=user, billing_group=resolve_billing_group_or_none(user, pinned=pinned)
        )

    api_key = await resolve_api_key(session, secret)
    requested = requested_billing_group(request)
    if requested is not None and api_key.is_issued_key:
        # Same refusal `get_principal` gives an issued key naming `x-bill-to`
        # (see there for why): a credential's own pin is its answer, and this
        # route is not the place that decision gets a second opinion.
        raise PermissionError_(
            "'x-bill-to' applies to callers authenticated with an OIDC token. An issued "
            "API key bills the group it pins, or your default; mint a key for the group "
            "you mean, or change your default billing group."
        )
    pinned = (
        _pin_from_header(api_key.user, requested)
        if requested is not None
        else api_key.billing_group
    )
    await _touch_last_used(session, api_key)
    return AuthenticatedCaller(
        user=api_key.user,
        api_key=api_key,
        billing_group=resolve_billing_group_or_none(api_key.user, pinned=pinned),
    )


async def _bearer_client(
    request: Request, session: AsyncSession, token: str
) -> tuple[OIDCClient | None, Any | None]:
    """The provider whose keys must verify this token, routed by its issuer.

    The issuer is read from the token *without* verification — it is a routing
    label between configured providers, not a claim anyone is being asked to
    believe. An unknown issuer means no provider here mints tokens for it, and
    the answer is the same "invalid key" a bad signature gets.
    """
    import base64
    import json

    settings: Settings = request.app.state.settings
    registry = getattr(request.app.state, "oidc_providers", None)
    if registry is None:
        return None, None
    try:
        _header, payload, _sig = token.split(".")
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        issuer = claims.get("iss")
    except Exception:
        return None, None
    if not isinstance(issuer, str):
        return None, None

    for record in await list_providers(session, settings, registry._secrets, enabled_only=True):
        if record.issuer.rstrip("/") == issuer.rstrip("/"):
            origin = str(request.base_url).rstrip("/")
            return registry.client_for(record, origin), record
    return None, None


async def load_user_for_management(
    session: AsyncSession, user_id: uuid.UUID, *, issued_at: datetime
) -> User:
    stmt = select(User).where(User.id == user_id).options(selectinload(User.memberships))
    user = (await session.execute(stmt)).scalar_one_or_none()
    if user is None or not user.is_active:
        raise AuthenticationError("Session is no longer valid.")
    # ADR 0093 §9.1: a disable (or a merge) stamps `sessions_valid_after`,
    # and a token minted before that instant is exactly the one a disable
    # exists to end — no second query, since the comparison is against the
    # row this function already loaded. Break-glass deliberately does NOT
    # stamp it: it never demotes or disables anyone (§10), and bearer tokens
    # from the switched-away provider are refused elsewhere, by provider
    # enablement. Console cookies minted before a switch stay valid for
    # their 8 h TTL; rotate `SESSION_SECRET` if compromise is suspected.
    if user.sessions_valid_after is not None and issued_at < user.sessions_valid_after:
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
        claims = verify_session_token(token, secret=settings.session_secret.get_secret_value())
    except OIDCError as exc:
        raise AuthenticationError("Session is not valid or has expired.") from exc

    return await load_user_for_management(session, claims.user_id, issued_at=claims.issued_at)


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
OptionalPrincipalDep = Annotated[Principal | None, Depends(get_optional_principal)]
AuthenticatedCallerDep = Annotated[AuthenticatedCaller, Depends(get_authenticated_caller)]
QuotaDep = Annotated[QuotaEngine, Depends(get_quota_engine)]
RedactorDep = Annotated[Redactor, Depends(get_redactor)]
ProvidersDep = Annotated[ProviderRegistry, Depends(get_providers)]
SecretsDep = Annotated[SecretBox, Depends(get_secrets)]
ControlHttpDep = Annotated[Any, Depends(get_control_http)]
EstimatorDep = Annotated[TokenEstimator, Depends(get_estimator)]
