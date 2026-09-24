"""Console-managed users for the bundled Authelia (decision D10).

Authelia 4.39 has no admin interface: its file backend *is* the directory,
and the old instructions were "edit users_database.yml by hand and hash the
password with openssl". The gateway already shares that file's volume, so the
console can be the directory's editor: create, disable, change groups, reset a
password. Authelia watches the file and reloads it on change.

Writes are atomic (temp file + rename in the same directory) and keep every
entry and field this module does not know about. Passwords are never chosen
by an administrator and never stored in plaintext: a new or reset password is
minted here, returned once, and written as an argon2id digest.
"""

from __future__ import annotations

import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher

LOGIN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
GROUP = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,127}$")


class UsersFileError(ValueError):
    pass


@dataclass(frozen=True)
class AutheliaUser:
    username: str
    email: str
    display_name: str
    groups: list[str]
    disabled: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "email": self.email,
            "display_name": self.display_name,
            "groups": self.groups,
            "disabled": self.disabled,
        }


class UsersFile:
    def __init__(self, path: Path, hasher: PasswordHasher | None = None) -> None:
        self.path = path
        self.hasher = hasher or PasswordHasher()

    def _load(self) -> dict[str, Any]:
        import yaml

        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError as exc:
            raise UsersFileError(
                f"{self.path} does not exist (is the authelia profile on?)"
            ) from exc
        if not isinstance(data.get("users"), dict):
            raise UsersFileError(f"{self.path} has no users mapping")
        return data

    def _save(self, data: dict[str, Any]) -> None:
        import yaml

        text = (
            "# Authelia's user directory, managed from the Pystino console (and\n"
            "# hot-reloaded by Authelia). Hand edits are kept, comments are not.\n"
            + yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
        )
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".users.")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    @staticmethod
    def _user(login: str, info: dict[str, Any]) -> AutheliaUser:
        return AutheliaUser(
            username=login,
            email=str(info.get("email") or ""),
            display_name=str(info.get("displayname") or login),
            groups=[str(g) for g in info.get("groups") or []],
            disabled=bool(info.get("disabled")),
        )

    def list(self) -> list[AutheliaUser]:
        return [self._user(k, v or {}) for k, v in self._load()["users"].items()]

    def _mint(self) -> tuple[str, str]:
        password = secrets.token_urlsafe(15)
        return password, self.hasher.hash(password)

    @staticmethod
    def _check_groups(groups: list[str]) -> list[str]:
        clean = [g.strip() for g in groups if g and g.strip()]
        for group in clean:
            if not GROUP.match(group):
                raise UsersFileError(f"invalid group name {group!r}")
        return list(dict.fromkeys(clean))

    def create(
        self, username: str, email: str, display_name: str, groups: list[str]
    ) -> tuple[AutheliaUser, str]:
        if not LOGIN.match(username):
            raise UsersFileError("the login must be lowercase letters, digits, '.', '_' or '-'")
        if "@" not in email or "." not in email.split("@")[-1]:
            raise UsersFileError("a real email address is needed (the chat refuses local ones)")
        data = self._load()
        if username in data["users"]:
            raise UsersFileError(f"{username} already exists")
        password, digest = self._mint()
        data["users"][username] = {
            "disabled": False,
            "displayname": display_name or username,
            "password": digest,
            "email": email,
            "groups": self._check_groups(groups) or ["users"],
        }
        self._save(data)
        return self._user(username, data["users"][username]), password

    def update(
        self,
        username: str,
        *,
        email: str | None = None,
        display_name: str | None = None,
        groups: list[str] | None = None,
        disabled: bool | None = None,
    ) -> AutheliaUser:
        data = self._load()
        info = data["users"].get(username)
        if info is None:
            raise UsersFileError(f"no user {username}")
        if email is not None:
            info["email"] = email
        if display_name is not None:
            info["displayname"] = display_name
        if groups is not None:
            info["groups"] = self._check_groups(groups)
        if disabled is not None:
            info["disabled"] = disabled
        self._save(data)
        return self._user(username, info)

    def reset_password(self, username: str) -> str:
        data = self._load()
        info = data["users"].get(username)
        if info is None:
            raise UsersFileError(f"no user {username}")
        password, info["password"] = self._mint()
        self._save(data)
        return password

    def delete(self, username: str) -> None:
        data = self._load()
        if data["users"].pop(username, None) is None:
            raise UsersFileError(f"no user {username}")
        if not data["users"]:
            # Authelia refuses an empty users file; keep the last one (disable it instead).
            raise UsersFileError("cannot delete the last user; disable them instead")
        self._save(data)
