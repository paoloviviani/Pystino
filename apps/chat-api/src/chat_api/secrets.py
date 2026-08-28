"""Encrypting the refresh token at rest.

Deliberately a copy of the gateway's approach rather than an import of it: the
two services share no code, and a security primitive is a poor first exception
to that rule. It is thirty lines and the reasoning is identical — Fernet does
authenticated encryption with a random IV correctly, and a comma-separated key
list makes rotation possible without re-authenticating every user.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

_PREFIX = "v1:"


class SecretsUnavailableError(RuntimeError):
    """No usable encryption key is configured."""


class SecretDecryptionError(RuntimeError):
    """A stored value cannot be read with any configured key."""


def _fernet_key(passphrase: str) -> bytes:
    """Fernet wants 32 url-safe base64 bytes; operators set a passphrase."""
    return base64.urlsafe_b64encode(hashlib.sha256(passphrase.encode("utf-8")).digest())


class SecretBox:
    def __init__(self, keys: list[str]) -> None:
        usable = [key for key in keys if key]
        self._fernet = MultiFernet([Fernet(_fernet_key(key)) for key in usable]) if usable else None

    @property
    def available(self) -> bool:
        return self._fernet is not None

    def encrypt(self, plaintext: str) -> str:
        if self._fernet is None:
            raise SecretsUnavailableError("CHAT_SECRET_KEY is not set")
        return _PREFIX + self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, stored: str) -> str:
        if self._fernet is None:
            raise SecretsUnavailableError("CHAT_SECRET_KEY is not set")
        if not stored.startswith(_PREFIX):
            raise SecretDecryptionError("stored value is not in a format this version writes")
        try:
            return self._fernet.decrypt(stored[len(_PREFIX) :].encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            # Loud, never a silent fallback: a session that cannot be decrypted
            # must end, not quietly proceed unauthenticated.
            raise SecretDecryptionError("no configured key can decrypt this value") from exc
