"""OIDC authorization-code login for the management API.

Scope: this authenticates *humans* to the management surface. Programmatic
``/v1`` traffic uses API keys, because a key is revocable server-side and carries
a billing group, and because no chat client knows how to refresh an OIDC token.

Three things here are configurable because every identity provider differs:

* **Which claim holds groups.** Keycloak uses ``realm_access.roles``, Entra ID
  uses ``groups``, others use a namespaced URI. Dotted paths walk nested objects;
  a literal dot in a claim name is escaped as ``\\.``.
* **Whether groups come from the ID token or userinfo.** Several providers omit
  groups from the ID token to keep it small.
* **Whether unknown groups are created.** Auto-creation is convenient; turning it
  off makes group membership an explicit administrative act.

The device authorization flow that the ``opencode`` bootstrap needs is Phase 4 and
is deliberately not started here.
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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from gateway.config import OIDCSettings
from gateway.models import Group, GroupSource, Membership, User
from gateway.types import utcnow

logger = logging.getLogger(__name__)

# Only asymmetric algorithms are accepted for ID tokens. Permitting HS256 here
# would let an attacker who learns the (non-secret) client_id forge tokens.
_ID_TOKEN_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "PS256"]
_SESSION_ALGORITHM = "HS256"


class OIDCError(Exception):
    """Any failure in the login flow. Never surfaced verbatim to the browser."""


@dataclass(frozen=True, slots=True)
class OIDCMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    userinfo_endpoint: str | None = None
    # Present when the provider supports the device flow; recorded now because
    # Phase 4 will need it and discovery is already being parsed.
    device_authorization_endpoint: str | None = None
    # RP-initiated logout. Optional in the spec, so a provider without it means
    # the gateway can only drop its own session — see `logout`.
    end_session_endpoint: str | None = None


def generate_pkce_pair() -> tuple[str, str]:
    """Return ``(verifier, challenge)`` for PKCE S256.

    PKCE is used even though this is a confidential client: it costs nothing and
    removes the authorization-code interception class of attack entirely.
    """
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def split_claim_path(path: str) -> list[str]:
    """Split a dotted claim path, honouring ``\\.`` as a literal dot."""
    parts: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(path):
        char = path[index]
        if char == "\\" and index + 1 < len(path) and path[index + 1] == ".":
            current.append(".")
            index += 2
            continue
        if char == ".":
            parts.append("".join(current))
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    parts.append("".join(current))
    return [part for part in parts if part]


def resolve_claim(claims: dict[str, Any], path: str) -> Any:
    """Fetch a possibly nested claim.

    The full path is tried as a flat key first, because namespaced claim names
    such as ``https://example.org/groups`` contain dots that are not nesting.
    """
    if path in claims:
        return claims[path]

    current: Any = claims
    for part in split_claim_path(path):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def normalise_groups(value: Any) -> list[str]:
    """Coerce a groups claim into a list of names.

    A bare string is treated as a *single* group rather than split on whitespace
    or commas. Splitting would be guesswork, and group names containing spaces are
    common in directory systems — inventing two groups out of one would silently
    grant or deny the wrong access.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list | tuple | set):
        names: list[str] = []
        for item in value:
            if isinstance(item, str):
                if item.strip():
                    names.append(item)
            elif isinstance(item, dict):
                # Some providers send objects; take the first recognisable label.
                for key in ("name", "displayName", "display_name", "value", "id"):
                    if isinstance(label := item.get(key), str) and label.strip():
                        names.append(label)
                        break
        return names
    return []


def extract_groups(claims: dict[str, Any], settings: OIDCSettings) -> list[str]:
    """Group names for a login, after the configured allowlist is applied."""
    names = normalise_groups(resolve_claim(claims, settings.groups_claim))
    if settings.group_allowlist:
        allowed = set(settings.group_allowlist)
        names = [name for name in names if name in allowed]
    # Deduplicate, preserving order for stable logs.
    return list(dict.fromkeys(names))


class OIDCClient:
    """Discovery, token exchange and ID token validation."""

    def __init__(self, settings: OIDCSettings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http
        self._metadata: OIDCMetadata | None = None
        self._jwks: KeySet | None = None
        self._jwks_fetched_at: float = 0.0

    async def metadata(self) -> OIDCMetadata:
        if self._metadata is not None:
            return self._metadata
        url = f"{self._settings.issuer}/.well-known/openid-configuration"
        try:
            response = await self._http.get(url)
            response.raise_for_status()
            document = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OIDCError(f"could not fetch OIDC discovery document from {url}: {exc}") from exc

        try:
            self._metadata = OIDCMetadata(
                issuer=document["issuer"],
                authorization_endpoint=document["authorization_endpoint"],
                token_endpoint=document["token_endpoint"],
                jwks_uri=document["jwks_uri"],
                userinfo_endpoint=document.get("userinfo_endpoint"),
                device_authorization_endpoint=document.get("device_authorization_endpoint"),
                end_session_endpoint=document.get("end_session_endpoint"),
            )
        except KeyError as exc:
            raise OIDCError(f"discovery document is missing {exc}") from exc
        return self._metadata

    async def jwks(self, *, force: bool = False) -> KeySet:
        """Signing keys, cached.

        Refetched when a token names a ``kid`` we do not hold, which is how key
        rotation is meant to be handled: providers rotate without warning.
        """
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
                # Serving with a slightly stale key set beats failing every login
                # because the provider's JWKS endpoint blipped.
                logger.warning("JWKS refresh failed, using cached keys: %s", exc)
                return self._jwks
            raise OIDCError(f"could not fetch JWKS: {exc}") from exc

        self._jwks_fetched_at = now
        return self._jwks

    async def build_authorization_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        metadata = await self.metadata()
        params = httpx.QueryParams(
            {
                "response_type": "code",
                "client_id": self._settings.client_id,
                "redirect_uri": self._settings.redirect_uri,
                "scope": " ".join(self._settings.scopes),
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        separator = "&" if "?" in metadata.authorization_endpoint else "?"
        return f"{metadata.authorization_endpoint}{separator}{params}"

    async def exchange_code(self, code: str, code_verifier: str) -> dict[str, Any]:
        metadata = await self.metadata()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._settings.redirect_uri,
            "client_id": self._settings.client_id,
            "code_verifier": code_verifier,
        }
        if secret := self._settings.client_secret.get_secret_value():
            data["client_secret"] = secret

        try:
            response = await self._http.post(metadata.token_endpoint, data=data)
        except httpx.HTTPError as exc:
            raise OIDCError(f"token endpoint unreachable: {exc}") from exc

        if response.status_code >= 400:
            # Provider error bodies can contain the code; log, do not propagate.
            logger.warning("token exchange failed: %s %s", response.status_code, response.text)
            raise OIDCError("token exchange was rejected by the identity provider")

        try:
            payload = response.json()
        except ValueError as exc:
            raise OIDCError("token endpoint returned a non-JSON body") from exc
        if not isinstance(payload, dict):
            raise OIDCError("token endpoint returned an unexpected body")
        return payload

    async def validate_id_token(self, id_token: str, *, nonce: str) -> dict[str, Any]:
        """Verify signature, issuer, audience, expiry and nonce."""
        metadata = await self.metadata()
        keys = await self.jwks()

        try:
            token = jwt.decode(id_token, keys, algorithms=_ID_TOKEN_ALGORITHMS)
        except JoseError:
            # Most likely an unknown kid after a rotation; refetch once.
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

        # The nonce binds this token to the browser session that started the
        # flow, defeating token replay into someone else's login.
        if nonce and token.claims.get("nonce") != nonce:
            raise OIDCError("ID token nonce does not match the login attempt")

        return dict(token.claims)

    async def fetch_userinfo(self, access_token: str) -> dict[str, Any]:
        metadata = await self.metadata()
        if not metadata.userinfo_endpoint:
            return {}
        try:
            response = await self._http.get(
                metadata.userinfo_endpoint,
                headers={"authorization": f"Bearer {access_token}"},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Not fatal: the ID token may already carry everything needed.
            logger.warning("userinfo request failed: %s", exc)
            return {}
        return payload if isinstance(payload, dict) else {}


async def provision_user(
    session: AsyncSession,
    *,
    issuer: str,
    subject: str,
    email: str | None,
    display_name: str | None,
    group_names: list[str],
    settings: OIDCSettings,
) -> User:
    """Create or update a user and reconcile their group memberships.

    Group membership is **replaced**, not merged: the identity provider is
    authoritative, so a group removed there must disappear here, or revoking
    someone's access in the directory would not revoke their ability to bill.

    Groups created manually in the gateway are also removed if the IdP does not
    report them, which is a deliberate consequence of that same rule — mixing
    authoritative and local membership silently produces access nobody intended.
    """
    stmt = (
        select(User)
        .where(User.issuer == issuer, User.subject == subject)
        .options(selectinload(User.memberships))
    )
    user = (await session.execute(stmt)).scalar_one_or_none()

    if user is None:
        user = User(issuer=issuer, subject=subject, email=email, display_name=display_name)
        session.add(user)
        await session.flush()
    else:
        # Refresh mutable profile fields on every login.
        if email is not None:
            user.email = email
        if display_name is not None:
            user.display_name = display_name

    user.last_login_at = utcnow()

    groups = await _resolve_groups(session, group_names, settings)
    await _reconcile_memberships(session, user, groups)

    # Order matters. Clear a default the user can no longer bill *first*, so that
    # the "sole group becomes the default" rule can then adopt the group they do
    # still have. Doing it the other way round leaves a single-group user with no
    # default at all, and therefore unable to make a request until they call the
    # management API.
    # Admin follows group membership when configured, in both directions. Left
    # unconfigured, the flag is never touched here and stays a manual decision —
    # which is what keeps `gateway seed`'s local admin usable.
    if settings.admin_groups:
        held = {group.name for group in groups}
        user.is_admin = bool(held & set(settings.admin_groups))

    valid_group_ids = {group.id for group in groups}
    if (
        user.default_billing_group_id is not None
        and user.default_billing_group_id not in valid_group_ids
    ):
        user.default_billing_group_id = None
    if user.default_billing_group_id is None and len(groups) == 1:
        user.default_billing_group_id = groups[0].id

    await session.flush()
    # Reload the relationship so callers see the reconciled membership set rather
    # than whatever was loaded before it changed.
    await session.refresh(user, attribute_names=["memberships"])
    return user


async def _resolve_groups(
    session: AsyncSession, names: list[str], settings: OIDCSettings
) -> list[Group]:
    if not names:
        return []
    existing = list(
        (await session.execute(select(Group).where(Group.name.in_(names)))).scalars().all()
    )
    by_name = {group.name: group for group in existing}

    if settings.auto_create_groups:
        for name in names:
            if name not in by_name:
                group = Group(name=name, source=GroupSource.OIDC)
                session.add(group)
                by_name[name] = group
        await session.flush()

    return [by_name[name] for name in names if name in by_name and by_name[name].is_active]


async def _reconcile_memberships(session: AsyncSession, user: User, groups: list[Group]) -> None:
    """Make the stored memberships match *groups* exactly.

    Memberships are queried explicitly rather than read off
    ``user.memberships``. Touching an unloaded relationship from async code
    triggers a lazy load in a synchronous context, which raises
    ``MissingGreenlet`` — and it would do so on the *first login of every new
    user*, which is the least convenient place to discover it.
    """
    desired = {group.id for group in groups}
    existing = list(
        (await session.execute(select(Membership).where(Membership.user_id == user.id)))
        .scalars()
        .all()
    )

    for membership in existing:
        if membership.group_id not in desired:
            await session.delete(membership)

    already_present = {
        membership.group_id for membership in existing if membership.group_id in desired
    }
    for group in groups:
        if group.id not in already_present:
            session.add(Membership(user_id=user.id, group_id=group.id))

    await session.flush()


# -- management session cookies --------------------------------------------


def issue_session_token(user_id: uuid.UUID, *, secret: str, ttl_seconds: int) -> str:
    """Mint a signed, expiring session token.

    Stateless by design: there is no server-side session table to grow, and
    revocation is handled by deactivating the user, which is re-checked on every
    request anyway.
    """
    if not secret:
        raise OIDCError("GATEWAY_SESSION_SECRET is not configured")
    now = int(utcnow().timestamp())
    claims = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + ttl_seconds,
        "typ": "gw-session",
    }
    key = OctKey.import_key(secret)
    return jwt.encode({"alg": _SESSION_ALGORITHM}, claims, key)


def verify_session_token(token: str, *, secret: str) -> uuid.UUID:
    """Return the user id, or raise :class:`OIDCError`."""
    if not secret:
        raise OIDCError("GATEWAY_SESSION_SECRET is not configured")
    key = OctKey.import_key(secret)
    try:
        decoded = jwt.decode(token, key, algorithms=[_SESSION_ALGORITHM])
        JWTClaimsRegistry(
            exp={"essential": True},
            sub={"essential": True},
            typ={"essential": True, "value": "gw-session"},
        ).validate(decoded.claims)
    except JoseError as exc:
        raise OIDCError(f"session token is not valid: {exc}") from exc

    try:
        return uuid.UUID(str(decoded.claims["sub"]))
    except (KeyError, ValueError) as exc:
        raise OIDCError("session token subject is not a user id") from exc
