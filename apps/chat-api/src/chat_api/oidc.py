"""Signing a person in, and keeping a token to call the gateway with.

Two halves, and the second is the one that makes this service work at all:

* the **authorization-code flow**, which ends in a row in ``sessions`` and a
  signed cookie naming it;
* **getting a fresh access token** for each gateway call, from the refresh token
  that row holds.

The second is why sessions are stored server-side. An access token lives about
five minutes; a conversation lasts longer than that, so something has to refresh,
and the only credential that can is a refresh token — which is precisely the
credential that must not sit in a browser.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet, OctKey
from joserfc.jwt import JWTClaimsRegistry

from chat_api.config import OIDCSettings
from chat_api.types import utcnow

logger = logging.getLogger(__name__)

_ID_TOKEN_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "PS256"]
_SESSION_ALGORITHM = "HS256"


class OIDCError(Exception):
    """Any failure in the login flow. Never surfaced verbatim to the browser."""


@dataclass(frozen=True, slots=True)
class Metadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    end_session_endpoint: str | None = None


@dataclass(frozen=True, slots=True)
class Identity:
    """Who signed in, and the tokens that prove it."""

    issuer: str
    subject: str
    email: str | None
    display_name: str | None
    groups: list[str]
    access_token: str
    refresh_token: str | None


def generate_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _groups_from(claims: dict[str, Any]) -> list[str]:
    """The realm puts them in ``groups``; anything else is not our business.

    The gateway's group claim is configurable because it must work against
    whatever identity provider a deployment has. This service reads groups only
    to decide what to *show* — the "Gateway console" link, and later which
    shared assistants are visible — and the gateway re-reads them from the token
    for every decision that matters. So a wrong answer here is a cosmetic bug,
    not an access-control one, and it does not earn a configuration knob.
    """
    value = claims.get("groups")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


class OIDCClient:
    def __init__(self, settings: OIDCSettings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http
        self._metadata: Metadata | None = None
        self._jwks: KeySet | None = None
        self._jwks_fetched_at = 0.0

    async def metadata(self) -> Metadata:
        if self._metadata is not None:
            return self._metadata
        url = f"{self._settings.issuer}/.well-known/openid-configuration"
        try:
            response = await self._http.get(url)
            response.raise_for_status()
            document = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OIDCError(f"could not fetch OIDC discovery from {url}: {exc}") from exc
        try:
            self._metadata = Metadata(
                issuer=document["issuer"],
                authorization_endpoint=document["authorization_endpoint"],
                token_endpoint=document["token_endpoint"],
                jwks_uri=document["jwks_uri"],
                end_session_endpoint=document.get("end_session_endpoint"),
            )
        except KeyError as exc:
            raise OIDCError(f"discovery document is missing {exc}") from exc
        return self._metadata

    async def jwks(self, *, force: bool = False) -> KeySet:
        now = utcnow().timestamp()
        fresh = now - self._jwks_fetched_at < self._settings.jwks_cache_seconds
        if self._jwks is not None and fresh and not force:
            return self._jwks
        metadata = await self.metadata()
        try:
            response = await self._http.get(metadata.jwks_uri)
            response.raise_for_status()
            self._jwks = KeySet.import_key_set(response.json())
        except (httpx.HTTPError, ValueError, JoseError) as exc:
            if self._jwks is not None:
                logger.warning("JWKS refresh failed, using cached keys: %s", exc)
                return self._jwks
            raise OIDCError(f"could not fetch JWKS: {exc}") from exc
        self._jwks_fetched_at = now
        return self._jwks

    async def authorization_url(self, *, state: str, nonce: str, challenge: str) -> str:
        metadata = await self.metadata()
        params = httpx.QueryParams(
            {
                "response_type": "code",
                "client_id": self._settings.client_id,
                "redirect_uri": self._settings.redirect_uri,
                "scope": " ".join(self._settings.scopes),
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        separator = "&" if "?" in metadata.authorization_endpoint else "?"
        return f"{metadata.authorization_endpoint}{separator}{params}"

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        metadata = await self.metadata()
        data = dict(data, client_id=self._settings.client_id)
        if secret := self._settings.client_secret.get_secret_value():
            data["client_secret"] = secret
        try:
            response = await self._http.post(metadata.token_endpoint, data=data)
        except httpx.HTTPError as exc:
            raise OIDCError(f"token endpoint unreachable: {exc}") from exc
        if response.status_code >= 400:
            # Provider error bodies can echo the code or the refresh token.
            logger.warning("token request failed: %s", response.status_code)
            raise OIDCError("the identity provider rejected the token request")
        payload = response.json()
        if not isinstance(payload, dict):
            raise OIDCError("token endpoint returned an unexpected body")
        return payload

    async def exchange_code(self, code: str, verifier: str, *, nonce: str) -> Identity:
        payload = await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._settings.redirect_uri,
                "code_verifier": verifier,
            }
        )
        claims = await self._validate_id_token(payload.get("id_token", ""), nonce=nonce)
        return Identity(
            issuer=claims["iss"],
            subject=claims["sub"],
            email=claims.get("email"),
            display_name=claims.get("name") or claims.get("preferred_username"),
            groups=_groups_from(claims),
            access_token=payload.get("access_token", ""),
            refresh_token=payload.get("refresh_token"),
        )

    async def refresh(self, refresh_token: str) -> tuple[str, str | None]:
        """A fresh access token, and the refresh token to store next time.

        The realm may rotate refresh tokens — Keycloak does — so the second
        element is not decoration: dropping it means the next refresh fails with
        a token the provider has already retired, and the symptom is a session
        that works for exactly one token lifetime.
        """
        payload = await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )
        access = payload.get("access_token")
        if not access:
            raise OIDCError("refresh returned no access token")
        return access, payload.get("refresh_token") or refresh_token

    async def _validate_id_token(self, id_token: str, *, nonce: str) -> dict[str, Any]:
        if not id_token:
            raise OIDCError("the token response carried no ID token")
        metadata = await self.metadata()
        keys = await self.jwks()
        try:
            token = jwt.decode(id_token, keys, algorithms=_ID_TOKEN_ALGORITHMS)
        except JoseError:
            keys = await self.jwks(force=True)
            try:
                token = jwt.decode(id_token, keys, algorithms=_ID_TOKEN_ALGORITHMS)
            except JoseError as exc:
                raise OIDCError(f"ID token signature is not valid: {exc}") from exc

        registry = JWTClaimsRegistry(
            iss={"essential": True, "value": metadata.issuer},
            aud={"essential": True, "values": [self._settings.client_id]},
            exp={"essential": True},
            sub={"essential": True},
            leeway=self._settings.leeway_seconds,
        )
        try:
            registry.validate(token.claims)
        except JoseError as exc:
            raise OIDCError(f"ID token claims are not valid: {exc}") from exc
        # Same tolerance as the gateway's login (ADR 0044): a provider may omit
        # the nonce claim entirely — gitlab.com's discovery does — but a nonce
        # that is present and does not match this browser's flow is refused.
        # The chat-api stays a /v1 client and does not import the gateway's
        # code, so the rule is written twice on purpose.
        presented = token.claims.get("nonce")
        if presented is not None and presented != nonce:
            raise OIDCError("ID token nonce does not match the login attempt")
        return dict(token.claims)


def issue_session_cookie(session_id: uuid.UUID, *, secret: str, ttl_seconds: int) -> str:
    """A signed statement of which session row this browser holds.

    The cookie carries an id and nothing else. Everything about the person —
    and every credential — is in the row, so revoking a session is a DELETE
    rather than a hope that the browser cooperates.
    """
    now = int(utcnow().timestamp())
    claims = {"sid": str(session_id), "iat": now, "exp": now + ttl_seconds}
    return jwt.encode({"alg": _SESSION_ALGORITHM}, claims, OctKey.import_key(secret))


def read_session_cookie(token: str, *, secret: str) -> uuid.UUID:
    try:
        decoded = jwt.decode(token, OctKey.import_key(secret), algorithms=[_SESSION_ALGORITHM])
        JWTClaimsRegistry(exp={"essential": True}, sid={"essential": True}).validate(decoded.claims)
        return uuid.UUID(str(decoded.claims["sid"]))
    except (JoseError, ValueError, KeyError) as exc:
        raise OIDCError("session cookie is not valid") from exc
