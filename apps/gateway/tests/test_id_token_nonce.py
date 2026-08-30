"""The ID-token nonce check: required to match when present, tolerated absent.

Keycloak puts a nonce in every ID token, so requiring it cost nothing — until
gitlab.com's discovery document came along with no nonce claim in
``claims_supported`` (ADR 0044). A provider we want to support demonstrably
omits it, so *absent* is now tolerated. What stays refused, and this is the
part that does the work, is a token whose nonce does not match this browser's
flow — a replayed token must have been minted for a different nonce, and any
nonce at all is caught.

These tests drive the real ``validate_id_token`` through the stub client, so
the signature, issuer, audience and expiry checks all run as they do in
production.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import BEARER_ISSUER, StubOIDCClient
from gateway.config import OIDCSettings
from gateway.oidc import OIDCClient, OIDCError
from joserfc import jwt
from joserfc.jwk import RSAKey


@pytest.fixture(scope="module")
def signing_key() -> RSAKey:
    return RSAKey.generate_key(2048, parameters={"kid": "nonce-test-1"})


@pytest.fixture
def client(signing_key: RSAKey) -> OIDCClient:
    return StubOIDCClient(
        OIDCSettings(issuer=BEARER_ISSUER, client_id="llm-gateway"), signing_key
    )


def id_token(key: RSAKey, *, nonce: str | None = None, **extra: Any) -> str:
    """An ID token the way the stub issuer signs one."""
    import time

    claims: dict[str, Any] = {
        "iss": BEARER_ISSUER,
        "sub": "subject-1",
        "aud": "llm-gateway",
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
    }
    if nonce is not None:
        claims["nonce"] = nonce
    claims.update(extra)
    return jwt.encode({"alg": "RS256"}, claims, key)


class TestNonceTolerance:
    async def test_a_matching_nonce_is_accepted(
        self, client: OIDCClient, signing_key: RSAKey
    ) -> None:
        claims = await client.validate_id_token(
            id_token(signing_key, nonce="flow-nonce"), nonce="flow-nonce"
        )
        assert claims["sub"] == "subject-1"

    async def test_an_absent_nonce_is_accepted(
        self, client: OIDCClient, signing_key: RSAKey
    ) -> None:
        """GitLab's shape: no nonce claim at all. The flow still completes."""
        claims = await client.validate_id_token(id_token(signing_key), nonce="flow-nonce")
        assert claims["sub"] == "subject-1"

    async def test_a_mismatched_nonce_is_refused(
        self, client: OIDCClient, signing_key: RSAKey
    ) -> None:
        """The replay case: a token minted for a different login attempt."""
        with pytest.raises(OIDCError, match="nonce"):
            await client.validate_id_token(
                id_token(signing_key, nonce="someone-elses-nonce"), nonce="flow-nonce"
            )

    async def test_a_nonce_when_none_was_sent_is_refused(
        self, client: OIDCClient, signing_key: RSAKey
    ) -> None:
        """A token naming a nonce we never asked for is not ours either."""
        with pytest.raises(OIDCError, match="nonce"):
            await client.validate_id_token(id_token(signing_key, nonce="surprise"), nonce="")
