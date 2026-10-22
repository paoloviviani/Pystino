"""Directory adapters: list the people a directory knows about."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx


@dataclass(frozen=True)
class Entry:
    """One person as a directory describes them."""

    external_id: str
    username: str | None = None
    email: str | None = None
    display_name: str | None = None
    groups: tuple[str, ...] = ()
    active: bool = True
    #: True when external_id is the OIDC subject (SCIM with
    #: externalId): the account can be created before the first login.
    is_subject: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class AdapterError(RuntimeError):
    pass


class Adapter(Protocol):
    async def list_entries(self) -> list[Entry]: ...


class AutheliaFileAdapter:
    """Authelia's file backend: `users_database.yml`, keyed by login name.

    The login name is not the OIDC subject — Authelia mints an opaque `sub` at
    a person's first OIDC use — so entries are linked at first login instead.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    async def list_entries(self) -> list[Entry]:
        import yaml

        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AdapterError(f"cannot read {self.path}: {exc}") from exc
        data = yaml.safe_load(text) or {}
        users = data.get("users")
        if not isinstance(users, dict):
            # An empty or broken file must not read as "everyone left".
            raise AdapterError(f"{self.path} has no users mapping")
        entries = []
        for login, info in users.items():
            info = info or {}
            entries.append(
                Entry(
                    external_id=str(login),
                    username=str(login),
                    email=info.get("email") or None,
                    display_name=info.get("displayname") or None,
                    groups=tuple(str(g) for g in info.get("groups") or ()),
                    active=not bool(info.get("disabled")),
                )
            )
        return entries


def build_adapter(record: Any, config_json: str | None, http: httpx.AsyncClient) -> Adapter:
    """The adapter a provider row names, from its decrypted config."""
    config = json.loads(config_json) if config_json else {}
    if record.sync_adapter == "authelia_file":
        return AutheliaFileAdapter(Path(config.get("path") or "/authelia/users_database.yml"))
    raise AdapterError(f"no pull adapter for {record.sync_adapter!r}")
