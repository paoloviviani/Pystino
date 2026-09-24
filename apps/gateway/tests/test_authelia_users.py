"""Console-managed users for the bundled Authelia (decision D10)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from argon2 import PasswordHasher
from gateway.directory.authelia_users import UsersFile, UsersFileError

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
    user, password = users.create("carol", "carol@example.org", "Carol", ["research"])
    assert user.groups == ["research"]
    stored = yaml.safe_load(users.path.read_text())["users"]["carol"]
    assert PasswordHasher().verify(stored["password"], password)
    assert password not in users.path.read_text()
    # Everything else survives, unknown fields included.
    assert yaml.safe_load(users.path.read_text())["users"]["admin"]["custom_field"] == "kept"


def test_update_reset_delete(users: UsersFile) -> None:
    users.create("carol", "carol@example.org", "", [])
    assert users.update("carol", disabled=True, groups=["a", "a", " b "]).groups == ["a", "b"]
    new = users.reset_password("carol")
    stored = yaml.safe_load(users.path.read_text())["users"]["carol"]
    assert PasswordHasher().verify(stored["password"], new)
    users.delete("carol")
    assert [u.username for u in users.users()] == ["admin"]


def test_refusals(users: UsersFile) -> None:
    with pytest.raises(UsersFileError, match="lowercase"):
        users.create("Carol", "c@example.org", "", [])
    with pytest.raises(UsersFileError, match="real email"):
        users.create("carol", "carol@local", "", [])
    with pytest.raises(UsersFileError, match="already exists"):
        users.create("admin", "a@example.org", "", [])
    with pytest.raises(UsersFileError, match="last user"):
        users.delete("admin")
    with pytest.raises(UsersFileError, match="invalid group"):
        users.create("dave", "d@example.org", "", ["bad/group"])
