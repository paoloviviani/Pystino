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

The device authorization flow itself — the endpoint dance that gets ``opencode`` a
token in the first place — is still to come; what is here is the half that
matters to the gateway, which is what to do with the token once it exists.
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
from sqlalchemy import Select, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from gateway.config import OIDCSettings
from gateway.models import (
    Group,
    GroupSource,
    GroupSync,
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

        return dict(token.claims)

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


# The issuer of an account whose door is a password (ADR 0043). Spelled out
# here because this module now has to *avoid* matching it as a directory.
_LOCAL_ISSUER = "local"


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


async def _adopt_local_account(
    session: AsyncSession,
    *,
    issuer: str,
    subject: str,
    email: str | None,
    email_verified: bool | None,
) -> User | None:
    """The local account this directory identity may adopt, or ``None``.

    Linking is off unless an operator turned it on for this provider, and the
    caller has already established that. What is decided here is whether the
    claims earn it.

    **A verified address, or nothing.** ``email_verified`` absent is treated as
    unverified, never as consent: a provider that does not say has not said
    yes, and reading silence as verification is what would turn an ``email``
    claim into a password for the account it names. This is the single property
    that makes the feature tolerable — see ADR 0056.

    The match is on ``(issuer="local", subject=<address>)``, which is unique by
    construction, and not on ``users.email``, which is neither unique nor
    stable. Every refusal is logged with its reason: a login that quietly
    creates a second account instead of linking is exactly the confusion an
    operator would otherwise debug from the outside.
    """
    if not email:
        logger.warning(
            "account linking declined for %s/%s: the claims carry no email", issuer, subject
        )
        return None
    if email_verified is not True:
        logger.warning(
            "account linking declined for %s at %s: email_verified is %r — an unverified "
            "address is a claim, not proof that the person owns it",
            email,
            issuer,
            email_verified,
        )
        return None

    # Local subjects are casefolded addresses: that is what `local_login`
    # looks up, so it is what a link has to agree with.
    address = email.strip().casefold()
    local = (await session.execute(_identity_select(_LOCAL_ISSUER, address))).scalar_one_or_none()
    if local is None:
        # Not a refusal. There is simply no local account by that name, and
        # the caller goes on to create the ordinary new one.
        return None

    held = (
        await session.execute(
            select(UserIdentity).where(
                UserIdentity.user_id == local.id, UserIdentity.issuer == issuer
            )
        )
    ).scalar_one_or_none()
    if held is not None:
        # One person, one identity per directory. A second subject arriving
        # from the same issuer for the same address is not the same person
        # twice — it is a directory that reassigned the address, or two
        # accounts in it, and adopting on the strength of the address alone
        # would hand the second one everything the first one has.
        logger.warning(
            "account linking declined for %s/%s: local account %s is already linked to "
            "%s at this provider",
            issuer,
            subject,
            local.id,
            held.subject,
        )
        return None

    session.add(
        UserIdentity(user_id=local.id, issuer=issuer, subject=subject, matched_email=address)
    )
    await session.flush()
    logger.info(
        "linked identity %s/%s to local account %s (%s)", issuer, subject, local.id, address
    )
    return local


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
    allow_local_link: bool = False,
    email_verified: bool | None = None,
    group_sync: GroupSync = GroupSync.EVERY_LOGIN,
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

    ``allow_local_link`` is this provider's ``link_local_by_email`` switch and
    defaults to off, so every caller that does not pass it keeps the behaviour
    it always had. Note where it is *not* passed: an access token on ``/v1``
    resolves an existing link but never creates one (ADR 0056). Linking is a
    decision about who an account belongs to, and the browser callback is the
    only door that sees the full claim set — userinfo included, which is where
    several providers put ``email_verified``.
    """
    user = (await session.execute(_identity_select(issuer, subject))).scalar_one_or_none()

    # "First" for `group_sync=first_login`: the first login of this identity
    # here, which is the login that creates the account *or* the one that
    # adopts an existing local one — both are the first time this directory
    # has anything to say about it.
    first_login_here = user is None

    if user is None and allow_local_link:
        # An adopted account is not a new one, so this runs *before* the
        # auto-provisioning gate below: refusing to create strangers is a
        # rule about strangers, and this person already has a row here.
        user = await _adopt_local_account(
            session,
            issuer=issuer,
            subject=subject,
            email=email,
            email_verified=email_verified,
        )

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
        if display_name is not None and "display_name" not in edited:
            user.display_name = display_name
        if username is not None and "username" not in edited:
            user.username = username

    # A `/v1` call made with an access token is not a login, and recording it as
    # one would make "last seen" mean two different things on the same column.
    if touch_login:
        user.last_login_at = utcnow()

    # `never` does not even resolve the claim's group names: with nothing to
    # apply them to, creating groups from them would leave a directory's
    # vocabulary lying around in a deployment that decided not to use it.
    if group_sync is GroupSync.EVERY_LOGIN or (
        group_sync is GroupSync.FIRST_LOGIN and first_login_here
    ):
        groups = await _resolve_groups(session, group_names, settings)
        await _reconcile_memberships(session, user, groups)

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


async def sync_user_from_claims(
    session: AsyncSession,
    *,
    claims: dict[str, Any],
    settings: OIDCSettings,
    policy: OIDCPolicy | None = None,
    group_sync: GroupSync = GroupSync.EVERY_LOGIN,
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
        and not _claims_diverge(user, group_names, settings, policy, group_sync)
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
    )


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
