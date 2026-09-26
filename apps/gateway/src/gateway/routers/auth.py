"""OIDC login for the management API.

The flow state (``state``, ``nonce``, PKCE verifier) is carried in a short-lived
signed cookie rather than in server-side storage, so that logging in works
identically across several gateway workers without a shared session store.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import OctKey
from joserfc.jwt import JWTClaimsRegistry
from pydantic import BaseModel, Field

from gateway import identity_policy
from gateway.config import Settings
from gateway.deps import ManagementUserDep, SessionDep, SettingsDep
from gateway.directory.engine import link_at_login
from gateway.errors import (
    BadRequestError,
    ModelNotFoundError,
    ServiceUnavailableError,
)
from gateway.identity_registry import OIDCProviderRegistry, list_providers, provider_by_name
from gateway.oidc import (
    OIDCClient,
    OIDCError,
    ProvisioningRefused,
    extract_groups,
    generate_pkce_pair,
    issue_session_token,
    promote_bootstrap_admin,
    provision_user,
)
from gateway.types import utcnow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_FLOW_COOKIE = "gw_login"
_FLOW_TTL_SECONDS = 600

#: The ID token, kept only as `id_token_hint` for a one-click sign-out.
_HINT_COOKIE = "gw_idt"
#: Below the 4KB per-cookie limit with room for the name and attributes.
_MAX_HINT_BYTES = 3072

#: Where a browser lands after signing in, when it did not ask for anywhere.
_DEFAULT_LANDING = "/console"


def _safe_next(value: str | None) -> str | None:
    """A caller-supplied return path, or ``None`` if it is not obviously ours.

    This is the input to a redirect the browser follows *while holding a
    freshly minted session cookie*, which makes a permissive check here an open
    redirect: an attacker sends someone a login link with their own host in
    ``next``, the victim signs in for real, and lands on a page of the
    attacker's choosing that looks like it came from us.

    So the rule is a path on this origin and nothing else. Rejected rather than
    sanitised — a value we had to repair is a value we did not understand, and
    guessing at intent is how the interesting cases get through:

    * ``//evil.test`` and ``/\\evil.test`` are protocol-relative URLs, which
      browsers resolve to another host despite the leading slash;
    * anything with a scheme, even after whitespace or control characters,
      which some parsers strip before resolving;
    * ``\\r``/``\\n``, which can split a header on a less careful stack than
      this one.
    """
    if not value or not value.startswith("/"):
        return None
    if value.startswith("//") or value.startswith("/\\"):
        return None
    if any(character in value for character in "\r\n\t") or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in value
    ):
        return None
    return value


def _issue_flow_cookie(
    secret: str, *, state: str, nonce: str, verifier: str, next_path: str | None
) -> str:
    now = int(utcnow().timestamp())
    claims = {
        "state": state,
        "nonce": nonce,
        "cv": verifier,
        # Carried in the signed cookie rather than round-tripped through the
        # identity provider: it comes back to us tamper-evident, and it is
        # nobody else's business where this browser was headed.
        "nx": next_path,
        "iat": now,
        "exp": now + _FLOW_TTL_SECONDS,
        "typ": "gw-login",
    }
    return jwt.encode({"alg": "HS256"}, claims, OctKey.import_key(secret))


def _read_flow_cookie(token: str, secret: str) -> dict[str, Any]:
    try:
        decoded = jwt.decode(token, OctKey.import_key(secret), algorithms=["HS256"])
        JWTClaimsRegistry(
            exp={"essential": True},
            typ={"essential": True, "value": "gw-login"},
        ).validate(decoded.claims)
    except JoseError as exc:
        raise BadRequestError("The login attempt has expired. Try again.") from exc
    return dict(decoded.claims)


async def _resolve_provider_client(
    request: Request, session: SessionDep, provider_name: str | None
) -> tuple[OIDCClient, Any]:
    """The client for the named provider, or for the only one there is.

    A deployment with exactly one provider should not have to name it in a
    URL — that was the whole shape of OIDC here before there could be two.
    With several, the name is required, because guessing which directory a
    person means is not a thing a login can do quietly.
    """
    settings: Settings = request.app.state.settings
    registry: OIDCProviderRegistry | None = getattr(request.app.state, "oidc_providers", None)
    if registry is None:
        raise ServiceUnavailableError(
            "OIDC is not configured. Set GATEWAY_OIDC__ENABLED=true and the "
            "issuer/client credentials, or add a provider in the Settings screen."
        )
    origin = str(request.base_url).rstrip("/")
    if provider_name is None:
        providers = await list_providers(session, settings, registry._secrets, enabled_only=True)
        if len(providers) == 1:
            return registry.client_for(providers[0], origin), providers[0]
        if len(providers) > 1:
            raise BadRequestError(
                "Several identity providers are configured: choose one with ?provider=<name>."
            )
        raise ServiceUnavailableError("No identity provider is enabled.")
    record = await provider_by_name(session, settings, registry._secrets, provider_name)
    if record is None or not record.is_enabled:
        raise ModelNotFoundError(f"No identity provider named {provider_name!r}.")
    return registry.client_for(record, origin), record


class AuthProvider(BaseModel):
    """One enabled identity provider, as the login page needs it (ADR 0051).

    The name is what ``GET /auth/login?provider=`` takes, so it is the button's
    payload rather than only its label. The issuer is shown beside it, because
    with two directories configured "Sign in with SSO" twice tells a reader
    nothing about which door is theirs.
    """

    name: str
    issuer: str


class AuthMethods(BaseModel):
    """Which ways in this deployment offers. Drives the console's login page."""

    local: bool
    oidc: bool
    # Declared, and that is the whole of a bug worth naming. The handler has
    # always computed this list — a database query per call — and passed it as
    # `providers=`, and Pydantic dropped it on the way out because the model did
    # not declare it. No error anywhere: `/auth/methods` answered
    # `{"local": true, "oidc": true}`, the console read `providers` as
    # `undefined`, rendered zero buttons, and a deployment with a working
    # identity provider showed only the password form.
    #
    # It also hid a second fault. `Login.tsx` reads
    # `methods.data.providers.length` when local login is off, to skip its own
    # page for a single provider — which would have thrown on `undefined`. Local
    # auth being on is the only reason nobody saw it.
    providers: list[AuthProvider] = Field(default_factory=list)


def _set_session_cookie(response: Response, request: Request, user_id: Any) -> None:
    """Issue the management session, shared by the OIDC callback and local login.

    Both flows end in exactly the same credential — a signed ``gw-session``
    cookie — so nothing downstream of authentication needs to know which way in
    the person came.
    """
    settings: Any = request.app.state.settings
    response.set_cookie(
        settings.session_cookie_name,
        issue_session_token(
            user_id,
            secret=settings.session_secret.get_secret_value(),
            ttl_seconds=settings.session_ttl_seconds,
        ),
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )


@router.get("/login")
async def login(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    next: str | None = None,
    provider: str | None = None,
) -> RedirectResponse:
    """Start the flow against one identity provider, remembering where the
    browser was trying to go.

    ``next`` lets a deep link survive signing in: following a bookmark to a
    quota rule should end at that rule, not at the overview with the reader
    navigating back to where they already were. ``provider`` names the IdP —
    required when several are enabled, optional when there is exactly one.
    """
    client, _record = await _resolve_provider_client(request, session, provider)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier, challenge = generate_pkce_pair()

    try:
        url = await client.build_authorization_url(
            state=state, nonce=nonce, code_challenge=challenge
        )
    except OIDCError as exc:
        logger.error("could not start OIDC login: %s", exc)
        raise ServiceUnavailableError("The identity provider is unavailable.") from exc

    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        _FLOW_COOKIE,
        _issue_flow_cookie(
            settings.session_secret.get_secret_value(),
            state=state,
            nonce=nonce,
            verifier=verifier,
            next_path=_safe_next(next),
        ),
        max_age=_FLOW_TTL_SECONDS,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )
    return response


@router.get("/methods")
async def methods(request: Request, session: SessionDep, settings: SettingsDep) -> AuthMethods:
    """Which sign-in methods this deployment offers.

    Unauthenticated by design: the console must ask *before* it can show a
    login page, and a 401 there would loop it back to redirecting. What this
    reveals — whether local login is on and which identity providers exist —
    is already public the moment anyone visits ``/auth/login``, so hiding it
    would protect nothing.
    """
    providers: list[dict[str, str]] = []
    registry: OIDCProviderRegistry | None = getattr(request.app.state, "oidc_providers", None)
    if registry is not None:
        records = await list_providers(session, settings, registry._secrets, enabled_only=True)
        providers = [{"name": record.name, "issuer": record.issuer} for record in records]
    return AuthMethods(
        # The local-password door is gone (ADR 0088, D3); kept in the payload
        # as False so an older console renders no form rather than breaking.
        local=False,
        oidc=bool(providers),
        providers=providers,
    )


@router.get("/callback/{provider_name}")
async def callback(
    provider_name: str,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> Response:
    """The IdP lands here, namespaced per provider (ADR 0051): the redirect URI
    each provider registers is this path with its own name in it, so two
    directories cannot deliver a code to the wrong flow."""
    if error:
        raise BadRequestError(f"The identity provider returned an error: {error}")
    if not code or not state:
        raise BadRequestError("The callback is missing 'code' or 'state'.")

    flow_cookie = request.cookies.get(_FLOW_COOKIE)
    if not flow_cookie:
        raise BadRequestError("No login is in progress in this browser.")

    flow = _read_flow_cookie(flow_cookie, settings.session_secret.get_secret_value())

    # Constant-time not required: state is not a secret, it is an anti-CSRF nonce.
    if flow.get("state") != state:
        raise BadRequestError("The login state does not match. Start again.")

    client, record = await _resolve_provider_client(request, session, provider_name)
    # The policy in force, with this provider's dialect folded in: its group
    # claim and its IdP→local mappings. Provisioning decisions (auto-provision,
    # the unknown-user rule, admin groups) stay global — they answer "who may
    # exist here", not "how does this directory speak".
    global_policy = getattr(request.app.state, "oidc_policy", None)
    from dataclasses import replace as _dc_replace

    policy = (
        _dc_replace(
            global_policy.policy,
            groups_claim=record.groups_claim,
            group_mappings=record.mappings_dict(),
        )
        if global_policy is not None
        else None
    )
    try:
        tokens = await client.exchange_code(code, str(flow["cv"]))
        id_token = tokens.get("id_token")
        if not isinstance(id_token, str):
            raise OIDCError("the token response contained no id_token")
        claims = await client.validate_id_token(id_token, nonce=str(flow["nonce"]))

        # Groups may live only on userinfo, depending on the provider.
        merged: dict[str, Any] = dict(claims)
        if record.fetch_userinfo and isinstance(access_token := tokens.get("access_token"), str):
            merged.update(await client.fetch_userinfo(access_token))
    except OIDCError as exc:
        logger.warning("OIDC login failed: %s", exc)
        raise BadRequestError("Sign-in failed. Please try again.") from exc

    # Mapping first: from here on, the flow speaks local group names.
    groups = extract_groups(merged, settings.oidc, policy)
    if policy is not None:
        groups = policy.map_group_names(groups)
    try:
        user = await provision_user(
            session,
            issuer=str(claims["iss"]),
            subject=str(claims["sub"]),
            email=merged.get("email"),
            display_name=merged.get("name") or merged.get("preferred_username"),
            username=merged.get("preferred_username"),
            group_names=groups,
            settings=settings.oidc,
            policy=policy,
            # This provider's switch, and the claim that has to back it up
            # (ADR 0056). `merged` rather than `claims` on purpose: several
            # providers put `email_verified` on userinfo only, and reading it
            # from the ID token alone would decline every link they could
            # legitimately make.
            allow_local_link=record.link_by_email,
            # Passed raw, not coerced. OIDC core says this claim is a boolean;
            # a provider that sends the *string* "true" gets its link declined
            # and the value printed in the log, which is a five-second
            # diagnosis. Coercing it here would make the gate accept whatever
            # a directory happens to spell, and this is the one gate that
            # separates linking from "an email claim is a password".
            email_verified=merged.get("email_verified"),
            # How far this directory's answer about groups reaches (ADR 0057).
            group_sync=record.group_sync,
            # Where it comes from, and whether it decides admin (ADR 0088).
            group_source=record.group_source,
            claims=merged,
            group_mappings=record.mappings_dict(),
        )
        # A directory whose subjects are unknown until first login (Authelia)
        # links its mirrored entry now: pre-assigned groups and directory
        # groups apply at once, not at the next scheduled sync.
        if record.sync_adapter != "none" and getattr(record, "source", "") != "environment":
            await link_at_login(session, record, user, merged, settings=settings.oidc)
        # OIDC-only deployments have no password door to make the first
        # administrator through; the configured address, verified, is it.
        await promote_bootstrap_admin(
            session,
            user,
            bootstrap_email=settings.bootstrap_admin_email,
            email=merged.get("email"),
            email_verified=merged.get("email_verified"),
            kind=settings.oidc.kind,
        )
    except ProvisioningRefused as exc:
        # The policy's message is written for the person at the keyboard
        # ("ask an administrator"); the generic sign-in failure would bury it.
        await session.commit()
        raise BadRequestError(str(exc)) from exc
    await session.commit()

    # No session for a disabled account. An administrator turned this person
    # off (or provisioning created them disabled, ADR 0048, awaiting approval):
    # a login that lands on a console where every request answers 401 is not a
    # login, it is a maze. The commit above already landed — an account created
    # here exists as a disabled row an administrator can see and enable.
    if not user.is_active:
        raise BadRequestError(
            "This account is not enabled. Ask an administrator to enable it, then sign in again."
        )

    logger.info("oidc login: user=%s subject=%s groups=%s", user.id, user.subject, groups)

    # A browser ends up here, not an API client — it arrived by following a
    # redirect from the identity provider — so this ends the journey where the
    # reader was going. It used to answer with the JSON below, which left them
    # looking at `{"status":"ok",...}` in the address bar of what should have
    # been the console, with a working session and no sign of it.
    #
    # 303 rather than 302: the result of a completed exchange is a different
    # resource, and 303 says so without inviting a replay of this URL. The
    # authorization code is single-use, so a reload of the callback would fail.
    #
    # Headless deployments (`INCLUDE_CONSOLE=false`) keep the JSON: there is no
    # page to send anyone to, and something is driving this programmatically.
    landing = _safe_next(flow.get("nx")) or _DEFAULT_LANDING
    response: Response
    if getattr(request.app.state, "console_mounted", False):
        response = RedirectResponse(landing, status_code=303)
    else:
        response = JSONResponse(
            {
                "status": "ok",
                "user_id": str(user.id),
                "groups": groups,
                "default_billing_group_id": (
                    str(user.default_billing_group_id) if user.default_billing_group_id else None
                ),
            }
        )
    _set_session_cookie(response, request, user.id)
    # Kept solely to pass as `id_token_hint` when signing out. Without it
    # Keycloak cannot tell which session is being ended, so it interrupts with
    # a "Logging out?" confirmation page — safe, but an unfamiliar screen in
    # the middle of a one-click action.
    #
    # Scoped to /auth so it is not sent on every console API call, and skipped
    # entirely if it is large enough to risk the 4KB cookie limit: a realm with
    # many role claims could otherwise produce a cookie the browser silently
    # drops, and this must never be the reason a login fails. Losing it costs
    # the confirmation page and nothing else.
    if len(id_token) <= _MAX_HINT_BYTES:
        response.set_cookie(
            _HINT_COOKIE,
            id_token,
            max_age=settings.session_ttl_seconds,
            path="/auth",
            httponly=True,
            secure=settings.session_cookie_secure,
            samesite="lax",
        )
    response.delete_cookie(_FLOW_COOKIE)
    return response


async def _logout_client(request: Request, session: SessionDep) -> tuple[OIDCClient | None, Any]:
    """The provider whose session this logout should end, if any.

    Chosen by the ``iss`` of the id-token hint we stored at login, read
    **without** verification — the same routing-not-trusting move
    ``_bearer_client`` documents. Nothing is granted on the strength of it: a
    forged hint can at most send its own bearer to the wrong provider's logout
    page, and the local session is dropped either way.

    Falls back to the sole enabled provider when there is no usable hint, which
    is every single-provider deployment. With several and no hint there is no
    honest answer — signing someone out of a directory they did not use is not
    better than not trying — so the local cookie goes and that is all.
    """
    settings: Settings = request.app.state.settings
    registry: OIDCProviderRegistry | None = getattr(request.app.state, "oidc_providers", None)
    if registry is None:
        return None, None

    providers = await list_providers(session, settings, registry._secrets, enabled_only=True)
    if not providers:
        return None, None

    origin = str(request.base_url).rstrip("/")
    hint = request.cookies.get(_HINT_COOKIE)
    if hint and len(providers) > 1:
        issuer = _unverified_issuer(hint)
        if issuer is not None:
            for record in providers:
                if record.issuer.rstrip("/") == issuer.rstrip("/"):
                    return registry.client_for(record, origin), record
        # A hint naming nobody we know is not a reason to guess.
        return None, None

    if len(providers) == 1:
        return registry.client_for(providers[0], origin), providers[0]
    return None, None


def _unverified_issuer(token: str) -> str | None:
    """The ``iss`` claim of a JWT, without checking its signature."""
    import base64
    import json

    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        # A malformed hint is simply unusable, and it arrived from a cookie —
        # nothing here is worth failing a logout over.
        return None
    issuer = claims.get("iss")
    return issuer if isinstance(issuer, str) else None


@router.post("/logout")
async def logout(request: Request, session: SessionDep, settings: SettingsDep) -> JSONResponse:
    """End the session here **and** at the identity provider.

    Dropping our own cookie is not logging out. Keycloak keeps its own SSO
    session, so the next visit to ``/auth/login`` was answered without a
    password prompt and the reader arrived back on the console as the same
    person — signing out looked like it did nothing at all. On a shared machine
    that is a real problem, not a cosmetic one.

    So the cookie goes *and* the caller is handed the provider's
    ``end_session_endpoint`` to navigate to. Returned rather than redirected
    into, because this is a POST from `fetch` — following a 3xx would fetch
    Keycloak's page into JavaScript instead of taking the browser there. The
    console does a full page navigation with it.

    ``end_session_endpoint`` is optional in the spec, and the bundled Authelia
    (4.39) publishes none — so a null here used to mean the same silent
    half-logout. The provider's configured logout URL comes first, then
    discovery, then the kind's default (``identity_policy.logout_redirect``);
    null only when none of them names anything.
    """
    redirect_to: str | None = None
    console_mounted = bool(getattr(request.app.state, "console_mounted", False))
    # Resolved through the provider registry, because `app.state.oidc_client`
    # is set nowhere and has not been since ADR 0051 moved providers into rows.
    client, record = await _logout_client(request, session)
    if record is not None:
        end_session: str | None = None
        if isinstance(client, OIDCClient) and not record.logout_url.strip():
            try:
                end_session = (await client.metadata()).end_session_endpoint
            except OIDCError as exc:
                # Never fatal: failing to reach the provider must not leave
                # someone unable to drop their session here.
                logger.warning("could not read discovery for logout: %s", exc)
        # Built from the origin this request actually arrived on, so it is
        # the origin whose post-logout URI the provider knows.
        origin = str(request.base_url).rstrip("/")
        landing = f"{origin}{_DEFAULT_LANDING}" if console_mounted else origin
        redirect_to = identity_policy.logout_redirect(
            override=record.logout_url,
            end_session_endpoint=end_session,
            kind=record.kind,
            issuer=record.issuer,
            landing=landing,
            id_token_hint=request.cookies.get(_HINT_COOKIE),
            # The *provider's* client id, not the environment's: the env one
            # seeded the first row and may name a different provider.
            client_id=record.client_id,
        )

    response = JSONResponse({"status": "ok", "redirect_to": redirect_to})
    response.delete_cookie(settings.session_cookie_name)
    # Same path it was set with, or the browser keeps it.
    response.delete_cookie(_HINT_COOKIE, path="/auth")
    # The other applications on this origin (the chat): one sign-out, not a
    # console signed out beside a chat still signed in.
    for spec in settings.logout_also_clear_cookies:
        name, _, path = spec.partition(":")
        if name.strip():
            response.delete_cookie(name.strip(), path=path.strip() or "/")
    return response


@router.get("/session")
async def whoami(user: ManagementUserDep) -> dict[str, Any]:
    """Cheap check that a session cookie is still good."""
    return {"user_id": str(user.id), "email": user.email, "is_admin": user.is_admin}
