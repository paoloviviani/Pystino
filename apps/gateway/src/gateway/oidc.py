"""OIDC: authorization-code login for the management API, and bearer access
tokens for ``/v1`` when an audience is configured.

Two flows, deliberately separate. The **authorization-code** flow authenticates a
human to the management surface and ends in a session cookie. The **access
token** path authenticates a first-party application calling ``/v1`` on behalf of
the human using it — the chat app, and the ``opencode`` device flow — and is what
ADR 0040 exists for. The alternative was
minting an API key per user inside each application, which is credential storage
invented to work around a missing token path.

API keys remain the credential for programs acting as themselves: a key is
revocable server-side, carries a billing group, and does not expire in five
minutes.

Three things here are configurable because every identity provider differs:

* **Which claim holds groups.** Keycloak uses ``realm_access.roles``, Entra ID
  uses ``groups``, others use a namespaced URI. Dotted paths walk nested objects;
  a literal dot in a claim name is escaped as ``\\.``.
* **Whether groups come from the ID token or userinfo.** Several providers omit
  groups from the ID token to keep it small.
* **Whether unknown groups are created.** Auto-creation is convenient; turning it
  off makes group membership an explicit administrative act.

The device authorization flow itself is only a diagnostic tool's, so far
(``OIDCClient.start_device_flow``/``poll_device_token``, for
``pystino idp check --device``, ADR 0093 §11); the endpoint dance that gets
``opencode`` a token in the first place is still to come. What is here for
sign-in is the half that matters to the gateway either way: what to do with
the token once it exists.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet, OctKey
from joserfc.jwt import JWTClaimsRegistry
from sqlalchemy import Select, and_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from gateway.config import OIDCSettings, Settings
from gateway.deployment_state import get_or_create_deployment_state, mark_bootstrap_consumed
from gateway.email_normalize import is_trusted_email, normalize_email
from gateway.identity_events import record_event
from gateway.models import (
    DirectoryEntry,
    Group,
    GroupSource,
    GroupSync,
    IdentityEventAction,
    IdentityEventActor,
    IdentityProvider,
    Membership,
    MembershipSource,
    User,
    UserIdentity,
)
from gateway.oidc_policy import OIDCPolicy
from gateway.types import utcnow

logger = logging.getLogger(__name__)

# Only asymmetric algorithms are accepted for ID tokens. Permitting HS256 here
# would let an attacker who learns the (non-secret) client_id forge tokens.
_ID_TOKEN_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "PS256"]
_SESSION_ALGORITHM = "HS256"

# Keycloak marks an ID token `typ: ID` and an access token `typ: Bearer`. The
# claim is not standard, so its *absence* proves nothing and is allowed; its
# presence saying "ID" is refused. Without this, a deployment whose configured
# audience equals its own client_id would accept a console ID token as an API
# credential — and that token is handed to the browser.
_ID_TYP = "ID"


class OIDCError(Exception):
    """Any failure in the login flow. Never surfaced verbatim to the browser."""


class ProvisioningRefused(OIDCError):
    """A login the identity *policy* refused, not a technical failure.

    Distinct from its parent because the message is meant for the person
    typing their password: "ask an administrator" is actionable, while
    OIDCError's callers replace the text with a generic sign-in failure.
    """


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
    # RFC 7662, for providers whose access tokens are opaque (GitLab); see
    # `gateway.introspection`.
    introspection_endpoint: str | None = None


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


def extract_groups(
    claims: dict[str, Any], settings: OIDCSettings, policy: OIDCPolicy | None = None
) -> list[str]:
    """Group names for a login, after the configured allowlist is applied.

    The claim path is the policy's when one is in force (ADR 0048) — which
    claim names the groups is an operator decision now, and every IdP puts
    them somewhere different. The allowlist stays environmental: it filters
    what the IdP *reports*, which is plumbing, not meaning.
    """
    claim_path = policy.groups_claim if policy is not None else settings.groups_claim
    names = normalise_groups(resolve_claim(claims, claim_path))
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
        # ADR 0093 §2: a token with neither claim still passes on audience
        # alone, but it is worth one note per client, not one per request.
        self._warned_no_client_claim = False

    def _backchannel_headers(self) -> dict[str, str]:
        """Forwarded headers naming the public issuer, for internal-URL calls.

        Authelia derives its issuer — and so every endpoint in discovery and
        the `iss` of what it mints — from these headers; called on its internal
        address without them it answers nothing at all (verified against
        4.39.22). Empty when there is no internal URL, so an external IdP sees
        exactly the requests it always did.
        """
        if not self._settings.internal_base_url:
            return {}
        public = httpx.URL(self._settings.issuer)
        host = public.host if public.port is None else f"{public.host}:{public.port}"
        return {"x-forwarded-proto": public.scheme, "x-forwarded-host": host}

    def _backchannel_url(self, url: str | None) -> str | None:
        """Rewrite a public endpoint onto the internal base, when one is set.

        Only endpoints under the public issuer move. Anything else — an IdP
        that serves JWKS from a CDN — is left alone, because rewriting a URL we
        do not understand would send the request somewhere it was never meant
        to go.
        """
        internal = self._settings.internal_base_url.rstrip("/")
        public = self._settings.issuer.rstrip("/")
        if not url or not internal or not url.startswith(public):
            return url
        return internal + url[len(public) :]

    async def metadata(self) -> OIDCMetadata:
        if self._metadata is not None:
            return self._metadata
        base = (self._settings.internal_base_url or self._settings.issuer).rstrip("/")
        url = f"{base}/.well-known/openid-configuration"
        try:
            response = await self._http.get(url, headers=self._backchannel_headers())
            response.raise_for_status()
            document = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OIDCError(f"could not fetch OIDC discovery document from {url}: {exc}") from exc

        try:
            # The browser-facing endpoints (authorization, end_session) stay as
            # the IdP published them; only what this server calls itself moves
            # onto the internal URL.
            self._metadata = OIDCMetadata(
                issuer=document["issuer"],
                authorization_endpoint=document["authorization_endpoint"],
                token_endpoint=self._backchannel_url(document["token_endpoint"]) or "",
                jwks_uri=self._backchannel_url(document["jwks_uri"]) or "",
                userinfo_endpoint=self._backchannel_url(document.get("userinfo_endpoint")),
                device_authorization_endpoint=document.get("device_authorization_endpoint"),
                end_session_endpoint=document.get("end_session_endpoint"),
                introspection_endpoint=self._backchannel_url(
                    document.get("introspection_endpoint")
                ),
            )
        except KeyError as exc:
            raise OIDCError(f"discovery document is missing {exc}") from exc
        published = self._metadata.issuer.rstrip("/")
        if self._settings.internal_base_url and published != self._settings.issuer.rstrip("/"):
            # The one misconfiguration the forwarded headers can produce: the
            # IdP ignored them and answered as its internal self. Every token
            # would then fail the issuer check with a message about the token,
            # not about this, so say it here.
            raise OIDCError(
                f"discovery via {base} reports issuer {published!r}, not "
                f"{self._settings.issuer!r}: the IdP did not honour X-Forwarded-Host/Proto"
            )
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
            response = await self._http.get(metadata.jwks_uri, headers=self._backchannel_headers())
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
            response = await self._http.post(
                metadata.token_endpoint, data=data, headers=self._backchannel_headers()
            )
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
        #
        # Tolerated *absent*, deliberately (ADR 0044's GitLab finding):
        # gitlab.com's discovery document names no nonce claim in
        # `claims_supported`, so a provider we want to support demonstrably
        # does not put one in its tokens. Requiring it would lock every such
        # provider out. What is still refused, and this is the part that does
        # the work, is a token carrying a nonce that does not match this
        # browser's flow — an attacker replaying a token must present one
        # minted for a *different* nonce, and any nonce at all is caught.
        # What is lost is protection against a nonce-less stolen token, which
        # is exactly the protection such a provider never offered.
        presented = token.claims.get("nonce")
        if presented is not None and presented != nonce:
            raise OIDCError("ID token nonce does not match the login attempt")

        return dict(token.claims)

    async def validate_access_token(self, access_token: str) -> dict[str, Any]:
        """Verify an OIDC access token presented as a ``/v1`` credential.

        Four checks, and the reason each is here rather than inherited from
        :meth:`validate_id_token`, which validates a *different* kind of token:

        * **Signature**, against the realm's JWKS, refetched once on an unknown
          ``kid`` — key rotation happens without warning.
        * **Issuer**, which must equal the discovery document's exactly.
          ``iss`` is part of a user's identity here (users are keyed on
          ``(issuer, subject)``), so a token from a re-hosted Keycloak must fail
          rather than quietly provision a second set of everybody.
        * **Audience**, which must *contain* the configured value. An ID token
          names one audience; an access token may legitimately name several, so
          this is a membership test rather than equality.
        * **Not an ID token.** See ``_ID_TYP``.

        No userinfo request is made, unlike the login flow: this runs on every
        request, and an HTTP round trip to the identity provider per API call is
        not a thing a gateway may do. Whatever the token does not carry, the
        token does not carry.
        """
        audience = self._settings.access_token_audience
        if not audience:
            raise OIDCError("access tokens are not accepted: no audience is configured")

        metadata = await self.metadata()
        keys = await self.jwks()
        try:
            token = jwt.decode(access_token, keys, algorithms=_ID_TOKEN_ALGORITHMS)
        except JoseError:
            keys = await self.jwks(force=True)
            try:
                token = jwt.decode(access_token, keys, algorithms=_ID_TOKEN_ALGORITHMS)
            except JoseError as exc:
                raise OIDCError(f"access token signature is not valid: {exc}") from exc

        registry = JWTClaimsRegistry(
            iss={"essential": True, "value": metadata.issuer},
            exp={"essential": True},
            sub={"essential": True},
            leeway=self._settings.leeway_seconds,
        )
        try:
            registry.validate(token.claims)
        except JoseError as exc:
            raise OIDCError(f"access token claims are not valid: {exc}") from exc

        if token.claims.get("typ") == _ID_TYP:
            raise OIDCError("an ID token is not an API credential")

        # `aud` is a string or a list of strings, per RFC 7519.
        raw_audience = token.claims.get("aud")
        held = [raw_audience] if isinstance(raw_audience, str) else raw_audience or []
        if audience not in held:
            raise OIDCError(
                f"access token audience {held!r} does not include {audience!r}; "
                "the client needs an audience mapper naming this gateway"
            )

        self._check_accepted_client(token.claims)

        return dict(token.claims)

    def _check_accepted_client(self, claims: dict[str, Any]) -> None:
        """ADR 0093 §2: the audience check alone answers "for this gateway",
        never "asked for by a client we trust" — `aud` is a membership test,
        so any client an administrator gave an audience mapper can mint a
        token that passes it. `azp` (or, lacking that, `client_id`) is who
        actually asked, and it must be one of `ACCEPTED_CLIENTS` once that is
        configured; empty means the operator has not set it yet, the same
        escape hatch `GATEWAY_OIDC__ACCEPTED_CLIENTS`'s own startup check
        gives outside production.
        """
        accepted = self._settings.accepted_client_list()
        if not accepted:
            return
        client = claims.get("azp") or claims.get("client_id")
        if client is None:
            if not self._warned_no_client_claim:
                logger.warning(
                    "access tokens from %s carry neither azp nor client_id; "
                    "ACCEPTED_CLIENTS cannot be enforced for it, only the audience",
                    self._settings.issuer,
                )
                self._warned_no_client_claim = True
            return
        if client not in accepted:
            raise OIDCError(f"access token client {client!r} is not in ACCEPTED_CLIENTS")

    async def fetch_userinfo(self, access_token: str) -> dict[str, Any]:
        metadata = await self.metadata()
        if not metadata.userinfo_endpoint:
            return {}
        try:
            response = await self._http.get(
                metadata.userinfo_endpoint,
                headers={"authorization": f"Bearer {access_token}", **self._backchannel_headers()},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Not fatal: the ID token may already carry everything needed.
            logger.warning("userinfo request failed: %s", exc)
            return {}
        return payload if isinstance(payload, dict) else {}

    async def start_device_flow(self, *, client_id: str, scope: str = "openid") -> dict[str, Any]:
        """RFC 8628, first leg: get a ``device_code``/``user_code`` pair.

        The half of the device flow this module's own docstring said was
        "still to come" — ``pystino idp check --device`` (ADR 0093 §11) is
        the first caller, standing in for ``opencode``'s own enrollment until
        that lands.
        """
        metadata = await self.metadata()
        if not metadata.device_authorization_endpoint:
            raise OIDCError(
                "this identity provider does not publish a device_authorization_endpoint"
            )
        try:
            response = await self._http.post(
                metadata.device_authorization_endpoint,
                data={"client_id": client_id, "scope": scope},
                headers=self._backchannel_headers(),
            )
        except httpx.HTTPError as exc:
            raise OIDCError(f"device authorization endpoint unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise OIDCError(
                f"device authorization was refused: {response.status_code} {response.text}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise OIDCError("device authorization endpoint returned a non-JSON body") from exc
        if not isinstance(payload, dict):
            raise OIDCError("device authorization endpoint returned an unexpected body")
        for key in ("device_code", "user_code", "verification_uri"):
            if key not in payload:
                raise OIDCError(f"device authorization response is missing {key!r}")
        return payload

    async def poll_device_token(self, *, device_code: str, client_id: str) -> dict[str, Any]:
        """One poll of the token endpoint for a device grant.

        Exactly one request, so a caller can sleep and print progress between
        calls rather than being blocked inside this method for the whole
        wait. ``{"pending": "authorization_pending" | "slow_down"}`` is not an
        error — the operator has not finished signing in yet.
        """
        metadata = await self.metadata()
        data = {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
            "client_id": client_id,
        }
        try:
            response = await self._http.post(
                metadata.token_endpoint, data=data, headers=self._backchannel_headers()
            )
        except httpx.HTTPError as exc:
            raise OIDCError(f"token endpoint unreachable: {exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise OIDCError("token endpoint returned a non-JSON body") from exc
        if response.status_code >= 400:
            error = payload.get("error") if isinstance(payload, dict) else None
            if error in ("authorization_pending", "slow_down"):
                return {"pending": error}
            raise OIDCError(f"device grant failed: {error or response.text}")
        if not isinstance(payload, dict) or "access_token" not in payload:
            raise OIDCError("token endpoint returned an unexpected body")
        return payload


# A user created from the console before their first sign-in (ADR 0093 §3.1),
# with a placeholder `(issuer, subject)` until the bind at that person's first
# sign-in (§8.2) replaces it. Nothing creates one yet — that is stage (b) —
# but `other_active_admin_exists` below already excludes it, so that stage
# does not have to touch this function to stay correct, and it can never be
# made an administrator ahead of time (§5.4): an invite is not a grant.
PENDING_USER_ISSUER = "pystino:pending"

#: Serializes every last-admin guard against its peers until the surrounding
#: transaction commits, so concurrent demotes/disables cannot both pass the
#: check (ADR 0093 §5.5). Postgres only; arbitrary but fixed, and distinct
#: from the re-seed lock in `identity_registry` (93_002_001).
_LAST_ADMIN_ADVISORY_LOCK_KEY = 93_002_005


def _identity_select(issuer: str, subject: str) -> Select[tuple[User]]:
    """The one query that resolves ``(issuer, subject)`` to a person.

    Two places an identity can be recorded — the ``users`` row it was created
    with, and a ``user_identities`` row linking it to an account that already
    existed (ADR 0056) — resolved in a single statement rather than a lookup
    and a fallback. That is not tidiness: ``sync_user_from_claims`` runs this
    on the ``/v1`` request path, where ``test_query_counts.py`` bounds
    authentication at three round trips. A correlated subquery costs none of
    them; a second ``await session.execute`` would cost one on every request.
    """
    return (
        select(User)
        .where(
            or_(
                and_(User.issuer == issuer, User.subject == subject),
                User.id.in_(
                    select(UserIdentity.user_id).where(
                        UserIdentity.issuer == issuer, UserIdentity.subject == subject
                    )
                ),
            )
        )
        # `Membership.group` and `User.default_billing_group` are both
        # `lazy="joined"` on the model, so this loads everything the caller
        # needs. A lazy attribute touched later would raise MissingGreenlet
        # under asyncio rather than quietly costing a query.
        .options(selectinload(User.memberships))
    )


async def bind_bundled_login(
    session: AsyncSession,
    record: IdentityProvider,
    *,
    issuer: str,
    subject: str,
    preferred_username: str | None,
    email: str | None,
    email_verified: bool | None,
) -> None:
    """Bind an unknown ``(issuer, subject)`` to the console-created account its
    login was made for (ADR 0093 §8.2), bundled Authelia only.

    This replaces ``link_at_login``'s unlinked-entry path for this one kind:
    that function claims a ``directory_entries`` row with no ``user_id`` yet,
    which never describes a bundled entry — ``POST /admin/users`` and
    ``POST /admin/users/{id}/sign-in`` bind the row to a user (pending or
    real) at *creation* time (§8.2 step 4), not at the login that follows.
    What is unknown here is not "whose entry is this" but "does this
    signing-in identity belong to the user that entry already names".

    Call this **before** ``provision_user``, unconditionally — it runs its
    own ``_identity_select`` check and returns at once for an identity that
    is already known, so it is never wrong to call regardless of what the
    caller has or hasn't looked up yet. On a match it mutates the target
    user's identity — replacing the pending pair, or adding/updating a
    ``user_identities`` row — so ``provision_user``'s own ``_identity_select``
    then finds this same person instead of creating a new one. It writes
    nothing and returns quietly on every refusal: an unmatched login is an
    ordinary new user, not an error. The extra query costs nothing this path
    cares about — a browser login, not the ``/v1`` hot path
    ``test_query_counts.py`` bounds.
    """
    if record.kind != "authelia":
        return
    if (await session.execute(_identity_select(issuer, subject))).scalar_one_or_none() is not None:
        return
    if not preferred_username:
        return
    row = (
        await session.execute(
            select(DirectoryEntry).where(
                DirectoryEntry.provider_id == record.id,
                DirectoryEntry.external_id == preferred_username,
                DirectoryEntry.user_id.is_not(None),
            )
        )
    ).scalar_one_or_none()
    if row is None or row.user_id is None:
        return
    # §8.2: "when email_verified is True and a normalised email equals the
    # entry's" — the literal boolean, and both sides run through the same
    # normalisation before comparing (R2: exact-match comparison is how a
    # homoglyph or a casing difference silently fails to match).
    if email_verified is not True:
        return
    claim_email, claim_trusted = is_trusted_email(email or "")
    entry_email, entry_trusted = is_trusted_email(row.email or "")
    if not claim_trusted or not entry_trusted or claim_email != entry_email:
        return

    target = (
        await session.execute(select(User).where(User.id == row.user_id))
    ).scalar_one_or_none()
    if target is None:
        return

    if target.issuer == PENDING_USER_ISSUER:
        # The placeholder pair (§3.1) is replaced outright: it named nobody
        # real, so there is nothing to keep a secondary record of.
        target.issuer = issuer
        target.subject = subject
    else:
        # Otherwise the identity is added — except when the target already
        # holds one at *this* issuer (a lost `authelia-data` volume mints a
        # new opaque subject for the same login, one of §8.2's own named
        # cases): updating that identity's subject in place is what "the
        # same person, the same directory, a new subject" means, and
        # inserting a second row at the same issuer would violate
        # `uq_user_identities_user_issuer` anyway.
        existing = (
            await session.execute(
                select(UserIdentity).where(
                    UserIdentity.user_id == target.id, UserIdentity.issuer == issuer
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.subject = subject
            existing.matched_email = claim_email
        else:
            session.add(
                UserIdentity(
                    user_id=target.id,
                    issuer=issuer,
                    subject=subject,
                    matched_email=claim_email,
                )
            )
    await session.flush()

    await record_event(
        session,
        actor_type=IdentityEventActor.LOGIN,
        actor_label=email or preferred_username,
        action=IdentityEventAction.IDENTITY_BIND,
        target_user_id=target.id,
        target_label=email or preferred_username,
        issuer=issuer,
        subject=subject,
    )


async def claim_unbound_bundled_login(
    session: AsyncSession,
    record: IdentityProvider,
    user: User,
    *,
    preferred_username: str | None,
    email: str | None,
    email_verified: bool | None,
) -> None:
    """Claim a migration-created, unbound ``directory_entries`` row at the
    first real sign-in it was always going to need (ADR 0093 §13.4/§8.2).

    A users-file login that pre-dates stage (b) and never signed in has no
    gateway user for `migrate_bundled_directory` to bind in advance, so that
    migration leaves its entry's `user_id` null. `bind_bundled_login` cannot
    close that gap itself: it only ever matches an *already-bound* row, by
    its own design, because the entries it was written for (console-created
    pending users and fresh sign-ins) are bound at creation. So the ordinary
    new-user path runs first — `provision_user` creates `user` believing this
    is a brand new person — and this claims the entry immediately after,
    once there is a user row to claim it with.

    Call this **after** `provision_user`, unconditionally: it self-gates on
    `record.kind`, like `bind_bundled_login`, so stage (c)'s announce door can
    call both without knowing which kind it is talking to. Matches on the
    same rule as `bind_bundled_login` (`email_verified is True`, both sides'
    normalised email equal) so one sentence describes when either function
    binds an identity. A refusal is not an error — the entry stays unbound
    for a later administrator to reconcile by hand — so it only logs why.
    """
    if record.kind != "authelia":
        return
    if not preferred_username:
        return
    entry = (
        await session.execute(
            select(DirectoryEntry).where(
                DirectoryEntry.provider_id == record.id,
                DirectoryEntry.external_id == preferred_username,
                DirectoryEntry.user_id.is_(None),
            )
        )
    ).scalar_one_or_none()
    if entry is None:
        return
    if email_verified is not True:
        logger.info("not claiming unbound directory entry %r: email unverified", preferred_username)
        return
    claim_email, claim_trusted = is_trusted_email(email or "")
    entry_email, entry_trusted = is_trusted_email(entry.email or "")
    if not claim_trusted or not entry_trusted or claim_email != entry_email:
        logger.info(
            "not claiming unbound directory entry %r: email does not match", preferred_username
        )
        return

    entry.user_id = user.id
    await session.flush()

    await record_event(
        session,
        actor_type=IdentityEventActor.LOGIN,
        actor_label=email or preferred_username,
        action=IdentityEventAction.IDENTITY_BIND,
        target_user_id=user.id,
        target_label=email or preferred_username,
        issuer=record.issuer,
        subject=user.subject,
        detail={"claimed_unbound": True},
    )


#: ADR 0093 §6.2, in the order the design numbers them: each refusal logs a
#: WARNING naming its own number, so a declined link is a one-line diagnosis
#: rather than a search through the eight conditions.
async def link_by_email(
    session: AsyncSession,
    *,
    issuer: str,
    subject: str,
    email: str | None,
    email_verified: object,
) -> User | None:
    """Attach an unknown ``(issuer, subject)`` to the one existing account
    whose verified email matches (ADR 0093 §6.2).

    Call this only for an identity :func:`bind_bundled_login` left unmatched,
    and only when the caller has already checked ``OIDC_LINK_BY_EMAIL`` is on
    — this function does not read settings, so it never has to be told twice
    what the switch was for. Never call it from the plain ``/v1`` path: a
    bearer claim set may lack ``email_verified``, and that path's identity is
    never a stranger this rule should be deciding about.

    On success, adds a ``user_identities`` row for the target and writes the
    ``identity.link`` audit event; the caller still has to run
    :func:`provision_user` afterwards; over the same ``(issuer, subject)`` it
    will find the row just added rather than provisioning a second person.
    Every refusal just returns ``None`` — an unmatched login is an ordinary
    new user, not an error.
    """
    if email_verified is not True:
        logger.warning("link-by-email declined (1): email_verified is not the literal true")
        return None
    claim_email, trusted = is_trusted_email(email or "")
    if not trusted:
        logger.warning("link-by-email declined (2): %r is not a trusted ASCII address", email)
        return None

    candidates = (
        (await session.execute(select(User).where(User.email_normalized == claim_email)))
        .scalars()
        .all()
    )
    if len(candidates) != 1:
        logger.warning(
            "link-by-email declined (3): %d account(s) hold %r, need exactly one",
            len(candidates),
            claim_email,
        )
        return None
    target = candidates[0]

    if not (target.email_verified is True or "email" in (target.admin_edited_fields or [])):
        logger.warning(
            "link-by-email declined (4): %s's stored address was never itself verified", target.id
        )
        return None
    if not target.is_active:
        logger.warning("link-by-email declined (5): %s is not active", target.id)
        return None
    if target.is_admin:
        logger.warning("link-by-email declined (6): %s is an administrator", target.id)
        return None
    if target.issuer == PENDING_USER_ISSUER:
        logger.warning("link-by-email declined (7): %s is a pending user", target.id)
        return None
    existing_at_issuer = (
        await session.execute(
            select(UserIdentity).where(
                UserIdentity.user_id == target.id, UserIdentity.issuer == issuer
            )
        )
    ).scalar_one_or_none()
    if target.issuer == issuer or existing_at_issuer is not None:
        logger.warning(
            "link-by-email declined (8): %s already has an identity at this issuer", target.id
        )
        return None

    session.add(
        UserIdentity(user_id=target.id, issuer=issuer, subject=subject, matched_email=claim_email)
    )
    await session.flush()
    await record_event(
        session,
        actor_type=IdentityEventActor.LOGIN,
        actor_label=claim_email,
        action=IdentityEventAction.IDENTITY_LINK,
        target_user_id=target.id,
        target_label=claim_email,
        issuer=issuer,
        subject=subject,
        detail={"matched_email": claim_email, "issuer": issuer},
    )
    logger.info("link-by-email: %s/%s linked to existing user %s", issuer, subject, target.id)
    return target


async def provision_user(
    session: AsyncSession,
    *,
    issuer: str,
    subject: str,
    email: str | None,
    display_name: str | None,
    group_names: list[str],
    settings: OIDCSettings,
    #: The directory's own name for this person, `preferred_username`. Kept
    #: separately from `display_name` because an administrator searches by the
    #: name they created the account under, not by the one it is shown as.
    username: str | None = None,
    touch_login: bool = True,
    policy: OIDCPolicy | None = None,
    email_verified: bool | None = None,
    group_sync: GroupSync = GroupSync.EVERY_LOGIN,
    group_source: str = "claim",
    claims: dict[str, Any] | None = None,
    group_mappings: dict[str, str] | None = None,
) -> User:
    """Create or update a user and reconcile their group memberships.

    ``policy`` carries the console's decisions (ADR 0048); without one, the
    environment's stands and behaviour is exactly as it always was. The gate
    lives here rather than in the callers because both doors in — the browser
    callback and a bearer token on ``/v1`` — must be governed by the same
    answer, and this is the one place that sees a *first* login as such.

    Group membership is replaced **within what the directory granted**, and
    ``group_sync`` says how often (ADR 0057). The directory stays authoritative
    for its own grants, so revoking a group there still revokes the ability to
    bill it; a membership an administrator created is left alone, because an
    administrator who puts a directory user into a group means it.

    That is a change from what this function used to do, which was to replace
    every membership from the token and say so in this docstring: "mixing
    authoritative and local membership silently produces access nobody
    intended". The half that was right is kept above. The half that was wrong
    was treating *all* membership as the directory's to answer for, which made
    an administrator's own grant last until the person next signed in.

    Linking is not this function's decision any more (ADR 0093 §6): the
    matcher that used to live here only ever adopted ``issuer="local"``
    accounts, which §3.2 removes as unsound (a login at *any* other directory
    with the same address would have adopted it too). §6's cross-issuer rule
    is :func:`link_by_email`, run by :func:`sign_in` *before* this function,
    over an identity this function's own ``_identity_select`` would otherwise
    have treated as brand new — so a successful link simply means this call
    finds an existing user rather than creating one, with nothing left here
    to gate.
    """
    user = (await session.execute(_identity_select(issuer, subject))).scalar_one_or_none()

    # "First" for `group_sync=first_login`: the first login of this identity
    # here, which is the login that creates the account or the first one to
    # see it (stage (c) restores the adopted case).
    first_login_here = user is None

    if user is None and policy is not None and not policy.auto_provision:
        # Automatic provisioning is off (ADR 0048). Two answers, the operator's
        # choice: refuse the stranger outright, or create the account *inactive*
        # so an administrator can enable it — nobody signs in as a user nobody
        # has looked at, but the request is a name on a list rather than a
        # refusal to re-type next week.
        if policy.unknown_user_policy == "create_inactive":
            user = User(
                issuer=issuer,
                subject=subject,
                email=email,
                display_name=display_name,
                username=username,
                is_active=False,
            )
            session.add(user)
            await session.flush()
        else:
            raise ProvisioningRefused(
                "Automatic account creation is turned off. Ask an administrator "
                "to create your account, then sign in again."
            )

    if user is None:
        user = User(
            issuer=issuer,
            subject=subject,
            email=email,
            email_normalized=normalize_email(email) if email else None,
            display_name=display_name,
            username=username,
        )
        session.add(user)
        await session.flush()
    else:
        # Refresh mutable profile fields on every login — except the ones an
        # administrator has set from the console. A field in
        # `admin_edited_fields` is the console's now: refreshing it would
        # silently revert the administrator's edit at this login (and, for
        # `username`, again on the next /v1 request via the backfill clause in
        # `sync_user_from_claims`). Fields not listed keep following the
        # directory, so a rename or address change the console never touched
        # still arrives.
        edited = set(user.admin_edited_fields or [])
        if email is not None and "email" not in edited:
            user.email = email
            user.email_normalized = normalize_email(email)
        if display_name is not None and "display_name" not in edited:
            user.display_name = display_name
        if username is not None and "username" not in edited:
            user.username = username

    # `email_verified` (ADR 0093 §3.1) tracks the *last sign-in's* claim, not
    # whatever the email above happens to be — so it is written whenever a
    # caller has an answer, even one that leaves `email` untouched (an
    # administrator's edited address that the directory keeps re-asserting
    # unverified is exactly the case this must still record). Left alone when
    # a caller has none: a plain `/v1` claim set that omits it must not erase
    # what the last real login established.
    if email_verified is not None:
        user.email_verified = email_verified is True

    # A `/v1` call made with an access token is not a login, and recording it as
    # one would make "last seen" mean two different things on the same column.
    if touch_login:
        user.last_login_at = utcnow()

    # `never` does not even resolve the claim's group names: with nothing to
    # apply them to, creating groups from them would leave a directory's
    # vocabulary lying around in a deployment that decided not to use it.
    # How often the directory's answer applies (ADR 0057) — to groups and, when
    # the provider decides admin by claim, to the admin flag (ADR 0088).
    directory_answers = group_sync is GroupSync.EVERY_LOGIN or (
        group_sync is GroupSync.FIRST_LOGIN and first_login_here
    )
    # `group_source` says *where* that answer comes from: the token here, the
    # directory mirror (applied by a sync run, never from a token), or nowhere.
    if directory_answers and group_source == "claim":
        groups = await _resolve_groups(session, group_names, settings)
        await _reconcile_memberships(session, user, groups)
    # The env admin rules (ADR 0093 §5): a door (`touch_login`) always
    # re-evaluates both, full claim set and all; a plain `/v1` bearer call
    # only reconciles the claim rule, and only when the directory's answer
    # would apply anyway (`directory_answers`) — the email rule needs
    # `email_verified`'s literal boolean, which a bearer claim set may not
    # carry, and re-checking it on every request would put a write on the hot
    # path for no gain `_admin_diverges` does not already cover.
    if claims is not None and (touch_login or directory_answers):
        await apply_env_admin_rules(
            session, user, claims, settings, group_mappings=group_mappings, email_rule=touch_login
        )

    # Everything below reads the *effective* membership set, not the token's
    # answer. They are no longer the same thing: a manual grant is a real
    # membership, and it is a group the person may bill. Reading the token
    # here would have told a manually-added user that the group they are in is
    # not one of theirs.
    await session.refresh(user, attribute_names=["memberships"])
    effective = [membership.group for membership in user.memberships]

    # Order matters. Clear a default the user can no longer bill *first*, so that
    # the "sole group becomes the default" rule can then adopt the group they do
    # still have. Doing it the other way round leaves a single-group user with no
    # default at all, and therefore unable to make a request until they call the
    # management API.
    # The admin flag is never touched here (ADR 0069): authorisation is a
    # gateway fact, the directory's sync grants no roles, and an administrator
    # is made in the console or by `gateway passwd --admin`. Deriving the flag
    # from an admin-group claim was the one automatic path, and it is gone —
    # which is also what makes the console's own grant durable rather than
    # something the next login could reverse.
    valid_group_ids = {group.id for group in effective}
    if (
        user.default_billing_group_id is not None
        and user.default_billing_group_id not in valid_group_ids
    ):
        user.default_billing_group_id = None
    if user.default_billing_group_id is None and len(effective) == 1:
        user.default_billing_group_id = effective[0].id

    await session.flush()
    # Reload the relationship so callers see the reconciled membership set rather
    # than whatever was loaded before it changed.
    await session.refresh(user, attribute_names=["memberships"])
    return user


async def other_active_admin_exists(session: AsyncSession, *, excluding: uuid.UUID) -> bool:
    """Would at least one active administrator remain, other than ``excluding``?

    "Active" (ADR 0093 §5.5) means signed in, ``is_admin``, ``is_active``, and
    not the placeholder identity a console invite creates before someone's
    first sign-in (``PENDING_USER_ISSUER``). This is the one query every
    last-admin guard shares — the console's PATCH and DELETE routes, the CLI's
    ``admin revoke``, and the two rule-driven revocations below — so that
    "who counts as an active administrator" cannot drift between them.

    Under Postgres the check takes a transaction-scoped advisory lock first,
    so two guards running at once (two admins demoting each other, a console
    demotion racing a rule-driven revocation at sign-in) serialize instead of
    both seeing "another admin exists" and both committing — the last-admin
    rule must never depend on READ COMMITTED interleaving. SQLite has no
    advisory locks and a single-process test needs none, exactly like the
    re-seed guard in `identity_registry`.
    """
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LAST_ADMIN_ADVISORY_LOCK_KEY}
        )
    result = await session.execute(
        select(User.id)
        .where(
            User.is_admin.is_(True),
            User.is_active.is_(True),
            User.issuer != PENDING_USER_ISSUER,
            User.id != excluding,
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def apply_admin_answer(session: AsyncSession, user: User, is_admin: bool) -> str:
    """Apply a directory sync's answer about the admin flag, within provenance.

    The batch counterpart to `apply_env_admin_rules` below: this is a pull
    adapter's mirrored group membership (`directory/engine.py`), read from the
    directory mirror rather than a fresh claim, but the same provenance rule
    applies — grants are recorded as ``admin_source="env"`` (ADR 0093 §5;
    "oidc" before it, migration 0050), ``admin_rule="claim"``, and a
    revocation only touches a flag a rule granted. Refused when it would
    leave no active administrator, which is the one way a directory glitch
    could lock everyone out of the console. Returns what happened, for sync
    reports.
    """
    if is_admin:
        if user.is_admin:
            return "unchanged"
        user.is_admin = True
        user.admin_source = "env"
        user.admin_rule = "claim"
        await mark_bootstrap_consumed(session)
        return "granted"
    if not user.is_admin or user.admin_source != "env":
        return "unchanged"
    if not await other_active_admin_exists(session, excluding=user.id):
        logger.warning(
            "not revoking admin from %s: they are the last active administrator", user.id
        )
        return "kept-last-admin"
    user.is_admin = False
    user.admin_rule = None
    return "revoked"


def _matches_email_rule(claims: dict[str, Any], settings: OIDCSettings) -> bool:
    """ADR 0093 §5.1. The literal boolean, an ASCII address, and list
    membership — all three, or it does not grant."""
    allowed = settings.admin_email_list()
    if not allowed or claims.get("email_verified") is not True:
        return False
    email = claims.get("email")
    if not isinstance(email, str):
        return False
    normalized, trusted = is_trusted_email(email)
    if not trusted:
        return False
    return normalized in {normalize_email(candidate) for candidate in allowed}


def _matches_claim_rule(
    claims: dict[str, Any], settings: OIDCSettings, mappings: dict[str, str] | None
) -> bool:
    """ADR 0093 §5.2. The existing `AdminRule` shape, fed from `.env`."""
    values = settings.admin_claim_value_list()
    if not (settings.admin_claim and values):
        return False
    from gateway.identity_policy import AdminRule

    rule = AdminRule(claim=settings.admin_claim, values=frozenset(values))
    return rule.matches(claims, mappings)


async def apply_env_admin_rules(
    session: AsyncSession,
    user: User,
    claims: dict[str, Any],
    settings: OIDCSettings,
    *,
    group_mappings: dict[str, str] | None = None,
    email_rule: bool = True,
) -> str:
    """The ``.env`` admin rules (ADR 0093 §5.1, §5.2), evaluated from claims.

    Never reads the provider row — ``OIDC_ADMIN_EMAIL``, ``OIDC_ADMIN_CLAIM``
    and ``OIDC_ADMIN_CLAIM_VALUE`` are read from ``settings`` directly, which
    is what lets stage c's announce door call this unchanged. ``email_rule``
    is off for a plain ``/v1`` bearer call: its claim set may lack
    ``email_verified``, and the email rule's whole safety is the *literal*
    boolean.

    If either rule matches, admin is granted when it was not already held —
    "email" wins the ``admin_rule`` label when both match, since it is the
    more specific of the two. A rule may only ever touch what a rule granted:
    a match never converts a ``manual`` admin (console, CLI, bootstrap,
    break-glass) to ``env`` provenance, and a mismatch never revokes one.
    ``admin_rule`` itself is only ever relabelled at a sign-in
    (``email_rule=True``) — never on ``/v1``, where the claim set that would
    justify "email" is not there to have been checked, so the label could
    otherwise flip on alternate requests depending on which claim happened to
    be present.

    A revocation is refused when it would leave no active administrator
    (audited as ``admin.refused_last`` — but only at a sign-in; ``/v1`` only
    logs, since a last admin whose claim lapsed would otherwise write an
    audit row on every request), and never reaches an ``admin_rule=="email"``
    grant at all on ``/v1``: only a sign-in, whose claims can carry the
    literal ``email_verified``, or the startup sweep, may revoke what the
    email rule gave.
    """
    matched_email = email_rule and _matches_email_rule(claims, settings)
    matched_claim = _matches_claim_rule(claims, settings, group_mappings)
    rule = "email" if matched_email else "claim"

    if matched_email or matched_claim:
        if user.is_admin:
            if user.admin_source != "env":
                return "unchanged"
            if email_rule and user.admin_rule != rule:
                user.admin_rule = rule
            return "unchanged"
        user.is_admin = True
        user.admin_source = "env"
        user.admin_rule = rule
        await mark_bootstrap_consumed(session)
        await record_event(
            session,
            actor_type=IdentityEventActor.LOGIN,
            actor_label=user.email or str(user.id),
            actor_user_id=user.id,
            action=IdentityEventAction.ADMIN_GRANT,
            target_user_id=user.id,
            target_label=user.email or "",
            detail={"rule": rule},
        )
        return "granted"

    if not user.is_admin or user.admin_source != "env":
        return "unchanged"
    if not email_rule and user.admin_rule == "email":
        return "unchanged"
    if not await other_active_admin_exists(session, excluding=user.id):
        if email_rule:
            await record_event(
                session,
                actor_type=IdentityEventActor.LOGIN,
                actor_label=user.email or str(user.id),
                actor_user_id=user.id,
                action=IdentityEventAction.ADMIN_REFUSED_LAST,
                target_user_id=user.id,
                target_label=user.email or "",
                reason="would leave no active administrator",
            )
        else:
            logger.warning(
                "not revoking admin from %s: they are the last active administrator", user.id
            )
        return "kept-last-admin"
    revoked_rule = user.admin_rule
    user.is_admin = False
    user.admin_rule = None
    await record_event(
        session,
        actor_type=IdentityEventActor.LOGIN,
        actor_label=user.email or str(user.id),
        actor_user_id=user.id,
        action=IdentityEventAction.ADMIN_REVOKE,
        target_user_id=user.id,
        target_label=user.email or "",
        detail={"rule": revoked_rule} if revoked_rule else {},
    )
    return "revoked"


async def sweep_env_admin_email_rule(session: AsyncSession, settings: OIDCSettings) -> None:
    """ADR 0093 §5.1's startup sweep: only ever revokes, and only the email
    rule's own grants. There is no fresh claim set to re-check the claim rule
    with at startup — that can only happen at a person's next sign-in
    (`apply_env_admin_rules`), which this leaves entirely alone.
    """
    allowed = {normalize_email(candidate) for candidate in settings.admin_email_list()}
    candidates = (
        await session.execute(
            select(User).where(
                User.is_admin.is_(True),
                User.admin_source == "env",
                User.admin_rule == "email",
            )
        )
    ).scalars()
    for user in candidates:
        if user.email_normalized in allowed:
            continue
        if not await other_active_admin_exists(session, excluding=user.id):
            await record_event(
                session,
                actor_type=IdentityEventActor.SYSTEM,
                actor_label="system",
                action=IdentityEventAction.ADMIN_REFUSED_LAST,
                target_user_id=user.id,
                target_label=user.email or "",
                reason="would leave no active administrator",
            )
            continue
        user.is_admin = False
        user.admin_rule = None
        await record_event(
            session,
            actor_type=IdentityEventActor.SYSTEM,
            actor_label="system",
            action=IdentityEventAction.ADMIN_REVOKE,
            target_user_id=user.id,
            target_label=user.email or "",
            detail={"rule": "email"},
        )
    await session.commit()


async def promote_bootstrap_admin(
    session: AsyncSession,
    user: User,
    *,
    bootstrap_email: str,
    email: str | None,
    email_verified: object,
    kind: str,
) -> bool:
    """Make ``user`` the first administrator, if everything lines up.

    All five must hold: the environment's provider kind is ``authelia`` — an
    external IdP names its own admins through ``OIDC_ADMIN_EMAIL`` /
    ``OIDC_ADMIN_CLAIM`` instead (ADR 0093 §5), and this variable is inert
    there, with a startup warning rather than a silent behaviour change; a
    bootstrap address is configured; this login's email matches it
    (case-insensitively); the provider says the address is verified — the
    literal boolean ``True``, the same strictness account linking uses (ADR
    0056), because otherwise an ``email`` claim would be a password; and the
    bootstrap has not already fired.

    That last condition used to be "no active administrator exists", which
    let deactivating every admin re-arm this door — a hidden recovery path and
    a hidden takeover path (review correction 2, R1). It is now
    ``deployment_state.bootstrap_admin_consumed_at``, set here and by every
    other path that can produce the deployment's first administrator
    (``gateway.deployment_state.mark_bootstrap_consumed``), so it can be
    consulted once and never re-armed for the life of the deployment.

    Called from the browser callback only. A ``/v1`` bearer call never
    promotes anyone, for the reason it never links: only the callback sees the
    full claim set.
    """
    if kind != "authelia":
        return False
    wanted = bootstrap_email.strip().casefold()
    if not wanted or not email or email.strip().casefold() != wanted:
        return False
    if email_verified is not True:
        logger.warning(
            "bootstrap admin %s signed in but email_verified=%r; not promoting",
            email,
            email_verified,
        )
        return False
    if user.is_admin:
        return False
    state = await get_or_create_deployment_state(session)
    if state.bootstrap_admin_consumed_at is not None:
        return False
    user.is_admin = True
    await mark_bootstrap_consumed(session)
    logger.warning("bootstrap admin: %s is now the first administrator", email)
    return True


@dataclass
class SignInResult:
    """What a sign-in door needs to answer, whatever its own response shape."""

    user: User
    groups: list[str]


async def sign_in(
    session: AsyncSession,
    record: IdentityProvider,
    *,
    issuer: str,
    subject: str,
    claims: dict[str, Any],
    settings: Settings,
    policy: OIDCPolicy | None = None,
) -> SignInResult:
    """The one sequence every sign-in door runs (ADR 0093 §4.1): the console
    callback and ``POST /v1/session/announce``. Both see the full claim set —
    ``claims`` merged with userinfo where the provider needs that — which is
    what lets them link, bind, apply the email admin rule and fire the
    bootstrap. A plain ``/v1`` bearer call never reaches here; it runs
    :func:`sync_user_from_claims` instead, on whatever claims its token
    happened to carry.

    ``issuer``/``subject`` are taken from the caller separately from
    ``claims`` rather than read off it here: the console callback's merged
    claim set folds in userinfo, whose own ``sub`` a provider is not
    guaranteed to echo byte-for-byte, so identity is decided from the ID
    token alone while everything else — email, name, groups — reads the
    merged set.

    The order is the design's own, and it is order, not a checklist: binding
    a bundled login and linking by email must both be settled *before*
    :func:`provision_user` ever asks "have I seen this identity before",
    because each one's whole job is to make that question answer "yes" for
    an identity that would otherwise look brand new.
    """
    # Removing a spent provider checks "nobody signed in with it" and then
    # deletes the row, and that is only true if nobody can sign in between. A
    # share lock on the row, held to this transaction's commit, makes the
    # removal's FOR UPDATE wait for us and then see the person we attach; and a
    # row that has gone, or been disabled since the provider was resolved, ends
    # the sign-in here. (SQLite has no row locks, and the test suite's one
    # writer needs none.) A record with no row is the environment fallback.
    if getattr(record, "source", "") == "console":
        enabled = (
            await session.execute(
                select(IdentityProvider.is_enabled)
                .where(IdentityProvider.id == record.id)
                .with_for_update(read=True)
            )
        ).scalar_one_or_none()
        if not enabled:
            raise ProvisioningRefused("This identity provider is no longer available.")

    email = claims.get("email")
    email_verified = claims.get("email_verified")
    preferred_username = claims.get("preferred_username")
    display_name = claims.get("name") or claims.get("preferred_username")
    username = preferred_username if isinstance(preferred_username, str) else None

    groups = extract_groups(claims, settings.oidc, policy)
    if policy is not None:
        groups = policy.map_group_names(groups)

    # ADR 0093 §8.2: a console-created account's login claims this identity
    # by login name and verified email, before anything below gets a chance
    # to treat it as a stranger.
    await bind_bundled_login(
        session,
        record,
        issuer=issuer,
        subject=subject,
        preferred_username=preferred_username,
        email=email,
        email_verified=email_verified,
    )

    # ADR 0093 §6.2: only for an identity still unknown after the bind above,
    # and only when the operator turned the switch on. A hit here means the
    # `provision_user` call below finds an existing person instead of
    # creating one, through the `user_identities` row this just added.
    known = (await session.execute(_identity_select(issuer, subject))).scalar_one_or_none()
    if known is None and settings.oidc.link_by_email:
        await link_by_email(
            session,
            issuer=issuer,
            subject=subject,
            email=email if isinstance(email, str) else None,
            email_verified=email_verified,
        )

    user = await provision_user(
        session,
        issuer=issuer,
        subject=subject,
        email=email if isinstance(email, str) else None,
        display_name=display_name if isinstance(display_name, str) else None,
        username=username,
        group_names=groups,
        settings=settings.oidc,
        policy=policy,
        # Raw, not coerced: several providers put `email_verified` on
        # userinfo only, and coercing a non-boolean here would make this the
        # one gate that treats "an email claim" as good enough to trust.
        email_verified=email_verified,
        group_sync=record.group_sync,
        group_source=record.group_source,
        claims=claims,
        group_mappings=policy.group_mappings if policy is not None else None,
    )

    # ADR 0093 §13.4/§8.2: a migration-created, unbound login claims itself
    # at the first real sign-in it gets, now that `provision_user` has given
    # it a user row to claim with.
    await claim_unbound_bundled_login(
        session,
        record,
        user,
        preferred_username=preferred_username,
        email=email,
        email_verified=email_verified,
    )

    # A directory whose subjects are unknown until first login (Authelia)
    # links its mirrored entry now, so pre-assigned groups and directory
    # groups apply at once rather than at the next scheduled sync. Imported
    # here, not at module level: `directory.engine` imports from this module,
    # and a top-level import the other way would be a cycle.
    from gateway.directory.engine import ensure_bundled_default_group, link_at_login

    if record.sync_adapter != "none" and getattr(record, "source", "") != "environment":
        await link_at_login(session, record, user, claims, settings=settings.oidc)

    # ADR 0093 to-do item 1: a bundled-Authelia sign-in with no membership at
    # all — a brand new account, or one provisioned before this existed —
    # gets the `users` group here, every sign-in, not just the first. An
    # external IdP's groups are the claim's or the admin's to answer for, as
    # today, so this only ever runs for the bundled provider.
    if record.kind == "authelia":
        await ensure_bundled_default_group(session, user)

    # OIDC-only deployments have no password door to make the first
    # administrator through; the configured address, verified, is it.
    await promote_bootstrap_admin(
        session,
        user,
        bootstrap_email=settings.bootstrap_admin_email,
        email=email if isinstance(email, str) else None,
        email_verified=email_verified,
        kind=settings.oidc.kind,
    )

    return SignInResult(user=user, groups=groups)


async def sync_user_from_claims(
    session: AsyncSession,
    *,
    claims: dict[str, Any],
    settings: OIDCSettings,
    policy: OIDCPolicy | None = None,
    group_sync: GroupSync = GroupSync.EVERY_LOGIN,
    group_source: str = "claim",
    group_mappings: dict[str, str] | None = None,
) -> User:
    """Resolve an access token's claims to the user row it names.

    The same ``(issuer, subject)`` key the browser login uses, so a person who
    signs into the console and a person whose chat message arrives over ``/v1``
    are one row, one spend total, one set of quotas.

    **Memberships are reconciled only when the token disagrees with them**, and
    that condition is the whole design of this function. Reconciling on every
    request would put writes on the hot path; reconciling never would mean that
    removing someone from a group in the directory stops them signing into the
    console while leaving them able to bill that group through the API, which is
    the worse half of the access being revoked. Comparing against the token
    costs nothing — the claims are already parsed and the memberships already
    loaded — and a token is short-lived, so the window in which a stale group
    can be used is one token lifetime, not forever.
    """
    issuer = claims.get("iss")
    subject = claims.get("sub")
    if not isinstance(issuer, str) or not isinstance(subject, str):
        raise OIDCError("access token has no usable issuer or subject")

    # The same resolution the browser login uses, so a linked account answers
    # to its directory identity on `/v1` too — without which someone would
    # sign into the console as themselves and bill as a second person.
    user = (await session.execute(_identity_select(issuer, subject))).scalar_one_or_none()

    # Mapping first (ADR 0048): the rest of the flow — reconciliation,
    # divergence, admin — speaks local names only.
    group_names = (
        policy.map_group_names(extract_groups(claims, settings, policy))
        if policy is not None
        else extract_groups(claims, settings)
    )
    email = claims.get("email")
    display_name = claims.get("name") or claims.get("preferred_username")
    # Read in its own right, not as a fallback. `display_name` above still
    # falls back to it for a directory that sends no `name`, but a directory
    # that sends both used to have its username discarded — which made an
    # account created as `chat@local` findable only as `chat@example.org`.
    raw_username = claims.get("preferred_username")
    username = raw_username if isinstance(raw_username, str) else None

    if (
        user is not None
        # A username the row does not already carry is a divergence. Without
        # this the early return below wins for every existing account — their
        # groups have not changed — and the column would only ever fill for
        # people who signed up after the migration. That is exactly the bug
        # this was added to fix, so it would have fixed nothing.
        #
        # Compared rather than merely tested for presence, so it settles: the
        # first request after a login writes it, and every one after that
        # takes the early return again. `_claims_diverge` exists precisely
        # because equality checks here once meant a write per `/v1` request.
        #
        # An administrator-edited username is settled by definition, not a
        # divergence: provisioning would refuse to write it (the override in
        # `provision_user`), so counting it here would route *every* request
        # through full provisioning forever — the divergence that can never
        # resolve, which is the exact failure `_claims_diverge` was written
        # to keep off the hot path.
        and (
            username is None
            or user.username == username
            or "username" in (user.admin_edited_fields or [])
        )
        and (
            group_source != "claim"
            or not _claims_diverge(user, group_names, settings, policy, group_sync)
        )
        and not _admin_diverges(user, claims, group_sync, settings, group_mappings)
    ):
        return user

    return await provision_user(
        session,
        issuer=issuer,
        subject=subject,
        email=email if isinstance(email, str) else None,
        display_name=display_name if isinstance(display_name, str) else None,
        username=username,
        group_names=group_names,
        settings=settings,
        touch_login=False,
        policy=policy,
        group_sync=group_sync,
        group_source=group_source,
        claims=claims,
        group_mappings=group_mappings,
    )


def _admin_diverges(
    user: User,
    claims: dict[str, Any],
    group_sync: GroupSync,
    settings: OIDCSettings,
    mappings: dict[str, str] | None,
) -> bool:
    """Would this token change the admin flag? Only asked on every-login sync.

    The claim rule only — never the email rule, whose claim set a bearer
    token may not carry (ADR 0093 §5.1) — built fresh from ``settings`` each
    call rather than threaded through as an ``AdminRule``, so this and
    ``apply_env_admin_rules`` can never disagree about what the rule is.

    Cheap — the claims are parsed, the row loaded — and it settles: after one
    provisioning the flag agrees with the token (or provenance forbids the
    change), so the hot path returns early again.

    The revocation half checks ``admin_rule == "claim"`` specifically, not
    just ``is_admin``: an ``admin_rule == "email"`` grant must never look like
    a divergence here, or an unrelated reason to re-provision (a changed
    group, a changed username) would reach ``apply_env_admin_rules`` with
    ``email_rule=False`` and revoke it — the same protection that function
    applies itself, kept here too so a future caller of either one alone
    stays correct.
    """
    if group_sync is not GroupSync.EVERY_LOGIN:
        return False
    if _matches_claim_rule(claims, settings, mappings):
        return not user.is_admin
    return user.is_admin and user.admin_source == "env" and user.admin_rule == "claim"


def _claims_diverge(
    user: User,
    group_names: list[str],
    settings: OIDCSettings,
    policy: OIDCPolicy | None = None,
    group_sync: GroupSync = GroupSync.EVERY_LOGIN,
) -> bool:
    """Does the token say something the stored row does not already reflect?

    Group *names* are compared rather than ids because that is what the token
    carries, and a name the gateway has never seen is a divergence whether or
    not it will end up creating a group: with ``auto_create_groups`` off it
    resolves to nothing, and the comparison correctly settles on the next call.

    The question asked is "would a sync change anything" (ADR 0057), not
    "does the stored set equal the claimed set". This runs on every ``/v1``
    request that arrives with a bearer token, and once an administrator's grant
    can outlive a login the two questions have different answers: a user with
    one such group is *permanently* unequal to their token, which would mean a
    write on the hot path per request.

    When the directory does not set membership on every login there is nothing
    for a group difference to mean, so the comparison is skipped entirely.
    ``is_admin`` is not compared at all (ADR 0069): the flag is a console fact
    no claim can move, so there is nothing a token could diverge from.
    """
    if group_sync is not GroupSync.EVERY_LOGIN:
        return False
    held = {membership.group.name for membership in user.memberships}
    claimed = set(group_names)
    granted = {
        membership.group.name
        for membership in user.memberships
        if membership.source is MembershipSource.OIDC
    }
    # Exactly the two things a sync would do, asked separately rather than
    # by comparing two sets for equality. Equality was wrong once
    # provenance existed: a user with one administrator-granted group
    # differs from their token *permanently*, so every request would have
    # re-provisioned — a write on the hot path, and one that would then try
    # to strip the group the administrator granted.
    return bool(claimed - held or granted - claimed)


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
    """Make the stored memberships match *groups*, within what the login granted.

    Additive half: every group in *groups* is granted, marked as the
    directory's. Subtractive half: a membership is removed only if the
    directory granted it (``MembershipSource.OIDC``) and the directory has now
    stopped naming it. An administrator's grant survives, which is ADR 0057.

    Provenance is the test, and the two obvious alternatives are both wrong.
    *Whether the token named the group* cannot be it — a directory that stops
    naming a group would thereby lose the right to remove it, which is
    revocation backwards. *Who created the group* cannot be it either, and the
    bearer-token revocation test is what proved it: an administrator creates
    "research" in the console, the directory also names it, and under that rule
    the directory could add people to it and never remove them.

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
        if membership.group_id not in desired and membership.source is MembershipSource.OIDC:
            await session.delete(membership)

    already_present = {
        membership.group_id for membership in existing if membership.group_id in desired
    }
    for group in groups:
        if group.id not in already_present:
            session.add(
                Membership(user_id=user.id, group_id=group.id, source=MembershipSource.OIDC)
            )

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


@dataclass(frozen=True)
class SessionClaims:
    """What a verified session token asserts: who, and since when.

    `issued_at` is the disable cascade's own check (ADR 0093 §9.1):
    `load_user_for_management` refuses a token whose `iat` predates the
    user's `sessions_valid_after` — a session minted before a disable (or a
    recovery, or a merge) is exactly the session a disable exists to end,
    even though the JWT signature itself still verifies. Carried on the
    token rather than looked up, so refusing a stale session costs no query
    beyond the one that already loads the user.
    """

    user_id: uuid.UUID
    issued_at: datetime


def verify_session_token(token: str, *, secret: str) -> SessionClaims:
    """Return the session's claims, or raise :class:`OIDCError`."""
    if not secret:
        raise OIDCError("GATEWAY_SESSION_SECRET is not configured")
    key = OctKey.import_key(secret)
    try:
        decoded = jwt.decode(token, key, algorithms=[_SESSION_ALGORITHM])
        JWTClaimsRegistry(
            exp={"essential": True},
            sub={"essential": True},
            iat={"essential": True},
            typ={"essential": True, "value": "gw-session"},
        ).validate(decoded.claims)
    except JoseError as exc:
        raise OIDCError(f"session token is not valid: {exc}") from exc

    try:
        user_id = uuid.UUID(str(decoded.claims["sub"]))
    except (KeyError, ValueError) as exc:
        raise OIDCError("session token subject is not a user id") from exc
    try:
        issued_at = datetime.fromtimestamp(int(decoded.claims["iat"]), tz=UTC)
    except (KeyError, TypeError, ValueError) as exc:
        raise OIDCError("session token carries no usable issued-at claim") from exc
    return SessionClaims(user_id=user_id, issued_at=issued_at)
