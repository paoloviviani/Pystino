"""`pystino bootstrap` creates state once and never overwrites it."""

from __future__ import annotations

import stat
from pathlib import Path

from argon2 import PasswordHasher
from gateway.deploy import bootstrap

DIGEST = PasswordHasher().hash("pw")
ENV = {
    "COMPOSE_PROFILES": "authelia",
    "POSTGRES_PASSWORD": "p",
    "AUTHELIA_ADMIN_USER": "admin",
    "AUTHELIA_ADMIN_EMAIL": "ops@example.org",
    "AUTHELIA_ADMIN_NAME": "O'Brien",
    "AUTHELIA_ADMIN_PASSWORD_DIGEST": DIGEST,
}


def test_authelia_state_is_created_once_and_kept(tmp_path: Path) -> None:
    assert bootstrap.run(ENV, tmp_path) == 0
    key = tmp_path / "keys" / "jwks.pem"
    users = tmp_path / "users_database.yml"
    assert key.read_text().startswith("-----BEGIN RSA PRIVATE KEY-----")
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    text = users.read_text()
    assert "  admin:" in text and "'O''Brien'" in text and DIGEST in text

    first_key = key.read_bytes()
    users.write_text(text + "  bob:\n    disabled: true\n")
    # A second run, even with a different first-user digest, touches nothing.
    assert (
        bootstrap.run(
            {**ENV, "AUTHELIA_ADMIN_PASSWORD_DIGEST": PasswordHasher().hash("x")}, tmp_path
        )
        == 0
    )
    assert key.read_bytes() == first_key
    assert users.read_text().endswith("  bob:\n    disabled: true\n")


def test_users_file_parses_as_yaml_when_available(tmp_path: Path) -> None:
    import pytest

    yaml = pytest.importorskip("yaml")
    bootstrap.run(ENV, tmp_path)
    data = yaml.safe_load((tmp_path / "users_database.yml").read_text())
    assert data["users"]["admin"]["password"] == DIGEST
    assert data["users"]["admin"]["groups"] == ["users"]


def test_a_bad_environment_fails_closed_with_every_problem(tmp_path: Path, capsys) -> None:
    code = bootstrap.run(
        {"COMPOSE_PROFILES": "chat,authelia", "AUTHELIA_ADMIN_USER": "Bad User"}, tmp_path
    )
    out = capsys.readouterr().out
    assert code == 2
    for fragment in ("POSTGRES_PASSWORD", "CHAT_PG_PASSWORD", "AUTHELIA_ADMIN_USER", "argon2"):
        assert fragment in out
    assert not any(tmp_path.iterdir())


def test_profiles_that_need_nothing_do_nothing(tmp_path: Path) -> None:
    assert bootstrap.run({"COMPOSE_PROFILES": "gateway", "POSTGRES_PASSWORD": "p"}, tmp_path) == 0
    assert not any(tmp_path.iterdir())


def test_role_password_is_quoted_not_interpolated() -> None:
    assert bootstrap._quote_literal("a'; DROP ROLE x; --") == "'a''; DROP ROLE x; --'"


def test_pbkdf2_digests_are_accepted(tmp_path: Path) -> None:
    # ./configure (cerea-deploy, ADR 0091 decision 4) mints these; it has no
    # argon2 dependency, so bootstrap must not refuse what it writes.
    for digest in (
        "$pbkdf2-sha512$310000$c2FsdA$aGFzaA",
        "$pbkdf2-sha256$29000$c2FsdA$aGFzaA",
        "$pbkdf2$29000$c2FsdA$aGFzaA",
    ):
        env = {**ENV, "AUTHELIA_ADMIN_PASSWORD_DIGEST": digest}
        assert bootstrap.BootstrapEnv.from_environ(env).problems() == []


def test_an_unrecognised_digest_scheme_is_refused() -> None:
    env = {**ENV, "AUTHELIA_ADMIN_PASSWORD_DIGEST": "$scrypt$plain-text-looking-thing"}
    problems = bootstrap.BootstrapEnv.from_environ(env).problems()
    assert any("pbkdf2" in p for p in problems)
