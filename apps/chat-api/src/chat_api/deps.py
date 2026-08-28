"""Request-scoped dependencies: the signed-in person, and a token to act as them."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from chat_api.config import Settings
from chat_api.gateway_client import GatewayClient
from chat_api.models import Session
from chat_api.oidc import OIDCClient, OIDCError, read_session_cookie
from chat_api.secrets import SecretBox, SecretDecryptionError
from chat_api.types import utcnow

logger = logging.getLogger(__name__)

SESSION_COOKIE = "chat_session"


@dataclass(slots=True)
class Caller:
    """The person this request is for, and a live token to act as them."""

    session: Session
    access_token: str

    @property
    def issuer(self) -> str:
        return self.session.issuer

    @property
    def subject(self) -> str:
        return self.session.subject


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as db:
        yield db


def get_gateway(request: Request) -> GatewayClient:
    client: GatewayClient = request.app.state.gateway_client
    return client


def get_oidc(request: Request) -> OIDCClient:
    client: OIDCClient = request.app.state.oidc_client
    return client


def get_secrets(request: Request) -> SecretBox:
    box: SecretBox = request.app.state.secret_box
    return box


def _unauthenticated() -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Sign in to continue.")


async def get_caller(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    oidc: Annotated[OIDCClient, Depends(get_oidc)],
    box: Annotated[SecretBox, Depends(get_secrets)],
) -> Caller:
    """Resolve the cookie to a session, and mint a fresh access token from it.

    A token is fetched per request rather than cached because caching it means
    holding a bearer credential in memory keyed by session, and the refresh call
    is one round trip to Keycloak on a path that is already making a round trip
    to the gateway. When that measurement stops holding — it will, once tool
    loops make several gateway calls per turn — the cache belongs here, keyed by
    session id with the token's own expiry, and nowhere else.
    """
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        raise _unauthenticated()
    try:
        session_id = read_session_cookie(cookie, secret=settings.session_secret.get_secret_value())
    except OIDCError as exc:
        raise _unauthenticated() from exc

    session = await db.get(Session, session_id)
    if session is None or session.expires_at <= utcnow():
        raise _unauthenticated()
    if not session.refresh_token_encrypted:
        raise _unauthenticated()

    try:
        refresh_token = box.decrypt(session.refresh_token_encrypted)
        access_token, next_refresh = await oidc.refresh(refresh_token)
    except (OIDCError, SecretDecryptionError) as exc:
        # The session is over: the provider retired the refresh token, or the
        # key that encrypted it is gone. Deleting the row is the honest
        # response — leaving it means every request retries a login that
        # cannot succeed.
        logger.info("session %s ended: %s", session_id, exc)
        await db.delete(session)
        await db.commit()
        raise _unauthenticated() from exc

    if next_refresh and next_refresh != refresh_token:
        session.refresh_token_encrypted = box.encrypt(next_refresh)
    session.last_seen_at = utcnow()
    await db.commit()

    return Caller(session=session, access_token=access_token)


CallerDep = Annotated[Caller, Depends(get_caller)]
DbDep = Annotated[AsyncSession, Depends(get_db)]
GatewayDep = Annotated[GatewayClient, Depends(get_gateway)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
