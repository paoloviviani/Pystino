"""API key generation, hashing and verification."""

from __future__ import annotations

from datetime import timedelta

from gateway.models import ApiKey
from gateway.security import (
    extract_prefix,
    generate_api_key,
    hash_api_key,
    parse_authorization_header,
    verify_api_key,
)
from gateway.types import utcnow


class TestGeneration:
    def test_keys_are_unique(self) -> None:
        secrets = {generate_api_key().secret for _ in range(200)}
        assert len(secrets) == 200

    def test_key_carries_its_own_lookup_prefix(self) -> None:
        generated = generate_api_key(environment_prefix="gwk")
        assert generated.secret.startswith(generated.prefix)
        assert extract_prefix(generated.secret) == generated.prefix

    def test_prefix_is_configurable(self) -> None:
        assert generate_api_key(environment_prefix="test").prefix.startswith("test_")

    def test_secret_has_high_entropy(self) -> None:
        """No slow KDF is used, so the entropy has to be real."""
        generated = generate_api_key()
        random_part = generated.secret[len(generated.prefix) + 1 :]
        assert len(random_part) >= 40

    def test_stored_hash_is_sha256_hex(self) -> None:
        generated = generate_api_key()
        assert len(generated.key_hash) == 64
        assert generated.key_hash == hash_api_key(generated.secret)
        # The secret itself must not be recoverable from what we store.
        assert generated.secret not in generated.key_hash


class TestVerification:
    def test_correct_key_verifies(self) -> None:
        generated = generate_api_key()
        assert verify_api_key(generated.secret, generated.key_hash)

    def test_wrong_key_does_not_verify(self) -> None:
        first = generate_api_key()
        second = generate_api_key()
        assert not verify_api_key(second.secret, first.key_hash)

    def test_altered_key_does_not_verify(self) -> None:
        generated = generate_api_key()
        tampered = generated.secret[:-1] + ("a" if generated.secret[-1] != "a" else "b")
        assert not verify_api_key(tampered, generated.key_hash)


class TestPrefixExtraction:
    def test_rejects_malformed_credentials_without_a_lookup(self) -> None:
        """A malformed key must cost a string split, not a database query."""
        assert extract_prefix("not-a-key") is None
        assert extract_prefix("only_two") is None
        assert extract_prefix("") is None
        assert extract_prefix("_leading_underscore") is None


class TestAuthorizationHeader:
    def test_bearer_scheme(self) -> None:
        assert parse_authorization_header("Bearer abc123") == "abc123"
        assert parse_authorization_header("bearer abc123") == "abc123"

    def test_bare_key_is_accepted(self) -> None:
        """Several OpenAI-compatible clients omit the scheme."""
        assert parse_authorization_header("gwk_deadbeef_secret") == "gwk_deadbeef_secret"

    def test_missing_or_empty_header(self) -> None:
        assert parse_authorization_header(None) is None
        assert parse_authorization_header("") is None
        assert parse_authorization_header("   ") is None
        assert parse_authorization_header("Bearer ") is None


class TestUsability:
    def test_fresh_key_is_usable(self) -> None:
        assert ApiKey(prefix="p", key_hash="h").is_usable()

    def test_revoked_key_is_not_usable(self) -> None:
        assert not ApiKey(prefix="p", key_hash="h", revoked_at=utcnow()).is_usable()

    def test_expired_key_is_not_usable(self) -> None:
        expired = ApiKey(prefix="p", key_hash="h", expires_at=utcnow() - timedelta(seconds=1))
        assert not expired.is_usable()

    def test_future_expiry_is_usable(self) -> None:
        valid = ApiKey(prefix="p", key_hash="h", expires_at=utcnow() + timedelta(days=1))
        assert valid.is_usable()
