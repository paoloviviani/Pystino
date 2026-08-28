"""Sign in, sign out, and who am I.

The flow is the gateway's, and it is written out again rather than imported for
the reason the whole service exists: this is a separate deployment with its own
client and its own session store. What is *not* duplicated is anything about
users, groups or spend — those stay the gateway's, and this service only ever
repeats what a token told it.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from typing import Annotated
from urllib.parse import parse_qs, urlencode, urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from chat_api.config import Settings
from chat_api.deps import (
    SESSION_COOKIE,
    CallerDep,
    DbDep,
    SettingsDep,
    get_db,
    get_oidc,
    get_secrets,
    get_settings,
)
from chat_api.models import Session
from chat_api.oidc import OIDCClient, OIDCError, generate_pkce_pair, issue_session_cookie
from chat_api.routers import MOUNT_PATH
from chat_api.schemas import Me
from chat_api.secrets import SecretBox
from chat_api.types import utcnow

logger = logging.getLogger(__name__)

router = APIRouter(prefix=MOUNT_PATH, tags=["auth"])

#: Holds the state, nonce and PKCE verifier between the redirect out and the
#: redirect back. Short-lived and per-browser, so a cookie is the right home —
#: it is the one piece of login state that must survive a round trip through
#: somebody else's website.
LOGIN_COOKIE = "chat_login"
_LOGIN_TTL_SECONDS = 600


def _safe_next(raw: str | None) -> str:
    """Only paths inside this application.

    An absolute URL here is an open redirect. Requiring the mount path as well
    is what stops a login on this service ending on the gateway's console with
    a session it did not issue.
    """
    if not raw or not raw.startswith(f"{MOUNT_PATH}/") or raw.startswith("//"):
        return f"{MOUNT_PATH}/"
    return raw


@router.get("/auth/login")
async def login(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    oidc: Annotated[OIDCClient, Depends(get_oidc)],
    next: str | None = None,
) -> RedirectResponse:
    verifier, challenge = generate_pkce_pair()
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    try:
        destination = await oidc.authorization_url(state=state, nonce=nonce, challenge=challenge)
    except OIDCError as exc:
        logger.warning("cannot start login: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Sign-in is unavailable.") from exc

    response = RedirectResponse(destination, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        LOGIN_COOKIE,
        urlencode({"s": state, "n": nonce, "v": verifier, "next": _safe_next(next)}),
        max_age=_LOGIN_TTL_SECONDS,
        httponly=True,
        secure=settings.session_cookie_secure,
        # Lax, not Strict: the browser arrives here from the identity provider,
        # and Strict would drop the cookie on exactly that navigation — a login
        # that works everywhere except at the end.
        samesite="lax",
        path=MOUNT_PATH,
    )
    return response


@router.get("/auth/callback")
async def callback(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    oidc: Annotated[OIDCClient, Depends(get_oidc)],
    box: Annotated[SecretBox, Depends(get_secrets)],
    code: str | None = None,
    state: str | None = None,
) -> Response:
    raw = request.cookies.get(LOGIN_COOKIE)
    if not raw or not code or not state:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No sign-in is in progress.")
    stored = {k: v[0] for k, v in parse_qs(raw).items()}
    if not secrets.compare_digest(stored.get("s", ""), state):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This sign-in did not start here.")

    try:
        identity = await oidc.exchange_code(code, stored.get("v", ""), nonce=stored.get("n", ""))
    except OIDCError as exc:
        logger.warning("login failed: %s", exc)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Sign-in failed.") from exc

    if not identity.refresh_token:
        # Without one there is nothing to refresh from, so the session would
        # die minutes after it began. Better to refuse now and say why than to
        # produce a session that mysteriously stops working.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "The identity provider issued no refresh token; the client needs one.",
        )

    session = Session(
        issuer=identity.issuer,
        subject=identity.subject,
        email=identity.email,
        display_name=identity.display_name,
        groups=identity.groups,
        is_admin="platform-admins" in identity.groups,
        refresh_token_encrypted=box.encrypt(identity.refresh_token),
        expires_at=utcnow() + timedelta(seconds=settings.session_ttl_seconds),
    )
    db.add(session)
    await db.commit()

    response = RedirectResponse(
        _safe_next(stored.get("next")), status_code=status.HTTP_303_SEE_OTHER
    )
    response.delete_cookie(LOGIN_COOKIE, path=MOUNT_PATH)
    response.set_cookie(
        SESSION_COOKIE,
        issue_session_cookie(
            session.id,
            secret=settings.session_secret.get_secret_value(),
            ttl_seconds=settings.session_ttl_seconds,
        ),
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        # Scoped to this application, not the origin. Behind the TLS proxy the
        # gateway shares the origin and has no business receiving a session
        # cookie it did not issue and cannot read.
        path=MOUNT_PATH,
    )
    return response


@router.post("/api/auth/logout")
async def logout(caller: CallerDep, db: DbDep) -> Response:
    """End the session here, and say where the provider's own logout is.

    Deleting the row is what makes this real: a cookie the browser is asked to
    forget is not a session that has ended.
    """
    await db.delete(caller.session)
    await db.commit()
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(SESSION_COOKIE, path=MOUNT_PATH)
    return response


@router.get("/api/me", response_model=Me)
async def me(caller: CallerDep, settings: SettingsDep) -> Me:
    return Me(
        subject=caller.subject,
        email=caller.session.email,
        display_name=caller.session.display_name,
        groups=list(caller.session.groups or []),
        is_admin=caller.session.is_admin,
        console_url=_console_url(settings),
    )


def _console_url(settings: Settings) -> str:
    """Where the gateway's console is, from the browser's point of view.

    Derived from the OIDC redirect URI rather than configured separately,
    because those two are the same origin in every deployment shape this
    project has — one https origin behind the proxy, or localhost ports in
    development — and a second variable is a second thing to get wrong.
    """
    if not settings.oidc.redirect_uri:
        return ""
    parsed = urlparse(settings.oidc.redirect_uri)
    return f"{parsed.scheme}://{parsed.netloc}/console"
