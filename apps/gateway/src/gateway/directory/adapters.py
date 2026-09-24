"""Directory adapters: list the people a directory knows about."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

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
    #: True when external_id is the OIDC subject (Keycloak, SCIM with
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


class KeycloakAdminAdapter:
    """Keycloak's admin REST API, through a service-account client.

    The client needs `serviceAccountsEnabled` and the realm-management roles
    `view-users` and `query-groups`. Keycloak's `sub` is the user id, so every
    entry is keyed by the subject and accounts can exist before first login.
    """

    PAGE = 200

    def __init__(
        self,
        issuer: str,
        client_id: str,
        client_secret: str,
        http: httpx.AsyncClient,
        *,
        base_url: str = "",
    ) -> None:
        parts = urlsplit(issuer.rstrip("/"))
        marker = "/realms/"
        if marker not in parts.path:
            raise AdapterError(f"{issuer} is not a Keycloak realm issuer (…/realms/<realm>)")
        root_path, realm = parts.path.split(marker, 1)
        root = f"{parts.scheme}://{parts.netloc}{root_path}"
        self.root = (base_url or root).rstrip("/")
        self.realm = realm.strip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.http = http

    async def _token(self) -> str:
        response = await self.http.post(
            f"{self.root}/realms/{self.realm}/protocol/openid-connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        )
        if response.status_code >= 400:
            raise AdapterError(
                f"Keycloak refused the service-account login ({response.status_code})"
            )
        return str(response.json()["access_token"])

    async def list_entries(self) -> list[Entry]:
        headers = {"authorization": f"Bearer {await self._token()}"}
        base = f"{self.root}/admin/realms/{self.realm}"
        users: list[dict[str, Any]] = []
        first = 0
        while True:
            page = await self.http.get(
                f"{base}/users", params={"first": first, "max": self.PAGE}, headers=headers
            )
            if page.status_code >= 400:
                raise AdapterError(f"Keycloak refused the user listing ({page.status_code})")
            batch = page.json()
            users.extend(batch)
            if len(batch) < self.PAGE:
                break
            first += self.PAGE
        entries = []
        for user in users:
            groups = await self.http.get(f"{base}/users/{user['id']}/groups", headers=headers)
            names = [g.get("name", "") for g in groups.json()] if groups.status_code < 400 else []
            name = " ".join(p for p in (user.get("firstName"), user.get("lastName")) if p)
            entries.append(
                Entry(
                    external_id=str(user["id"]),
                    username=user.get("username"),
                    email=user.get("email"),
                    display_name=name or None,
                    groups=tuple(n for n in names if n),
                    active=bool(user.get("enabled", True)),
                    is_subject=True,
                )
            )
        return entries


def build_adapter(record: Any, config_json: str | None, http: httpx.AsyncClient) -> Adapter:
    """The adapter a provider row names, from its decrypted config."""
    config = json.loads(config_json) if config_json else {}
    if record.sync_adapter == "authelia_file":
        return AutheliaFileAdapter(Path(config.get("path") or "/authelia/users_database.yml"))
    if record.sync_adapter == "keycloak_admin":
        if not config.get("client_id") or not config.get("client_secret"):
            raise AdapterError("the Keycloak adapter needs a service-account client id and secret")
        return KeycloakAdminAdapter(
            record.issuer,
            config["client_id"],
            config["client_secret"],
            http,
            base_url=record.internal_base_url.rsplit("/realms/", 1)[0]
            if record.internal_base_url
            else "",
        )
    raise AdapterError(f"no pull adapter for {record.sync_adapter!r}")
