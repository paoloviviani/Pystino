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

from gateway.deps import ManagementUserDep, SessionDep, SettingsDep
from gateway.errors import BadRequestError, ServiceUnavailableError
from gateway.oidc import (
    OIDCClient,
    OIDCError,
    extract_groups,
    generate_pkce_pair,
    issue_session_token,
    provision_user,
)
from gateway.types import utcnow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_FLOW_COOKIE = "gw_login"
_FLOW_TTL_SECONDS = 600

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
    response.set_cookie(
        settings.session_cookie_name,
        issue_session_token(
            user.id,
            secret=settings.session_secret.get_secret_value(),
            ttl_seconds=settings.session_ttl_seconds,
        ),
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )
    response.delete_cookie(_FLOW_COOKIE)
    return response


@router.post("/logout")
async def logout(settings: SettingsDep) -> JSONResponse:
    response = JSONResponse({"status": "ok"})
    response.delete_cookie(settings.session_cookie_name)
    return response


@router.get("/session")
async def whoami(user: ManagementUserDep) -> dict[str, Any]:
    """Cheap check that a session cookie is still good."""
    return {"user_id": str(user.id), "email": user.email, "is_admin": user.is_admin}
