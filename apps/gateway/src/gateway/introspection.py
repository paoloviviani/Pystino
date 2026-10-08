"""Opaque OIDC access tokens: RFC 7662 introspection, bound to one provider.

Some identity providers do not issue JWT access tokens. GitLab is the case
that forced this: its access tokens are Doorkeeper's random strings, so the
issuer cannot be read out of the token to route it, and there is no signature
to check. The only authority on such a token is the provider that issued it,
asked through its ``introspection_endpoint``.

Binding a token to a provider, not merely to "some provider said yes". Each
provider is asked with *its own* registered client credentials (the chat's,
for the environment's provider, then the provider's own client), and an
answer is believed only when it is ``active: true``, names the client that
asked (``client_id``), carries an unexpired ``exp`` and, if it says ``iss``,
says this provider's issuer. A client's credentials are registered at exactly
one provider, and GitLab (Doorkeeper's default ``allow_token_introspection``)
answers ``active: true`` only to the application the token was issued to, so
an active answer under a provider's own credentials is that provider vouching
for its own token. The identity is then ``(that provider's discovery issuer,
sub)``, exactly the pair an ID-token login at the same provider produces.

Doorkeeper's introspection answer carries no ``sub`` (``active``, ``scope``,
``client_id``, ``token_type``, ``iat``, ``exp`` and nothing else), so the
subject comes from userinfo fetched with the token itself. GitLab's userinfo
``sub`` is the same user id its ID tokens carry.

Answers are cached by a SHA-256 of the token, never the token, for at most
:data:`POSITIVE_TTL_SECONDS` and never past the token's own ``exp``; a
refusal for :data:`NEGATIVE_TTL_SECONDS`. That bound is also the revocation
latency: a token revoked at the provider keeps working here for up to a
minute.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from gateway.config import Settings
from gateway.identity_registry import ProviderRecord
from gateway.oidc import OIDCClient, OIDCError

logger = logging.getLogger(__name__)

POSITIVE_TTL_SECONDS = 60.0
# Short: long enough that a client retrying a dead token in a loop does not
# turn into one request to the provider per call, short enough that a token
# refused because the provider blipped is retried soon.
NEGATIVE_TTL_SECONDS = 10.0
MAX_CACHE_ENTRIES = 10_000


class OpaqueTokenRejected(OIDCError):
    """An opaque token no provider vouched for; the message is the reason."""


@dataclass(frozen=True, slots=True)
class IntrospectionCredentials:
    client_id: str
    client_secret: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One provider that may have issued an opaque token, and how to ask it."""

    record: ProviderRecord
    client: OIDCClient
    credentials: tuple[IntrospectionCredentials, ...]


def credentials_for(
    record: ProviderRecord, settings: Settings
) -> tuple[IntrospectionCredentials, ...]:
    """Which client credentials may introspect tokens from this provider.

    The chat's first, and only at the environment's provider: the chat signs
    in there and nowhere else, and its secret must never be sent to another
    provider. Then the provider's own (console) client, for tokens it issued
    itself. Order matters only for cost: GitLab answers ``active: false`` to
    every client but the token's own, so the likelier owner goes first.
    """
    out: list[IntrospectionCredentials] = []
    oidc = settings.oidc
    chat_secret = oidc.chat_client_secret.get_secret_value()
    if oidc.chat_client_id and chat_secret and record.issuer.rstrip("/") == oidc.issuer.rstrip("/"):
        out.append(IntrospectionCredentials(oidc.chat_client_id, chat_secret))
    if (
        record.client_id
        and record.client_secret
        and record.client_id not in {c.client_id for c in out}
    ):
        out.append(IntrospectionCredentials(record.client_id, record.client_secret))
    return tuple(out)


@dataclass(frozen=True, slots=True)
class _Positive:
    provider_id: uuid.UUID
    claims: dict[str, Any]
    expires_at: float


@dataclass(frozen=True, slots=True)
class _Negative:
    reason: str
    expires_at: float


class IntrospectionCache:
    """Bounded LRU of introspection answers, keyed by a hash of the token.

    Synchronous and never awaiting, like ``SlidingWindowLimiter``, so two
    requests on one event loop cannot interleave inside it. Two concurrent
    first requests for one token may both introspect; that costs one extra
    call to the provider and is not worth a lock.
    """

    def __init__(
        self,
        *,
        max_entries: int = MAX_CACHE_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._max_entries = max_entries
        self._clock = clock
        self._wall_clock = wall_clock
        self._entries: OrderedDict[str, _Positive | _Negative] = OrderedDict()

    @staticmethod
    def key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def now(self) -> float:
        return self._wall_clock()

    def get(self, token: str) -> _Positive | _Negative | None:
        key = self.key(token)
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= self._clock():
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return entry

    def put_positive(
        self, token: str, provider_id: uuid.UUID, claims: dict[str, Any], exp: float
    ) -> None:
        ttl = min(exp - self._wall_clock(), POSITIVE_TTL_SECONDS)
        if ttl <= 0:
            return
        self._put(token, _Positive(provider_id, claims, self._clock() + ttl))

    def put_negative(self, token: str, reason: str) -> None:
        self._put(token, _Negative(reason, self._clock() + NEGATIVE_TTL_SECONDS))

    def _put(self, token: str, entry: _Positive | _Negative) -> None:
        key = self.key(token)
        self._entries[key] = entry
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def __len__(self) -> int:
        return len(self._entries)


async def _ask(
    client: OIDCClient, endpoint: str, token: str, credentials: IntrospectionCredentials
) -> dict[str, Any] | None:
    """One RFC 7662 call; ``None`` when the provider could not answer.

    ``client_secret_basic``: the method GitLab's discovery lists first for its
    token endpoint, and the one Doorkeeper's introspection reads before the
    body. Never logs a body: an error body is the provider's, and nothing in
    this function may put the token in a log line.
    """
    try:
        response = await client._http.post(
            endpoint,
            data={"token": token, "token_type_hint": "access_token"},
            auth=(credentials.client_id, credentials.client_secret),
            headers={"accept": "application/json", **client._backchannel_headers()},
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "introspection at %s as client %r failed: %s",
            endpoint,
            credentials.client_id,
            type(exc).__name__,
        )
        return None
    if response.status_code != 200:
        # 401 is Doorkeeper's answer to credentials it does not know: the
        # secret is wrong, or the client was deleted at the provider.
        logger.warning(
            "introspection at %s as client %r answered HTTP %s",
            endpoint,
            credentials.client_id,
            response.status_code,
        )
        return None
    try:
        payload = response.json()
    except ValueError:
        logger.warning("introspection at %s returned a non-JSON body", endpoint)
        return None
    return payload if isinstance(payload, dict) else None


async def introspect(
    token: str, candidates: Sequence[Candidate], cache: IntrospectionCache
) -> tuple[ProviderRecord, OIDCClient, dict[str, Any]]:
    """The provider that vouches for ``token``, and the claims it stands for.

    Raises :class:`OpaqueTokenRejected` with a reason an operator can act on.
    The claims carry ``iss`` (the provider's discovery issuer), ``sub``,
    ``azp`` and ``client_id`` (the client the token was issued to), ``exp``,
    ``scope``, and whatever userinfo answered — so every check downstream of
    a JWT's claims (``ACCEPTED_CLIENTS``, the chat-client check, the user
    sync) applies to them unchanged.
    """
    cached = cache.get(token)
    if isinstance(cached, _Negative):
        raise OpaqueTokenRejected(f"{cached.reason} (cached)")
    if isinstance(cached, _Positive):
        for candidate in candidates:
            if candidate.record.id == cached.provider_id:
                return candidate.record, candidate.client, dict(cached.claims)
        # The provider was disabled since: fall through and ask the rest,
        # which will not vouch for a token they did not issue.

    usable = [c for c in candidates if c.credentials]
    if not usable:
        raise OpaqueTokenRejected(
            "not a JWT, and no enabled provider has client credentials to introspect it with"
        )

    reasons: list[str] = []
    answered = False
    for candidate in usable:
        try:
            metadata = await candidate.client.metadata()
        except OIDCError as exc:
            reasons.append(f"{candidate.record.name}: discovery failed ({exc})")
            continue
        endpoint = metadata.introspection_endpoint
        if not endpoint:
            reasons.append(f"{candidate.record.name}: no introspection_endpoint")
            continue
        for credentials in candidate.credentials:
            answer = await _ask(candidate.client, endpoint, token, credentials)
            if answer is None:
                reasons.append(
                    f"{candidate.record.name}: introspection as {credentials.client_id!r} failed"
                )
                continue
            answered = True
            if answer.get("active") is not True:
                reasons.append(
                    f"{candidate.record.name}: inactive for client {credentials.client_id!r}"
                )
                continue
            try:
                claims = await _claims_from(
                    candidate, metadata.issuer, credentials, answer, token, cache
                )
            except OpaqueTokenRejected as exc:
                # An active answer that fails a binding check is definite: no
                # other provider can vouch for a token this one issued.
                cache.put_negative(token, str(exc))
                raise
            cache.put_positive(token, candidate.record.id, claims, float(claims["exp"]))
            return candidate.record, candidate.client, dict(claims)

    reason = "not a JWT, and no introspecting provider vouched for it: " + "; ".join(reasons)
    if answered:
        # Only a definite "inactive" is remembered: a provider that could not
        # be reached must be asked again on the next request.
        cache.put_negative(token, reason)
    raise OpaqueTokenRejected(reason)


async def _claims_from(
    candidate: Candidate,
    issuer: str,
    credentials: IntrospectionCredentials,
    answer: dict[str, Any],
    token: str,
    cache: IntrospectionCache,
) -> dict[str, Any]:
    name = candidate.record.name
    client_id = answer.get("client_id")
    if client_id != credentials.client_id:
        # Doorkeeper never answers active for another application's token,
        # but an IdP configured to let one client introspect everything
        # would, and the token would then be bound to the wrong client.
        raise OpaqueTokenRejected(
            f"{name}: introspection names client {client_id!r}, not the asking "
            f"client {credentials.client_id!r}"
        )
    answered_iss = answer.get("iss")
    if answered_iss is not None and str(answered_iss).rstrip("/") != issuer.rstrip("/"):
        raise OpaqueTokenRejected(
            f"{name}: introspection names issuer {answered_iss!r}, not {issuer!r}"
        )
    exp = answer.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, int | float):
        raise OpaqueTokenRejected(f"{name}: introspection carries no exp")
    if exp <= cache.now():
        raise OpaqueTokenRejected(f"{name}: token expired")

    userinfo = await candidate.client.fetch_userinfo(token)
    subject = answer.get("sub")
    userinfo_subject = userinfo.get("sub")
    if (
        subject is not None
        and userinfo_subject is not None
        and str(subject) != str(userinfo_subject)
    ):
        raise OpaqueTokenRejected(f"{name}: introspection and userinfo disagree on sub")
    subject = subject if subject is not None else userinfo_subject
    if subject is None or str(subject) == "":
        raise OpaqueTokenRejected(f"{name}: neither introspection nor userinfo names a sub")

    claims: dict[str, Any] = dict(userinfo)
    claims.update(
        {
            "iss": issuer,
            "sub": str(subject),
            "azp": client_id,
            "client_id": client_id,
            "exp": exp,
        }
    )
    if isinstance(answer.get("scope"), str):
        claims["scope"] = answer["scope"]
    return claims
