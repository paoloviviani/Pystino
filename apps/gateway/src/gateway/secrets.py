"""Encrypting provider credentials at rest.

A provider created through the console has an API key, and that key has to be
readable by the gateway and unreadable in a database dump. Fernet does the
boring parts correctly — authenticated AES-CBC, a random IV per message, a
version byte — and getting those wrong by hand is how this kind of code fails.

Two properties the rest of the codebase depends on
([0027](../../../docs/adr/0027-inference-providers.md)):

* **Rotation is possible.** ``GATEWAY_SECRET_KEY`` takes a comma-separated list.
  The first key encrypts; any key decrypts. So rotating is: prepend a new key,
  restart, re-save each provider at leisure, drop the old key. Without this,
  rotation means re-entering every credential by hand, which means it never
  happens.
* **Failure is loud.** A gateway that cannot decrypt a key must say so, not fall
  back to sending no credentials — that failure arrives disguised as a provider
  outage and costs an afternoon.
"""

from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

logger = logging.getLogger(__name__)

# Prefixed so a stored value is self-describing and a future scheme can be told
# apart from this one without a migration guessing.
_PREFIX = "v1:"


class SecretsUnavailableError(RuntimeError):
    """No usable encryption key is configured."""


class SecretDecryptionError(RuntimeError):
    """A stored secret cannot be read with any configured key."""


def _fernet_key(material: str) -> bytes:
    """Turn arbitrary configured text into a Fernet key.

    Fernet wants 32 url-safe base64 bytes. Operators set a passphrase, so it is
    hashed rather than required to be in that exact form — an operator forced to
    generate base64 by hand tends to reach for something short instead.

    SHA-256 and not a slow KDF on purpose: this input is a high-entropy secret
    from a configuration file, not a human-chosen password being guessed at.
    Stretching it would add startup cost against an attack that does not apply.
    """
    digest = hashlib.sha256(material.encode()).digest()
    return base64.urlsafe_b64encode(digest)


class SecretBox:
    """Encrypts and decrypts values with the configured key(s)."""

    def __init__(self, keys: list[str]) -> None:
        usable = [key.strip() for key in keys if key.strip()]
        self._enabled = bool(usable)
        # MultiFernet decrypts with any key and encrypts with the first, which
        # is exactly the rotation story above.
        self._fernet = MultiFernet([Fernet(_fernet_key(key)) for key in usable]) if usable else None
        self._key_count = len(usable)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def key_count(self) -> int:
        return self._key_count

    def encrypt(self, plaintext: str) -> str:
        if self._fernet is None:
            raise SecretsUnavailableError(
                "GATEWAY_SECRET_KEY is not set, so provider credentials cannot be "
                "stored. Set it to a long random string before creating a provider "
                "with an API key."
            )
        return _PREFIX + self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        if self._fernet is None:
            raise SecretsUnavailableError(
                "GATEWAY_SECRET_KEY is not set, so a stored provider credential cannot be read."
            )
        if not ciphertext.startswith(_PREFIX):
            raise SecretDecryptionError(
                "stored secret is not in a format this gateway understands "
                f"(expected a {_PREFIX!r} prefix)"
            )
        try:
            return self._fernet.decrypt(ciphertext[len(_PREFIX) :].encode()).decode()
        except InvalidToken as exc:
            raise SecretDecryptionError(
                "a stored provider credential could not be decrypted with any "
                "configured key. This usually means GATEWAY_SECRET_KEY changed. "
                "Add the previous value to the comma-separated list to recover, "
                "or re-enter the provider's API key."
            ) from exc

    def rotate(self, ciphertext: str) -> str:
        """Re-encrypt under the current primary key.

        Used when a provider is saved, so ordinary editing gradually migrates
        stored secrets off a retired key without anyone running a script.
        """
        return self.encrypt(self.decrypt(ciphertext))


def hint_for(plaintext: str) -> str:
    """A masked form, enough to tell two keys apart and useless if leaked.

    Shows the first four and last four characters of anything long enough to
    spare them. A short secret is masked entirely rather than half-published —
    "ab…cd" of a six-character key gives away most of it.
    """
    if not plaintext:
        return ""
    if len(plaintext) < 12:
        return "•" * len(plaintext)
    return f"{plaintext[:4]}…{plaintext[-4:]}"
