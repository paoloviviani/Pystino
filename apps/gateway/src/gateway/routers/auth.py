"""OIDC login for the management API.

The flow state (``state``, ``nonce``, PKCE verifier) is carried in a short-lived
signed cookie rather than in server-side storage, so that logging in works
identically across several gateway workers without a shared session store.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import OctKey
from joserfc.jwt import JWTClaimsRegistry
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select

from gateway.deps import ManagementUserDep, SessionDep, SettingsDep
from gateway.errors import AuthenticationError as _AuthnError
from gateway.errors import (
    BadRequestError,
    ServiceUnavailableError,
    TooManyRequestsError,
)
from gateway.login_throttle import LoginThrottle
from gateway.models import LocalCredential, RefreshCredential, User
from gateway.oidc import (
    OIDCClient,
    OIDCError,
    extract_groups,
    generate_pkce_pair,
    issue_session_token,
    provision_user,
)
from gateway.passwords import verify_and_rehash, verify_dummy
from gateway.security import generate_api_key
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


def _oidc_client(request: Request) -> OIDCClient:
    client = getattr(request.app.state, "oidc_client", None)
    if not isinstance(client, OIDCClient):
        raise ServiceUnavailableError(
            "OIDC is not configured. Set GATEWAY_OIDC__ENABLED=true and the "
            "issuer/client credentials."
        )
    return client


class LocalLoginRequest(BaseModel):
    # Not `EmailStr`: that would drag in the email-validator dependency to
    # police the syntax of a string whose only real test is whether it names
    # an account. An unknown or malformed address costs one dummy Argon2
    # verification and one "incorrect" answer, same as any wrong guess. The
    # check here exists so the obvious garbage is refused before that.
    email: str = Field(min_length=3, max_length=320)

    @field_validator("email")
    @classmethod
    def _looks_like_an_address(cls, value: str) -> str:
        if value.count("@") != 1 or not value.partition("@")[0]:
            raise ValueError("a sign-in name must be an email address")
        return value

    password: str = Field(min_length=1, max_length=1024)

    # Naming a client asks for a *machine credential* (ADR 0046) alongside the
    # browser session: the response gains a refresh credential that client can
    # exchange for short-lived `/v1` access keys. A login that names none is
    # the console's login, bit for bit. The pattern is a slug because the
    # client name ends up in key rows, audit lines and another service's
    # configuration; anything outside [a-z0-9-] is a naming accident.
    client: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")


class AuthMethods(BaseModel):
    """Which ways in this deployment offers. Drives the console's login page."""

    local: bool
    oidc: bool


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


def _login_throttle(request: Request) -> LoginThrottle:
    throttle: LoginThrottle | None = getattr(request.app.state, "login_throttle", None)
    if throttle is None:
        raise ServiceUnavailableError("Local sign-in is not enabled.")
    return throttle


async def _rotate_refresh_credential(
    session: Any, user: Any, client: str, settings: Any
) -> dict[str, Any]:
    """Mint the (user, client) refresh credential, replacing any previous one.

    ADR 0046. One row per ``(user, client)``, rotated at every login: the old
    credential dies with the row, which is right for a credential whose only
    holder *just authenticated with the password* — there is no concurrent
    legitimate holder to break, and a re-login that left the old value working
    would make "sign in again" a no-op for anyone who copied it.

    The lifetime is the session TTL, because the credential exists to back a
    client session and nothing longer. Returns the response payload: the
    secret, shown once, and the identity the client stores beside it.
    """
    await session.execute(
        delete(RefreshCredential).where(
            RefreshCredential.user_id == user.id, RefreshCredential.client == client
        )
    )
    generated = generate_api_key(environment_prefix="gwr")
    row = RefreshCredential(
        user_id=user.id,
        client=client,
        prefix=generated.prefix,
        secret_hash=generated.key_hash,
        expires_at=utcnow() + timedelta(seconds=settings.session_ttl_seconds),
    )
    session.add(row)
    await session.commit()
    return {
        "refresh_token": generated.secret,
        "email": user.email,
        "display_name": user.display_name,
        "groups": [membership.group.name for membership in user.memberships],
        "is_admin": user.is_admin,
    }


@router.get("/login")
async def login(
    request: Request, settings: SettingsDep, next: str | None = None
) -> RedirectResponse:
    """Start the flow, remembering where the browser was trying to go.

    ``next`` lets a deep link survive signing in: following a bookmark to a
    quota rule should end at that rule, not at the overview with the reader
    navigating back to where they already were.
    """
    client = _oidc_client(request)
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
async def methods(request: Request) -> AuthMethods:
    """Which sign-in methods this deployment offers.

    Unauthenticated by design: the console must ask *before* it can show a
    login page, and a 401 there would loop it back to redirecting. What this
    reveals — whether local login or OIDC is on — is already public the moment
    anyone visits ``/auth/login``, so hiding it would protect nothing.
    """
    return AuthMethods(
        local=bool(getattr(request.app.state, "login_throttle", None)),
        oidc=getattr(request.app.state, "oidc_client", None) is not None,
    )


@router.post("/login")
async def local_login(
    payload: LocalLoginRequest,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
) -> JSONResponse:
    """Sign in with email and password, when local auth is enabled (ADR 0043).

    Three properties worth keeping in mind while reading the failure paths:

    * **One answer for every failure.** Unknown address, wrong password and a
      disabled account all return the same message and status. Distinguishing
      them would turn this endpoint into an account enumerator.
    * **Every failure costs the same work.** An unknown address runs a real
      Argon2 verification against a dummy hash, so timing does not reveal
      whether the address exists before a single password is guessed.
    * **Failures are counted.** See `login_throttle.py` for why the counter is
      per process rather than shared.
    """
    throttle = _login_throttle(request)
    email = payload.email.casefold()

    if not throttle.allowed(email):
        # 429, and deliberately no Retry-After: naming the window tells an
        # attacker exactly how long to wait between guesses.
        raise TooManyRequestsError("Too many failed sign-in attempts. Try again later.")

    # The query is by the credential, not the identity: `users.email` is not
    # unique, and a local account is exactly the row whose issuer is "local".
    row = (
        await session.execute(
            select(LocalCredential, User)
            .join(User, User.id == LocalCredential.user_id)
            .where(User.issuer == "local", User.subject == email)
        )
    ).first()

    if row is not None:
        credential, user = row
        valid, replacement = verify_and_rehash(payload.password, credential.password_hash)
    else:
        valid = False
        # Not free, on purpose — see the docstring.
        verify_dummy(payload.password)
        replacement = None

    if not valid or row is None:
        throttle.record_failure(email)
        raise _AuthnError("Incorrect email or password.", code="invalid_credentials")

    if not user.is_active:
        # The disabled case burns no Argon2 work beyond the verification above,
        # which already ran; that is acceptable, since a disabled account's
        # existence was admin action, not an attacker's discovery.
        raise _AuthnError("Incorrect email or password.", code="invalid_credentials")

    if replacement is not None:
        # Parameters moved on since this hash was made. The login holding the
        # plaintext is the one moment an upgrade is free.
        credential.password_hash = replacement

    user.last_login_at = utcnow()
    await session.commit()
    throttle.record_success(email)

    logger.info("local login: user=%s email=%s", user.id, email)
    body: dict[str, Any] = {
        "status": "ok",
        "user_id": str(user.id),
        "default_billing_group_id": (
            str(user.default_billing_group_id) if user.default_billing_group_id else None
        ),
    }
    if payload.client is not None:
        # ADR 0046: this login is also a machine credential mint. The browser
        # session below is unchanged — chat-api calls this endpoint
        # server-side, and a caller that named a client gets the refresh
        # credential in the body, shown once, plus the identity a client
        # session needs so it never has to parse the session cookie.
        body.update(await _rotate_refresh_credential(session, user, payload.client, settings))
        logger.info(
            "local login minted refresh credential: user=%s client=%s", user.id, payload.client
        )
    response = JSONResponse(body)
    # The same session the OIDC callback issues — that equivalence is the
    # whole point; nothing downstream knows which way in the person came.
    _set_session_cookie(response, request, user.id)
    return response


@router.get("/callback")
async def callback(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> Response:
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

    client = _oidc_client(request)
    try:
        tokens = await client.exchange_code(code, str(flow["cv"]))
        id_token = tokens.get("id_token")
        if not isinstance(id_token, str):
            raise OIDCError("the token response contained no id_token")
        claims = await client.validate_id_token(id_token, nonce=str(flow["nonce"]))

        # Groups may live only on userinfo, depending on the provider.
        merged: dict[str, Any] = dict(claims)
        if settings.oidc.fetch_userinfo and isinstance(
            access_token := tokens.get("access_token"), str
        ):
            merged.update(await client.fetch_userinfo(access_token))
    except OIDCError as exc:
        logger.warning("OIDC login failed: %s", exc)
        raise BadRequestError("Sign-in failed. Please try again.") from exc

    groups = extract_groups(merged, settings.oidc)
    user = await provision_user(
        session,
        issuer=str(claims["iss"]),
        subject=str(claims["sub"]),
        email=merged.get("email"),
        display_name=merged.get("name") or merged.get("preferred_username"),
        group_names=groups,
        settings=settings.oidc,
    )
    await session.commit()

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


@router.post("/logout")
async def logout(request: Request, settings: SettingsDep) -> JSONResponse:
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

    ``end_session_endpoint`` is optional in the spec. When a provider does not
    publish one, ``redirect_to`` is null and the caller falls back to signing
    in again — our session is still gone, which is as much as we can do.
    """
    redirect_to: str | None = None
    console_mounted = bool(getattr(request.app.state, "console_mounted", False))
    client = getattr(request.app.state, "oidc_client", None)
    if isinstance(client, OIDCClient):
        try:
            metadata = await client.metadata()
        except OIDCError as exc:
            # Never fatal: failing to reach the provider must not leave someone
            # unable to drop their session here.
            logger.warning("could not read discovery for logout: %s", exc)
        else:
            if metadata.end_session_endpoint:
                # Built from the origin this request actually arrived on, so it
                # is the origin whose post-logout URI is registered with the
                # provider. Hard-coding it would break the moment the stack is
                # reached on the overlay address instead of localhost.
                origin = str(request.base_url).rstrip("/")
                landing = f"{origin}{_DEFAULT_LANDING}" if console_mounted else origin
                parameters = {"post_logout_redirect_uri": landing}

                # With the hint, the provider knows which session to end and
                # does it. Without one it cannot, and Keycloak stops to ask —
                # correctly, since otherwise any page able to navigate a
                # browser here could sign people out. `client_id` is what makes
                # the request resolvable at all in that case.
                hint = request.cookies.get(_HINT_COOKIE)
                if hint:
                    parameters["id_token_hint"] = hint
                else:
                    parameters["client_id"] = settings.oidc.client_id

                redirect_to = f"{metadata.end_session_endpoint}?{urlencode(parameters)}"

    response = JSONResponse({"status": "ok", "redirect_to": redirect_to})
    response.delete_cookie(settings.session_cookie_name)
    # Same path it was set with, or the browser keeps it.
    response.delete_cookie(_HINT_COOKIE, path="/auth")
    return response


@router.get("/session")
async def whoami(user: ManagementUserDep) -> dict[str, Any]:
    """Cheap check that a session cookie is still good."""
    return {"user_id": str(user.id), "email": user.email, "is_admin": user.is_admin}
