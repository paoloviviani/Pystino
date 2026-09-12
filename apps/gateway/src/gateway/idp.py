"""The house issuer: a minimal IdP for browser-facing clients (ADR 0068).

The gateway has two identity roles, and this module is only one of them. As a
*relying party* it signs people in against external directories
(:mod:`gateway.oidc`); as an *issuer* it is the directory its own clients sign
in against — the chat, later the desktop shell. The two roles share the user
store and the session cookie and nothing else; a provider row in the registry
is an external fact, while the issuer's clients are software this deployment
ships, registered in settings rather than in the database (ADR 0068: no
public registration, because a public registry is how client squatting
starts).

Three decisions shape everything here:

* **Access tokens stay opaque.** The token endpoint mints the same ``gwa``
  key rows and ``gwr`` refresh credentials ADR 0046 built — an OAuth client
  cannot tell the difference, and the ``/v1`` hot path never learns that an
  IdP exists. The only JWT is the ``id_token``, signed once at login.
* **The issuer is configured, never sniffed.** Behind a plain-HTTP edge the
  request arrives as ``http://`` while every client knows the deployment as
  ``https://``; an issuer that changed with the vantage point would fail
  validation on one side or the other. All endpoint URLs in discovery are
  built from the configured issuer (browser-facing) and internal base URL
  (server-to-server).
* **The issuer aliases ``local``.** Users are keyed on ``(issuer, subject)``
  and the house issuer's ``id_token`` says so with the deployment's own URL —
  but the row it names is the pre-existing ``issuer="local"`` account, found
  by the casefolded email that is both the token's ``sub`` and the local
  subject. One row per person across both doors; no adoption decision, no
  ADR 0056 machinery for our own issuer. An external issuer cannot claim the
  alias: a token bearing it must verify against this issuer's own JWKS to be
  believed at all.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import serialization
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import ECKey, KeySet

from gateway.config import IdPSettings

ALGORITHM = "ES256"

#: What discovery advertises, and therefore what a client demands: an
#: ``id_token`` signed with anything else is refused by the client *after* a
#: login that appeared to work. One algorithm, the one this module signs with.
SIGNING_ALGS = [ALGORITHM]


def _b64url_no_pad(digest: bytes) -> str:
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def pkce_s256(verifier: str) -> str:
    """The S256 challenge for a verifier — RFC 7636, the only method allowed."""
    return _b64url_no_pad(hashlib.sha256(verifier.encode("ascii")).digest())


@dataclass(frozen=True, slots=True)
class IdpSigner:
    """The ES256 signing key and its published half.

    Loaded once at startup, from ``GATEWAY_IDP__SIGNING_KEY``. The ``kid`` is
    derived from the public key, so rotating the key (add a new one, retire
    the old) changes the ``kid`` clients see and nothing else has to know.
    """

    key: ECKey
    kid: str

    @classmethod
    def from_pem(cls, pem: str) -> IdpSigner:
        # Environment variables cannot carry newlines, so the deployment value
        # holds the PEM with `\n` escapes (see deploy/.env.example); the
        # replace makes both spellings work, and a PEM pasted with real
        # newlines is untouched by it.
        pem = pem.strip().replace("\\n", "\n")
        try:
            key = ECKey.import_key(pem, parameters={"alg": ALGORITHM})
        except Exception as exc:  # joserfc raises a family of errors on bad PEM
            raise ValueError(
                f"GATEWAY_IDP__SIGNING_KEY is not a readable EC private key (PEM): {exc}"
            ) from exc
        kid = f"idp-{_public_key_digest(key)[:12]}"
        return cls(key=ECKey.import_key(pem, parameters={"kid": kid, "alg": ALGORITHM}), kid=kid)

    def _public_pem(self) -> str:
        return self.key.public_key.public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")

    def public_jwks(self) -> Any:
        """The published half: a JWKS document with the one public key.

        ``Any`` rather than a mapping type: joserfc's importer wants its own
        key-set shape, and a hand-rolled TypedDict for one dictionary is the
        kind of ceremony the validator at the other end never reads.
        """
        public_key = ECKey.import_key(
            self._public_pem(), parameters={"kid": self.kid, "alg": ALGORITHM}
        )
        return {"keys": [public_key.as_dict()]}

    def key_set(self) -> KeySet:
        return KeySet.import_key_set(self.public_jwks())

    def sign_id_token(self, claims: dict[str, Any]) -> str:
        return jwt.encode({"alg": ALGORITHM, "kid": self.kid}, claims, self.key)


def _public_key_digest(key: ECKey) -> str:
    """A stable digest of the public half — the source of the ``kid``."""
    public_pem = key.public_key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(public_pem).hexdigest()


def issuer_urls(settings: IdPSettings) -> dict[str, str]:
    """The endpoint URLs discovery advertises.

    Browser-facing endpoints are built from the public issuer; the endpoints
    only another backend fetches — token, JWKS, userinfo — from the internal
    base, which the compose deployment points at the gateway's own address so
    the chat's exchanges never leave the host and never meet the edge. A
    standards client follows discovery, so the internal-ness is *driving*
    rather than merely enforced; the client-secret check on the token
    endpoint is what keeps it honest (the edge's path refusals are the second
    layer, and the deployment's to configure).
    """
    base = settings.internal_base_url or settings.issuer
    return {
        "issuer": settings.issuer,
        "authorization_endpoint": f"{settings.issuer}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "jwks_uri": f"{base}/oauth/jwks.json",
        "userinfo_endpoint": f"{base}/oauth/userinfo",
        "end_session_endpoint": f"{settings.issuer}/oauth/end_session",
    }


def discovery_document(settings: IdPSettings) -> dict[str, Any]:
    """The ``/.well-known/openid-configuration`` body.

    The shape a standards client requires — Cerea's ``openid-client``
    discovers against this and then refuses to build flows if a field it
    needs is missing — with the subset of the spec this issuer actually
    implements. Claiming more here would promise clients a surface that
    answers 404; the point of a minimal IdP is that the document is honest.
    """
    urls = issuer_urls(settings)
    return {
        **urls,
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": SIGNING_ALGS,
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": ["openid", "profile", "email"],
        "token_endpoint_auth_methods_supported": [
            "client_secret_basic",
            "client_secret_post",
            "none",
        ],
        "claims_supported": [
            "iss",
            "sub",
            "aud",
            "exp",
            "iat",
            "auth_time",
            "nonce",
            "email",
            "email_verified",
            "name",
            "preferred_username",
            "groups",
        ],
    }


def client_by_id(settings: IdPSettings, client_id: str | None) -> Any:
    """The registered client, or ``None`` — including for a missing id."""
    if not client_id:
        return None
    for client in settings.clients:
        if client.client_id == client_id:
            return client
    return None


def mint_code() -> tuple[str, str]:
    """The secret code and its hash. Hashed for the same reason API keys are."""
    code = secrets.token_urlsafe(32)
    return code, hashlib.sha256(code.encode()).hexdigest()


def hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def id_token_claims(
    *,
    settings: IdPSettings,
    client_id: str,
    user: Any,
    nonce: str,
    scope: str,
    now: float,
    ttl_seconds: int,
) -> dict[str, Any]:
    """The identity claims an ``id_token`` carries.

    ``sub`` is the casefolded email — the same string that is the subject of
    the ``issuer="local"`` row, which is the aliasing rule of ADR 0068 made
    concrete. ``email_verified`` is true because the gateway is the authority
    on its own accounts' addresses: the account exists because an operator
    created it. Groups are included because a client may read them, while
    authorisation itself remains a gateway fact (ADR 0069).
    """
    claims: dict[str, Any] = {
        "iss": settings.issuer,
        "sub": user.subject,
        "aud": client_id,
        "iat": int(now),
        "exp": int(now + ttl_seconds),
        "auth_time": int(now),
        "email": user.email,
        "email_verified": True,
    }
    if nonce:
        claims["nonce"] = nonce
    if user.display_name:
        claims["name"] = user.display_name
        claims["preferred_username"] = user.display_name
    held = [membership.group.name for membership in user.memberships if membership.group]
    if held:
        claims["groups"] = held
    if scope:
        claims["scope"] = scope
    return claims


def validate_id_token_hint(token: str, signer: IdpSigner) -> dict[str, Any] | None:
    """Claims of a presented ``id_token_hint``, or ``None``.

    The hint is *advisory* — ``end_session`` ends the session the browser's
    cookie names, and the hint only tells the issuer which client is asking
    (the same role ``client_id`` plays when no hint is present). A bad
    signature therefore means "unverifiable", not "unauthorised": refusing to
    end a session because its hint was stale would keep the one thing the
    person asked to have ended. Expiry is likewise tolerated — the hint is
    proof of *which* token it was, not a credential of its own.
    """
    try:
        decoded = jwt.decode(token, signer.key_set())
        # exp is checked by the registry below; a hint past its expiry is
        # still proof enough of *which* token it was, so expiry is not fatal.
        return dict(decoded.claims)
    except JoseError:
        return None


def safe_post_logout_redirect(value: str | None, issuer: str) -> str | None:
    """A post-logout landing the issuer is willing to send the browser to.

    First-party clients live on the issuer's origin, so the rule is that
    origin or a relative path — the same rule ``_safe_next`` applies to the
    login flow, for the same reason: this redirect is followed by a browser
    that just stopped holding a session, and a permissive check here would
    let a crafted logout land someone on a page of somebody else's choosing.
    """
    if not value:
        return None
    if value.startswith("/") and not value.startswith("//") and not value.startswith("/\\"):
        return value
    if value.rstrip("/") == issuer.rstrip("/"):
        return "/"
    if value.startswith(f"{issuer.rstrip('/')}/"):
        return value
    return None


def new_code_family_name(client_id: str) -> str:
    """The ``(user, client)`` slug minted credentials carry.

    Namespaces the IdP's credentials under their client, distinct from a
    plain ``cerea`` slug a legacy direct login could mint, so revoking one
    family never touches the other.
    """
    return f"idp-{client_id}"[:64]


def user_claims_from_key(user: Any) -> dict[str, Any]:
    """The userinfo body for a resolved access key.

    ``sub`` matches the ``id_token``'s by construction — both are the user
    row's subject, the casefolded email.
    """
    body: dict[str, Any] = {
        "sub": user.subject,
        "email": user.email,
        "email_verified": True,
    }
    if user.display_name:
        body["name"] = user.display_name
        body["preferred_username"] = user.display_name
    held = [membership.group.name for membership in user.memberships if membership.group]
    if held:
        body["groups"] = held
    return body
