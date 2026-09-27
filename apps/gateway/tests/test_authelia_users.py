"""Console-managed users for the bundled Authelia (decision D10, ADR 0093 §8)."""

from __future__ import annotations

import fcntl
import os
import threading
import time
from pathlib import Path

import pytest
import yaml
from argon2 import PasswordHasher
from gateway.directory.authelia_users import (
    LOCK_TIMEOUT_SECONDS,
    UsersFile,
    UsersFileError,
    UsersFileLockedError,
    validate_authelia_email,
)

SEED = """users:
  admin:
    disabled: false
    displayname: Admin
    password: '$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA'
    email: admin@example.org
    groups: [users]
    custom_field: kept
"""


@pytest.fixture
def users(tmp_path: Path) -> UsersFile:
    path = tmp_path / "users_database.yml"
    path.write_text(SEED)
    return UsersFile(path)


def test_create_mints_a_password_shown_once(users: UsersFile) -> None:
    user, password = users.create("carol", "carol@example.org", "Carol")
    assert user.groups == ["users"]
    stored = yaml.safe_load(users.path.read_text())["users"]["carol"]
    assert PasswordHasher().verify(stored["password"], password)
    assert password not in users.path.read_text()
    # Everything else survives, unknown fields included.
    assert yaml.safe_load(users.path.read_text())["users"]["admin"]["custom_field"] == "kept"


def test_groups_are_always_exactly_users_and_update_cannot_change_them(users: UsersFile) -> None:
    users.create("carol", "carol@example.org", "")
    assert users.update("carol", disabled=True).groups == ["users"]
    # `update` has no `groups` parameter at all (§8.3) — not merely one that
    # is ignored when passed, so passing it is a hard TypeError, the same
    # protection a removed function argument gives against a caller that
    # still believes this file assigns console groups.
    with pytest.raises(TypeError):
        users.update("carol", groups=["admins"])  # type: ignore[call-arg]


def test_reset_and_delete(users: UsersFile) -> None:
    users.create("carol", "carol@example.org", "")
    new = users.reset_password("carol")
    stored = yaml.safe_load(users.path.read_text())["users"]["carol"]
    assert PasswordHasher().verify(stored["password"], new)
    users.delete("carol")
    assert [u.username for u in users.users()] == ["admin"]


def test_refusals(users: UsersFile) -> None:
    with pytest.raises(UsersFileError, match="lowercase"):
        users.create("Carol", "c@example.org", "")
    with pytest.raises(UsersFileError, match="real email"):
        users.create("carol", "carol@local", "")
    with pytest.raises(UsersFileError, match="already exists"):
        users.create("admin", "a@example.org", "")
    with pytest.raises(UsersFileError, match="last user"):
        users.delete("admin")


class TestStricterEmailValidator:
    """ADR 0093 §6.1: beyond `is_trusted_email`'s own (deliberately looser,
    R2) checks — length caps, RFC 5322 atext, LDH domain labels, an
    alphabetic TLD."""

    def test_accepts_and_normalises_an_ordinary_address(self) -> None:
        assert validate_authelia_email("Carol@Example.ORG") == "carol@example.org"

    def test_refuses_non_ascii(self) -> None:
        with pytest.raises(UsersFileError, match="real email"):
            validate_authelia_email("üser@example.org")

    def test_refuses_a_local_part_over_64_characters(self) -> None:
        local = "a" * 65
        with pytest.raises(UsersFileError, match="local part is too long"):
            validate_authelia_email(f"{local}@example.org")

    def test_refuses_an_address_over_254_characters(self) -> None:
        # Every individual piece stays inside its own limit (local <= 64,
        # each domain label <= 63) — only the address as a whole is too long,
        # which is what this pins rather than a length any single field check
        # would also have caught.
        local = "a" * 64
        domain = ".".join(["b" * 63, "c" * 63, "d" * 63, "org"])
        long_email = f"{local}@{domain}"
        assert len(long_email) > 254
        with pytest.raises(UsersFileError, match="too long"):
            validate_authelia_email(long_email)

    def test_refuses_a_local_part_outside_atext(self) -> None:
        with pytest.raises(UsersFileError, match="local part must be"):
            validate_authelia_email('car"ol@example.org')

    def test_refuses_a_single_label_domain(self) -> None:
        # Caught by `is_trusted_email`'s own "at least one dot" rule before
        # this validator's stricter checks ever run — still refused, just
        # with that check's message rather than a domain-label one.
        with pytest.raises(UsersFileError, match="real email"):
            validate_authelia_email("carol@localhost")

    def test_refuses_a_domain_label_starting_with_a_hyphen(self) -> None:
        with pytest.raises(UsersFileError, match="domain label"):
            validate_authelia_email("carol@-example.org")

    def test_refuses_a_numeric_tld(self) -> None:
        with pytest.raises(UsersFileError, match="last label must be letters"):
            validate_authelia_email("carol@example.123")

    def test_refuses_a_single_character_tld(self) -> None:
        with pytest.raises(UsersFileError, match="last label must be letters"):
            validate_authelia_email("carol@example.o")


class TestLocking:
    """ADR 0093 §8.4, review R4: the sidecar lock, never the data file."""

    def test_two_threads_serialise_rather_than_lose_a_write(self, tmp_path: Path) -> None:
        """Two concurrent `create`s must both land, never one clobbering the other.

        An unlocked load-modify-save would read the same starting file
        twice and the second `_save` would overwrite the first writer's new
        user with a version of the file that never saw it.
        """
        path = tmp_path / "users_database.yml"
        path.write_text(SEED)
        users = UsersFile(path)

        errors: list[Exception] = []

        def create(username: str) -> None:
            try:
                users.create(username, f"{username}@example.org", username.title())
            except Exception as exc:  # recorded, not swallowed
                errors.append(exc)

        threads = [threading.Thread(target=create, args=(name,)) for name in ("bob", "dave")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=LOCK_TIMEOUT_SECONDS + 5)

        assert errors == []
        logins = {u.username for u in users.users()}
        assert logins == {"admin", "bob", "dave"}

    def test_a_lock_held_elsewhere_times_out_into_a_503_type(self, tmp_path: Path) -> None:
        path = tmp_path / "users_database.yml"
        path.write_text(SEED)
        # A short timeout: proving the retry loop fires and eventually gives
        # up does not need production's 10-second budget, only that it is
        # actually used rather than skipped.
        timeout = 0.3
        users = UsersFile(path, lock_timeout=timeout)

        lock_path = tmp_path / "users_database.yml.lock"
        holder_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(holder_fd, fcntl.LOCK_EX)
        try:
            started = time.monotonic()
            with pytest.raises(UsersFileLockedError, match="locked by another writer"):
                users.create("erin", "erin@example.org", "Erin")
            # It actually waited close to the timeout rather than failing
            # immediately — proof this exercised the retry loop, not a stray
            # unconditional refusal.
            assert time.monotonic() - started >= timeout - 0.1
        finally:
            fcntl.flock(holder_fd, fcntl.LOCK_UN)
            os.close(holder_fd)

        # Once released, an ordinary write succeeds — the timeout refused
        # only the contended attempt, not the file forever.
        users.create("erin", "erin@example.org", "Erin")
        assert "erin" in {u.username for u in users.users()}
