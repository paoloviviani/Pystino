"""API key generation, hashing and verification.

Keys are 256 bits of ``secrets.token_urlsafe`` randomness, stored as a bare
SHA-256 digest. That is a deliberate departure from how passwords are stored, and
the reasoning is worth keeping written down because "we hash secrets with Argon2"
is otherwise a reasonable-sounding objection:

A slow KDF exists to make guessing a *low-entropy* secret expensive. There is
nothing to guess here — an attacker must find one value in 2^256, and no amount of
key stretching changes that. Meanwhile the cost is paid on every single API call,
because unlike a login this credential is presented on every request. Argon2 at
sane parameters would add tens of milliseconds to every completion.

What SHA-256 does buy is that a leaked database dump does not yield usable keys.

The stored digest is looked up by a short public prefix, so verification is one
indexed row fetch plus one constant-time comparison.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Final

# 32 bytes of entropy. token_urlsafe returns ~43 characters for this.
_SECRET_BYTES: Final = 32
# Characters of the random part used as the lookup handle. Long enough that
# collisions are not a practical concern, short enough to be readable in logs.
_PREFIX_RANDOM_CHARS: Final = 8


@dataclass(frozen=True, slots=True)
class GeneratedKey:
    """A freshly minted key. ``secret`` is shown once and never stored."""

    secret: str
    prefix: str
    key_hash: str


def generate_api_key(*, environment_prefix: str = "gwk") -> GeneratedKey:
    """Mint a key of the form ``<prefix>_<handle>_<secret>``.

    The handle is embedded in the key so that authentication does not need to
    scan the table: it is parsed out, looked up, and only then is the full value
    compared. Prefixing also makes keys recognisable to secret scanners.
    """
    random_part = secrets.token_urlsafe(_SECRET_BYTES)
    handle = secrets.token_hex(_PREFIX_RANDOM_CHARS // 2)
    secret = f"{environment_prefix}_{handle}_{random_part}"
    return GeneratedKey(
        secret=secret,
        prefix=f"{environment_prefix}_{handle}",
        key_hash=hash_api_key(secret),
    )


def hash_api_key(secret: str) -> str:
    """SHA-256 hex digest of the presented key."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def extract_prefix(secret: str) -> str | None:
    """Parse the lookup handle out of a presented key.

    Returns None for anything that is not shaped like one of our keys, so a
    malformed credential costs one string split rather than a database query.
    """
    parts = secret.split("_")
    if len(parts) < 3:
        return None
    if not parts[0] or not parts[1]:
        return None
    return f"{parts[0]}_{parts[1]}"


def verify_api_key(secret: str, stored_hash: str) -> bool:
    """Constant-time comparison of a presented key against a stored digest."""
    return hmac.compare_digest(hash_api_key(secret), stored_hash)


def parse_authorization_header(value: str | None) -> str | None:
    """Pull the credential out of an ``Authorization`` header.

    Accepts ``Bearer <key>`` and a bare key. Bare keys are accepted because
    several OpenAI-compatible clients send the key without the scheme.
    """
    if not value:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    scheme, _, rest = stripped.partition(" ")
    if scheme.lower() == "bearer":
        candidate = rest.strip()
        return candidate or None
    return stripped
