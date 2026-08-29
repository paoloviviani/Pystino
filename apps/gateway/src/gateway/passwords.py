"""Password hashing for local sign-in (ADR 0043).

A thin wrapper over ``pwdlib`` rather than calls scattered across the routers
and the CLI, so there is exactly one place that knows how passwords are hashed
and one place to change when parameters are raised. ``pwdlib`` was chosen over
the abandoned ``passlib`` (ADR 0011's reasoning applies: one maintainer wants
code that is alive), with ``argon2-cffi`` as the backend — Argon2id, a
memory-hard KDF, which is the right answer for a low-entropy human secret.

The deliberate contrast is `security.py`, which hashes API keys with bare
SHA-256. That is not an inconsistency: a key is 256 bits of entropy and is
presented on every request, where key stretching would tax the hot path to
defend nothing; a password is whatever the person chose and is presented only
at login, where spending tens of milliseconds is exactly the point.
"""

from __future__ import annotations

from pwdlib import PasswordHash

from gateway.config import LocalAuthSettings

# `recommended()` resolves to Argon2id now and tracks the library's answer to
# "what should we be using" across releases, which is the behaviour we want
# from a component nobody should have to think about again.
_hasher = PasswordHash.recommended()

# Hashed once at import so an unknown email can be answered with a real
# verification run. Without it, "no such user" returns in microseconds while
# "wrong password" returns in tens of milliseconds, and response timing tells
# a prober which addresses exist before they guess a single password.
_DUMMY_HASH = _hasher.hash("not-a-real-password")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    return _hasher.verify(password, stored_hash)


def verify_dummy(password: str) -> None:
    """Burn the same Argon2 work a real verification would, then discard it."""
    _hasher.verify(password, _DUMMY_HASH)


def verify_and_rehash(password: str, stored_hash: str) -> tuple[bool, str | None]:
    """Verify, and return a replacement hash when parameters have moved on.

    ``(True, None)`` is the ordinary outcome — valid, nothing to do. A
    non-None hash means the stored one was made under weaker parameters than
    the library now recommends, and the caller should persist the replacement
    at this login, the one moment the plaintext is in hand anyway.
    """
    return _hasher.verify_and_update(password, stored_hash)


def validate_password(password: str, settings: LocalAuthSettings) -> str:
    """Apply the deployment's policy, returning the password or raising.

    Length only, deliberately. Character-class rules ("must contain a digit")
    are measured out of every modern guidance (NIST SP 800-63B among them) as
    making passwords *harder to remember and not harder to guess*; length is
    the axis that actually buys entropy. Argon2id behind this makes the rest
    of the trade-off the attacker's problem.
    """
    if len(password) < settings.min_password_length:
        raise ValueError(
            f"password must be at least {settings.min_password_length} characters"
        )
    return password
