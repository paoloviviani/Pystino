"""Console-managed users for the bundled Authelia (decision D10).

Authelia 4.39 has no admin interface: its file backend *is* the directory,
and the old instructions were "edit users_database.yml by hand and hash the
password with openssl". The gateway already shares that file's volume, so the
console can be the directory's editor: create, disable, reset a password.
Authelia watches the file and reloads it on change.

Writes are atomic (temp file + rename in the same directory) and keep every
entry and field this module does not know about. Passwords are never chosen
by an administrator and never stored in plaintext: a new or reset password is
minted here, returned once, and written as an argon2id digest.

**Groups are not this file's business any more** (ADR 0093 §8.3): every entry
carries exactly ``groups: ["users"]``, because Authelia's own token never
feeds group membership here — the bundled row is ``group_source=none`` — and
letting this file also assign console groups would be a second place group
membership silently disagreed with the one the console actually reads
(``memberships``). ``create`` never takes a groups argument, and ``update``
no longer accepts one at all.

**Locking** (ADR 0093 §8.4, review R4): every load-modify-save runs under an
advisory ``fcntl.flock`` on a *sidecar* lock file, `users_database.yml.lock`,
never the data file itself. Locking the data file would be lost at the very
first ``os.replace``, because a lock is held on an inode and the atomic
replace's whole point is to swap inodes — a second writer opening the new
file after a rename opens a file that was never locked. The sidecar's path
never changes, so every writer, across every process, contends for the same
inode for as long as the deployment exists.
"""

from __future__ import annotations

import fcntl
import os
import re
import secrets
import tempfile
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher

from gateway.email_normalize import is_trusted_email

LOGIN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

# The stricter validator ADR 0093 §6.1 asks for, beyond `is_trusted_email`'s
# own (deliberately looser, R2) checks: RFC 5322 atext for the local part
# rather than "no leading/trailing/doubled dot", LDH domain labels, and an
# alphabetic TLD. `is_trusted_email` guards an *automatic trust decision*
# (a link, an admin-email match) made against many kinds of claimed address;
# this guards what an administrator may type into a login file meant to be
# read by both Authelia and this deployment's own binding-at-sign-in (§8.2).
_ATEXT = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
_LOCAL_PART = re.compile(rf"^{_ATEXT}(\.{_ATEXT})*$")
_LDH_LABEL = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_ALPHA_TLD = re.compile(r"^[A-Za-z]{2,}$")

#: The lock's own timeout (§8.4): long enough that one slow writer never
#: fails an ordinary concurrent request, short enough that a truly stuck
#: holder (a crashed process that never released it — flock is released by
#: the kernel on process exit, but a hung one has not exited) fails the
#: request rather than hanging it forever.
LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_POLL_SECONDS = 0.05


class UsersFileError(ValueError):
    pass


class UsersFileLockedError(UsersFileError):
    """Another writer held the lock past `LOCK_TIMEOUT_SECONDS` (a 503, not a 400).

    Distinct from the base class so a caller can map it to "try again in a
    moment" rather than "you asked for something invalid" — the two are not
    the same refusal, and collapsing them would tell an admin retrying a
    reset that their input was wrong.
    """


@contextmanager
def locked_users_file(path: Path, *, timeout: float = LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """Hold the sidecar lock for `path` for the duration of the block.

    Shared by `UsersFile`'s own load-modify-save methods and by
    `gw/deploy/bootstrap.py`, which takes the same lock creating the file for
    the first time — a bootstrap racing a console write (two `compose up`s,
    or a retried one-shot service) is exactly the concurrent-writer case this
    exists for, not a special case of it.
    """
    lock_path = path.parent / f"{path.name}.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise UsersFileLockedError(
                        f"{path} is locked by another writer; try again in a moment."
                    ) from None
                time.sleep(_LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def validate_authelia_email(email: str) -> str:
    """The normalised address, or raise `UsersFileError` naming why not (§6.1)."""
    normalized, trusted = is_trusted_email(email)
    if not trusted:
        raise UsersFileError(
            "a real email address is needed (the chat refuses local ones)"
        )
    if len(normalized) > 254:
        raise UsersFileError("the email address is too long (254 characters at most)")
    local, _, domain = normalized.partition("@")
    if len(local) > 64:
        raise UsersFileError("the local part is too long (64 characters at most)")
    if not _LOCAL_PART.match(local):
        raise UsersFileError("the local part must be letters, digits, the usual symbols, or dots")
    # `is_trusted_email` already refused a domain with no dot at all, so
    # `labels` is guaranteed at least 2 here — no separate check for it.
    labels = domain.split(".")
    if any(len(label) > 63 or not _LDH_LABEL.match(label) for label in labels):
        raise UsersFileError(
            "each domain label must be 1-63 letters, digits or hyphens, "
            "and may not start or end with a hyphen"
        )
    if not _ALPHA_TLD.match(labels[-1]):
        raise UsersFileError("the domain's last label must be letters only, two or more")
    return normalized


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
    def __init__(
        self,
        path: Path,
        hasher: PasswordHasher | None = None,
        *,
        lock_timeout: float = LOCK_TIMEOUT_SECONDS,
    ) -> None:
        self.path = path
        self.hasher = hasher or PasswordHasher()
        #: Overridable so a test can prove the timeout path fires without
        #: actually waiting the production 10 seconds for it.
        self.lock_timeout = lock_timeout

    def _lock(self) -> AbstractContextManager[None]:
        return locked_users_file(self.path, timeout=self.lock_timeout)

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
            # The rename is durable once the directory entry pointing at the
            # new inode is itself synced — without this, a power loss right
            # after `os.replace` can leave the directory still pointing at
            # the old (now-unlinked) file on some filesystems/orderings.
            dir_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
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

    def users(self) -> list[AutheliaUser]:
        # A read, not a load-modify-save: the atomic replace means this
        # always sees either the whole old file or the whole new one, never
        # a partial write, so it takes no lock.
        return [self._user(k, v or {}) for k, v in self._load()["users"].items()]

    def _mint(self) -> tuple[str, str]:
        password = secrets.token_urlsafe(15)
        return password, self.hasher.hash(password)

    def create(self, username: str, email: str, display_name: str) -> tuple[AutheliaUser, str]:
        if not LOGIN.match(username):
            raise UsersFileError("the login must be lowercase letters, digits, '.', '_' or '-'")
        email = validate_authelia_email(email)
        with self._lock():
            data = self._load()
            if username in data["users"]:
                raise UsersFileError(f"{username} already exists")
            password, digest = self._mint()
            data["users"][username] = {
                "disabled": False,
                "displayname": display_name or username,
                "password": digest,
                "email": email,
                # Always exactly this (§8.3): groups and roles are the
                # console's alone, and this file's own `group_source=none`
                # means nothing here is ever read back as a grant.
                "groups": ["users"],
            }
            self._save(data)
            return self._user(username, data["users"][username]), password

    def update(
        self,
        username: str,
        *,
        email: str | None = None,
        display_name: str | None = None,
        disabled: bool | None = None,
    ) -> AutheliaUser:
        if email is not None:
            email = validate_authelia_email(email)
        with self._lock():
            data = self._load()
            info = data["users"].get(username)
            if info is None:
                raise UsersFileError(f"no user {username}")
            if email is not None:
                info["email"] = email
            if display_name is not None:
                info["displayname"] = display_name
            if disabled is not None:
                info["disabled"] = disabled
            self._save(data)
            return self._user(username, info)

    def reset_password(self, username: str) -> str:
        with self._lock():
            data = self._load()
            info = data["users"].get(username)
            if info is None:
                raise UsersFileError(f"no user {username}")
            password, info["password"] = self._mint()
            self._save(data)
            return password

    def delete(self, username: str) -> None:
        with self._lock():
            data = self._load()
            if data["users"].pop(username, None) is None:
                raise UsersFileError(f"no user {username}")
            if not data["users"]:
                # Authelia refuses an empty users file; keep the last one (disable it instead).
                raise UsersFileError("cannot delete the last user; disable them instead")
            self._save(data)

    def normalize_groups(self) -> dict[str, list[str]]:
        """Force every entry's `groups` to exactly `["users"]` (§8.3, §13.4).

        Returns the groups actually *removed*, per login that had any beyond
        `["users"]` — empty for a login that already matched exactly. The
        lock and a read happen every call (cheap, and the only way to know
        whether anything needs fixing), but the file is only rewritten when
        some entry's `groups` differed from `["users"]` — which is what
        keeps every start after the first a no-op write, and this safe to
        call unconditionally on every boot rather than a one-shot migration
        that has to remember it already ran.
        """
        with self._lock():
            data = self._load()
            removed: dict[str, list[str]] = {}
            changed = False
            for login, info in data["users"].items():
                current = [str(g) for g in (info or {}).get("groups") or []]
                if current == ["users"]:
                    continue
                changed = True
                if extra := [g for g in current if g != "users"]:
                    removed[login] = extra
                info["groups"] = ["users"]
            if changed:
                self._save(data)
            return removed
