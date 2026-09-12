"""The house issuer's HTTP surface (ADR 0068).

Three reachability classes pass through this router, and the split is the
security property, so it is worth naming where each lives:

* **Browser-facing** — ``/oauth/authorize``, ``/oauth/end_session``, and
  discovery itself. Public at the origin, exactly as the console's login page
  is; protected by PKCE, one-time codes, state, and the registered-client
  check.
* **Server-to-server** — ``/oauth/token``, whose URL discovery advertises at
  the *internal* base so a standards client follows it inside the compose
  network; guarded additionally by client authentication, which is the
  credential only the deployment's services hold.
* **Published keys** — ``/oauth/jwks.json``, which is public material by
  definition: a JWKS is what lets the world verify, not what lets it in.

The OAuth error responses here are deliberately *not* the gateway's error
envelope: an OAuth client parses ``error``/``error_description`` per RFC 6749
§5.2, and a client that cannot parse the refusal retries forever.
"""

from __future__ import annotations

import base64
import logging
import secrets
from datetime import timedelta
from typing import Any, cast
from urllib.parse import quote, unquote, urlencode

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from gateway.config import Settings
from gateway.deps import SessionDep, SettingsDep
from gateway.errors import BadRequestError, ServiceUnavailableError
from gateway.idp import (
    IdpSigner,
    client_by_id,
    discovery_document,
    hash_code,
    id_token_claims,
    mint_code,
    pkce_s256,
    safe_post_logout_redirect,
    user_claims_from_key,
    validate_id_token_hint,
)
from gateway.models import ApiKey, IdpAuthorizationCode, RefreshCredential, User
from gateway.oidc import OIDCError, verify_session_token
from gateway.routers.auth import _HINT_COOKIE
from gateway.security import extract_prefix, generate_api_key, verify_api_key
from gateway.types import utcnow

logger = logging.getLogger(__name__)

router = APIRouter(tags=["idp"])

#: The internal credential family prefix for the IdP's refresh credentials —
#: namespaced under the client so revoking an IdP session never touches a
#: credential a legacy direct login minted, and the other way round.
_REFRESH_PREFIX = "gwr"
_ACCESS_PREFIX = "gwa"

#: An access key minted by a token exchange lives this long — the same figure
#: ``routers.tokens`` chose, for the same reason: longer than a streamed turn,
#: short enough that the sweep keeps one row per concurrent exchange.
ACCESS_TTL_SECONDS = 900


def _signer(request: Request) -> IdpSigner:
    signer: IdpSigner | None = getattr(request.app.state, "idp_signer", None)
    if signer is None:  # only when the router is mounted without its wiring
        raise ServiceUnavailableError("The IdP is not enabled on this deployment.")
    return signer


def _oauth_error(
    error: str, description: str, redirect_uri: str | None, state: str | None
) -> Response:
    """An OAuth-level failure: redirected to the client when we trust it, JSON when not.

    Errors *before* the client and redirect are validated must not redirect —
    the redirect target is exactly what failed validation, and sending an
    error there is how an open redirect dresses up as spec compliance. After
    that, the RFC's way is the redirect, because a browser flow's client
    handles an error page better than a JSON body it never planned for.
    """
    if redirect_uri is None:
        raise BadRequestError(f"{error}: {description}")
    parameters: dict[str, str] = {"error": error, "error_description": description}
    if state:
        parameters["state"] = state
    return RedirectResponse(f"{redirect_uri}?{urlencode(parameters)}", status_code=302)


async def _session_user(request: Request, session: AsyncSession) -> User | None:
    """The user behind the management cookie, or ``None`` — never a raise.

    ``/authorize`` is a redirect flow: an unauthenticated browser must be
    *sent to sign in*, not shown a 401 body. This is the soft half of the
    session check ``deps`` does strictly for the management API.
    """
    settings: Settings = request.app.state.settings
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        return None
    try:
        user_id = verify_session_token(token, secret=settings.session_secret.get_secret_value())
    except OIDCError:
        return None
    user = (
        await session.execute(select(User).where(User.id == user_id, User.is_active.is_(True)))
    ).scalar_one_or_none()
    return user


@router.get("/.well-known/openid-configuration")
async def openid_configuration(settings: SettingsDep) -> dict[str, Any]:
    return discovery_document(settings.idp)


@router.get("/oauth/jwks.json")
async def jwks(request: Request) -> JSONResponse:
    return JSONResponse(_signer(request).public_jwks())


@router.get("/oauth/authorize")
async def authorize(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    response_type: str | None = None,
    client_id: str | None = None,
    redirect_uri: str | None = None,
    scope: str | None = None,
    state: str | None = None,
    nonce: str | None = None,
    code_challenge: str | None = None,
    code_challenge_method: str | None = None,
) -> Response:
    """The authorization endpoint: one redirect that ends in a code.

    The client and its redirect are validated before anything else, because
    every other failure is reported *by redirecting there* — the one rule
    that keeps this endpoint from becoming an open redirect that dresses up
    as spec compliance.
    """
    client = client_by_id(settings.idp, client_id)
    if client is None:
        raise BadRequestError("invalid_client: the client is not registered here.")
    # The origin is the *configured* issuer, never the request's — the same
    # rule discovery follows, and for the same reason: behind a plain-HTTP
    # edge the hop arrives as http (or with whatever Host the edge preserved),
    # while every client knows this deployment as the https issuer URL. A
    # redirect that validated against the request's vantage point would be
    # refused at the edge and accepted on the host, which is backwards.
    expected = f"{settings.idp.issuer}{client.redirect_path}"
    if redirect_uri != expected:
        # Logged rather than redirected: the mismatch is the interesting fact.
        logger.warning(
            "idp authorize: redirect_uri mismatch client=%s presented=%r expected=%r",
            client_id,
            redirect_uri,
            expected,
        )
        raise BadRequestError("invalid_request: the redirect URI is not registered.")

    if response_type != "code":
        return _oauth_error(
            "unsupported_response_type", "only 'code' is supported", redirect_uri, state
        )
    if not code_challenge or code_challenge_method != "S256":
        return _oauth_error("invalid_request", "PKCE with S256 is required", redirect_uri, state)
    if not state:
        # A client that cannot check state cannot tell its own flow from one
        # an attacker started, and CSRF on a login is a session fixation.
        return _oauth_error("invalid_request", "state is required", redirect_uri, state)

    user = await _session_user(request, session)
    if user is None:
        if not getattr(request.app.state, "console_mounted", False):
            # Nowhere to sign in: the flow cannot proceed and must say so here,
            # because the browser has nowhere else this deployment owns.
            raise ServiceUnavailableError(
                "Sign-in requires a session. This deployment serves no console to establish one."
            )
        # Into the console's login, and back: `next` survives the journey
        # (ADR 0043), and the URL to return to is this one, query included.
        # Percent-encoded whole, because it is a value inside someone else's
        # query string and a raw `&` in it would end it early.
        here = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(
            f"/console/login?next={quote(here, safe='')}",
            status_code=302,
        )

    code, code_hash = mint_code()
    session.add(
        IdpAuthorizationCode(
            code_hash=code_hash,
            client_id=client.client_id,
            redirect_uri=redirect_uri,
            user_id=user.id,
            nonce=nonce or "",
            code_challenge=code_challenge,
            scope=scope or "",
            expires_at=utcnow() + timedelta(seconds=settings.idp.code_ttl_seconds),
        )
    )
    await session.commit()
    logger.info("idp authorize: code minted user=%s client=%s", user.id, client.client_id)
    return RedirectResponse(
        f"{redirect_uri}?{urlencode({'code': code, 'state': state})}", status_code=302
    )


def _client_authentication(
    request: Request,
    body_client_id: str | None,
    body_client_secret: str | None,
) -> tuple[Any, str]:
    """Resolve the authenticated client, or the OAuth refusal for it.

    HTTP Basic is what ``openid-client`` sends for a confidential client; the
    post-body form is the other spell the RFC allows. Both parts are
    form-urlencoded before the colon per RFC 6749 §2.3.1, which almost nobody
    remembers until a client_id with a special character fails for no reason
    anyone can see.
    """
    settings: Settings = request.app.state.settings
    client_id: str | None = body_client_id
    client_secret: str | None = body_client_secret
    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        try:
            raw = base64.b64decode(header[6:]).decode("utf-8")
            client_id, client_secret = (unquote(part) for part in raw.split(":", 1))
        except (ValueError, UnicodeDecodeError) as exc:
            raise BadRequestError("invalid_client: malformed Basic authorization header.") from exc
    client = client_by_id(settings.idp, client_id)
    if client is None:
        raise BadRequestError("invalid_client: the client is not registered here.")
    expected = client.secret.get_secret_value()
    if expected and (not client_secret or not secrets.compare_digest(client_secret, expected)):
        raise BadRequestError("invalid_client: client authentication failed.")
    # The resolved id, never the raw parameter: the client row is what was
    # authenticated, and every credential minted below is bound to it.
    return client, client.client_id


def _no_grant(error: str, description: str) -> JSONResponse:
    logger.info("idp token refused: %s (%s)", error, description)
    return JSONResponse({"error": error, "error_description": description}, status_code=400)


async def _load_user_with_groups(session: AsyncSession, user_id: Any) -> User | None:
    return (
        await session.execute(
            select(User)
            .where(User.id == user_id, User.is_active.is_(True))
            .options(selectinload(User.memberships))
        )
    ).scalar_one_or_none()


async def _mint_credentials(
    session: AsyncSession,
    *,
    user: User,
    client_id: str,
    settings: Settings,
    nonce: str,
    scope: str,
    signer: IdpSigner,
) -> dict[str, Any]:
    """The token response body: opaque access key, rotated refresh credential, id_token.

    One family per ``(user, client)``, rotated here as ADR 0046 rotates it at
    login: the holder *just* proved themselves, so no concurrent legitimate
    holder exists to break. On the refresh grant this is what keeps the flow
    alive at all — the secret is stored hashed, so a refresh that returned
    nothing would leave the client with no way to refresh again, and the
    session would die one access TTL later.
    """
    family = f"idp-{client_id}"
    now = utcnow()
    await session.execute(
        delete(RefreshCredential).where(
            RefreshCredential.user_id == user.id, RefreshCredential.client == family
        )
    )
    refresh = generate_api_key(environment_prefix=_REFRESH_PREFIX)
    session.add(
        RefreshCredential(
            user_id=user.id,
            client=family,
            prefix=refresh.prefix,
            secret_hash=refresh.key_hash,
            expires_at=now + timedelta(seconds=settings.session_ttl_seconds),
        )
    )
    # Sweep this family's dead access rows — never the live ones, which a
    # concurrent turn may already be holding (routers.tokens, same reasoning).
    await session.execute(
        delete(ApiKey).where(
            ApiKey.user_id == user.id,
            ApiKey.minted_by == family,
            ApiKey.expires_at <= now,
        )
    )
    access = generate_api_key(environment_prefix=_ACCESS_PREFIX)
    session.add(
        ApiKey(
            user_id=user.id,
            prefix=access.prefix,
            key_hash=access.key_hash,
            name=f"idp:{client_id}",
            minted_by=family,
            billing_group_id=None,
            expires_at=now + timedelta(seconds=ACCESS_TTL_SECONDS),
        )
    )
    token = signer.sign_id_token(
        id_token_claims(
            settings=settings.idp,
            client_id=client_id,
            user=user,
            nonce=nonce,
            scope=scope,
            now=now.timestamp(),
            ttl_seconds=settings.idp.id_token_ttl_seconds,
        )
    )
    await session.commit()
    return {
        "access_token": access.secret,
        "token_type": "bearer",
        "expires_in": ACCESS_TTL_SECONDS,
        "refresh_token": refresh.secret,
        "id_token": token,
        "scope": scope or "openid",
    }


@router.post("/oauth/token")
async def token(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    grant_type: str | None = Form(default=None),
    code: str | None = Form(default=None),
    redirect_uri: str | None = Form(default=None),
    client_id: str | None = Form(default=None),
    client_secret: str | None = Form(default=None),
    code_verifier: str | None = Form(default=None),
    refresh_token: str | None = Form(default=None),
) -> JSONResponse:
    """The token endpoint: authorization codes and refresh tokens in, credentials out.

    The minted access key is an ordinary ``ApiKey`` row, so everything ``/v1``
    already does — revocation, disabled users, TTL, the pinned query budget —
    arrives with it unasked. There is no new validation on ``/v1``; there is
    no validation on ``/v1`` at all.
    """
    signer = _signer(request)
    _client, authenticated_id = _client_authentication(request, client_id, client_secret)

    if grant_type == "authorization_code":
        if not code or not redirect_uri or not code_verifier:
            return _no_grant("invalid_request", "code, redirect_uri and code_verifier are required")
        row = (
            await session.execute(
                select(IdpAuthorizationCode).where(
                    IdpAuthorizationCode.code_hash == hash_code(code)
                )
            )
        ).scalar_one_or_none()
        now = utcnow()
        if row is None or row.client_id != authenticated_id or row.redirect_uri != redirect_uri:
            return _no_grant("invalid_grant", "the code is not valid for this client and redirect")
        if row.used_at is not None or row.expires_at <= now:
            return _no_grant("invalid_grant", "the code has expired or was already used")
        if pkce_s256(code_verifier) != row.code_challenge:
            return _no_grant("invalid_grant", "the PKCE verifier does not match the challenge")
        # Single-use is a claim, not a hope: the conditional update makes two
        # racers for one code one winner and one refusal.
        spent = cast(
            "CursorResult[Any]",
            await session.execute(
                update(IdpAuthorizationCode)
                .where(IdpAuthorizationCode.id == row.id, IdpAuthorizationCode.used_at.is_(None))
                .values(used_at=now)
            ),
        )
        if spent.rowcount != 1:
            await session.rollback()
            return _no_grant("invalid_grant", "the code has expired or was already used")
        user = await _load_user_with_groups(session, row.user_id)
        if user is None:
            await session.rollback()
            return _no_grant("invalid_grant", "the code has expired or was already used")
        body = await _mint_credentials(
            session,
            user=user,
            client_id=authenticated_id,
            settings=settings,
            nonce=row.nonce,
            scope=row.scope,
            signer=signer,
        )
        logger.info("idp token: code exchanged user=%s client=%s", user.id, authenticated_id)
        return JSONResponse(body)

    if grant_type == "refresh_token":
        if not refresh_token:
            return _no_grant("invalid_request", "refresh_token is required")
        prefix = extract_prefix(refresh_token)
        refresh_row: RefreshCredential | None = None
        if prefix is not None:
            refresh_row = (
                await session.execute(
                    select(RefreshCredential).where(RefreshCredential.prefix == prefix)
                )
            ).scalar_one_or_none()
        if refresh_row is None or not verify_api_key(refresh_token, refresh_row.secret_hash):
            return _no_grant("invalid_grant", "the refresh token is not valid")
        if refresh_row.expires_at <= utcnow():
            return _no_grant("invalid_grant", "the refresh token has expired")
        family = f"idp-{authenticated_id}"
        if refresh_row.client != family:
            # A credential minted for one client (or by a legacy direct login)
            # does not become another's by being presented there.
            return _no_grant("invalid_grant", "the refresh token is not valid for this client")
        user = await _load_user_with_groups(session, refresh_row.user_id)
        if user is None:
            return _no_grant("invalid_grant", "the refresh token is not valid")
        body = await _mint_credentials(
            session,
            user=user,
            client_id=authenticated_id,
            settings=settings,
            nonce="",
            scope="",
            signer=signer,
        )
        logger.info("idp token: refreshed user=%s client=%s", user.id, authenticated_id)
        return JSONResponse(body)

    return _no_grant("unsupported_grant_type", "authorization_code and refresh_token are supported")


@router.get("/oauth/userinfo")
async def userinfo(request: Request, session: SessionDep) -> JSONResponse:
    """The identity claims an access token names — reached with the opaque key.

    The bearer is resolved through the same lookup ``/v1`` uses, so a key that
    would be refused there is refused here, and one that works there works
    here. A JWT presented instead is refused: the ``id_token`` is an identity
    document for the *client* to read, not a credential for this endpoint.
    """
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.startswith("Bearer ") else ""
    if not token:
        raise BadRequestError("invalid_token: a bearer access token is required.")
    if token.count(".") == 2:
        # The one JWT-shaped credential this issuer mints is the id_token, and
        # it is not an API credential — same rule, same reason as ADR 0040's.
        raise BadRequestError("invalid_token: an ID token is not an API credential.")
    prefix = extract_prefix(token)
    api_key: ApiKey | None = None
    if prefix is not None:
        api_key = (
            await session.execute(select(ApiKey).where(ApiKey.prefix == prefix))
        ).scalar_one_or_none()
    if api_key is None or not verify_api_key(token, api_key.key_hash) or not api_key.is_usable():
        raise BadRequestError("invalid_token: the access token is not valid.")
    user = await _load_user_with_groups(session, api_key.user_id)
    if user is None:
        raise BadRequestError("invalid_token: the access token is not valid.")
    return JSONResponse(user_claims_from_key(user))


@router.api_route("/oauth/end_session", methods=["GET", "POST"])
async def end_session(
    request: Request,
    settings: SettingsDep,
    id_token_hint: str | None = None,
    post_logout_redirect_uri: str | None = None,
    state: str | None = None,
) -> Response:
    """RP-initiated logout: the session goes here, and the browser goes home.

    The session is ended *unconditionally* — the person asked for it, and the
    hint only tells the issuer which client is asking, so an unverifiable hint
    downgrades the request to "sign out locally", never to "refuse".
    """
    # The hint's only current use is the log line: which client asked, when it
    # can be verified. Everything else it could say, the cookie already says.
    if id_token_hint:
        claims = validate_id_token_hint(id_token_hint, _signer(request))
        if claims:
            logger.info("idp end_session: hint verified for sub=%s", claims.get("sub"))
        else:
            logger.info("idp end_session: hint did not verify; ending the session anyway")

    target = safe_post_logout_redirect(post_logout_redirect_uri, settings.idp.issuer)
    if target and state:
        separator = "&" if "?" in target else "?"
        target = f"{target}{separator}{urlencode({'state': state})}"
    if not target and getattr(request.app.state, "console_mounted", False):
        target = "/"

    response: Response = (
        RedirectResponse(target, status_code=302) if target else JSONResponse({"status": "ok"})
    )
    response.delete_cookie(settings.session_cookie_name)
    # Same path the callback set it with, or the browser keeps it.
    response.delete_cookie(_HINT_COOKIE, path="/auth")
    return response
