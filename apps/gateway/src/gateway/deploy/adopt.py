"""`pystino adopt <old deploy dir>`: carry an installer-made deployment over.

Reads what the old bash installers left behind — `deploy/.env`, and for a
bundled Authelia the generated `deploy/idp/authelia-configuration.yml`,
`users_database.yml` and `authelia-jwks-rsa.pem` — and writes the new
deployment directory. It never starts, stops or edits anything in the old
directory, so the old install stays a working way back until cutover.

Three things decide whether an adopted install is the *same* deployment, and
each is carried verbatim, never re-minted:

- the compose project name (`llm-platform`), so the new compose file mounts the
  same named volumes — Postgres, Mongo, Valkey, authelia-data, caddy-data;
- Authelia's storage encryption key, because authelia-data holds every user's
  opaque subject encrypted with it, and users are keyed downstream on
  `(issuer, subject)`: a new key would silently make everyone a new person;
- the gateway's secret key, which decrypts every stored provider credential.

The users file and signing key are staged under `adopt/authelia-config/` for
`pystino bootstrap --import-only`, which copies them into the new authelia-config
volume only where nothing is there yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from gateway.deploy import envfile, presets
from gateway.deploy.envfile import Section
from gateway.deploy.init import AUTHELIA_INTERNAL, CONFIG_VERSION

OLD_PROJECT = "llm-platform"


class AdoptError(ValueError):
    pass


@dataclass
class AdoptOptions:
    old_deploy_dir: Path
    new_deploy_dir: Path
    #: acme | internal | upstream. The old edge shape is `upstream`.
    tls: str
    mode: str = "dist"
    project: str = OLD_PROJECT
    pystino_src: Path | None = None
    cerea_src: Path | None = None
    http_port: int | None = None
    https_port: int | None = None


@dataclass
class AdoptResult:
    sections: list[Section]
    #: Files to stage for `bootstrap --import-only`: relative path → bytes.
    staged: dict[str, bytes] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _authelia_secrets(config_text: str) -> dict[str, str]:
    import yaml  # PyYAML: a declared dependency of the gateway

    doc = yaml.safe_load(config_text) or {}
    session = doc.get("session") or {}
    cookies = session.get("cookies") or [{}]
    oidc = (doc.get("identity_providers") or {}).get("oidc") or {}
    clients = {c.get("client_id"): c for c in oidc.get("clients") or []}
    out = {
        "AUTHELIA_SESSION_SECRET": str(session.get("secret") or ""),
        "AUTHELIA_STORAGE_KEY": str((doc.get("storage") or {}).get("encryption_key") or ""),
        "AUTHELIA_HMAC_SECRET": str(oidc.get("hmac_secret") or ""),
        "AUTHELIA_COOKIE_DOMAIN": str(cookies[0].get("domain") or ""),
        # The old generator hashed client secrets with SHA512-crypt ($6$);
        # Authelia verifies those digests as readily as argon2id, so they are
        # carried as they are rather than re-hashed from the plaintext.
        "AUTHELIA_CONSOLE_CLIENT_DIGEST": str(
            (clients.get("pystino-console") or {}).get("client_secret") or ""
        ),
        "AUTHELIA_CHAT_CLIENT_DIGEST": str((clients.get("cerea") or {}).get("client_secret") or ""),
    }
    missing = [key for key, value in out.items() if not value]
    if missing:
        raise AdoptError(f"the old Authelia configuration lacks {', '.join(missing)}")
    return out


def _first_user(users_text: str) -> tuple[str, str, str, str]:
    import yaml

    users = (yaml.safe_load(users_text) or {}).get("users") or {}
    if not users:
        raise AdoptError("the old users_database.yml holds no user")
    login, entry = next(iter(users.items()))
    return (
        str(login),
        str(entry.get("email") or ""),
        str(entry.get("displayname") or login),
        str(entry.get("password") or ""),
    )


def _chat_pg_password(url: str) -> str:
    parts = urlsplit(url)
    return parts.password or ""


def build(options: AdoptOptions, release: dict[str, str]) -> AdoptResult:
    """Old files in, new `.env` sections and staged files out. Reads only."""
    old = options.old_deploy_dir
    env_path = old / ".env"
    if not env_path.is_file():
        raise AdoptError(
            f"{env_path} not found: point adopt at the old install's deploy/ directory"
        )
    values = envfile.read(env_path)
    notes: list[str] = []

    origin = values.get("PUBLIC_ORIGIN", "").rstrip("/")
    if not origin.startswith("https://"):
        raise AdoptError(
            f"PUBLIC_ORIGIN in the old .env is {origin!r}; an https origin is required"
        )
    host = urlsplit(origin).hostname or ""
    if options.tls == "acme":
        site, directive = f"https://{host}", ""
    elif options.tls == "internal":
        site, directive = f"https://{host}", "tls internal"
    elif options.tls == "upstream":
        site, directive = "http://:80", ""
    else:
        raise AdoptError("tls must be acme, internal or upstream (the old edge shape is upstream)")

    bundled = values.get("IDP_BUNDLED", "")
    if values.get("GATEWAY_IDP__ENABLED", "").lower() == "true":
        notes.append(
            "the old install used the house IdP, which the new stack does not ship: enable "
            "a provider with link_local_by_email so each person's first login keeps their "
            "account (report §10.2)"
        )
    if bundled == "keycloak":
        raise AdoptError(
            "bundled Keycloak is not carried over (decision D2); adopt it as an external IdP "
            "with --idp external once D2 is settled"
        )

    profiles = ["gateway"]
    if values.get("CHAT_SECRET_KEY") or values.get("CHAT_REPO"):
        profiles.append("chat")
    if values.get("GATEWAY_REDACTION__ENGINE", "noop") == "http":
        profiles.append("redaction")
    if values.get("FETCH_BACKEND") == "playwright":
        profiles.append("fetch")
    if bundled == "authelia":
        profiles.append("authelia")

    # Images: where they come from is the only thing the mode changes.
    images: list[tuple[str, str]] = []
    if options.mode == "dev":
        if options.pystino_src is None:
            raise AdoptError("--mode dev needs --pystino-src")
        stack = options.pystino_src / "deploy" / "stack"
        files = [stack / "compose.yaml", stack / "compose.build.yaml"]
        cerea_src = options.cerea_src or (
            Path(values["CHAT_REPO"]) if values.get("CHAT_REPO") else None
        )
        if cerea_src is not None:
            files.append(stack / "compose.build-cerea.yaml")
        images += [
            ("COMPOSE_FILE", ":".join(map(str, files))),
            ("PYSTINO_SRC", str(options.pystino_src)),
            ("PYSTINO_REGISTRY", "local"),
            ("PYSTINO_VERSION", "dev"),
        ]
        if cerea_src is not None:
            images += [
                ("CEREA_SRC", str(cerea_src)),
                ("CEREA_REGISTRY", "local"),
                ("CEREA_VERSION", "dev"),
            ]
            notes.append(
                f"the chat will build from {cerea_src}; its tree must be committed and clean"
            )
        else:
            images += [
                ("CEREA_REGISTRY", release["CEREA_REGISTRY"]),
                ("CEREA_VERSION", release["CEREA_VERSION"]),
            ]
    else:
        images += [
            ("COMPOSE_FILE", "compose.yaml"),
            ("PYSTINO_REGISTRY", release["PYSTINO_REGISTRY"]),
            ("PYSTINO_VERSION", release["PYSTINO_VERSION"]),
            ("CEREA_REGISTRY", release["CEREA_REGISTRY"]),
            ("CEREA_VERSION", release["CEREA_VERSION"]),
            ("PYSTINO_RELEASE_CEREA_VERSION", release["CEREA_VERSION"]),
        ]
    for key in (
        "POSTGRES_IMAGE",
        "VALKEY_IMAGE",
        "MONGO_IMAGE",
        "PLAYWRIGHT_IMAGE",
        "PLAYWRIGHT_VERSION",
    ):
        if key in release:
            images.append((key, release[key]))

    def carry(*keys: str) -> list[tuple[str, str]]:
        return [(key, values[key]) for key in keys if key in values]

    identity: list[tuple[str, str]] = []
    authelia: list[tuple[str, str]] = []
    staged: dict[str, bytes] = {}
    chat_secret = values.get("CHAT_OIDC_CLIENT_SECRET") or values.get("CHAT_IDP_CLIENT_SECRET", "")
    if bundled == "authelia":
        idp = old / "idp"
        config_path = idp / "authelia-configuration.yml"
        users_path = idp / "users_database.yml"
        key_path = idp / "authelia-jwks-rsa.pem"
        for path in (config_path, users_path, key_path):
            if not path.is_file():
                raise AdoptError(
                    f"{path} not found; a bundled-Authelia install keeps it in deploy/idp"
                )
        secrets = _authelia_secrets(config_path.read_text(encoding="utf-8"))
        login, email, name, digest = _first_user(users_path.read_text(encoding="utf-8"))
        admin_email = values.get("IDP_ADMIN_EMAIL") or email
        authelia = [
            *secrets.items(),
            ("AUTHELIA_ADMIN_USER", login),
            ("AUTHELIA_ADMIN_EMAIL", admin_email),
            ("AUTHELIA_ADMIN_NAME", name),
            # Only used if the users file were ever absent; the staged file
            # below is what the volume actually receives.
            ("AUTHELIA_ADMIN_PASSWORD_DIGEST", digest),
        ]
        identity = [
            ("OIDC_ISSUER", f"{origin}/authelia"),
            ("OIDC_INTERNAL_BASE_URL", AUTHELIA_INTERNAL),
            ("OIDC_CONSOLE_CLIENT_ID", "pystino-console"),
            ("OIDC_CONSOLE_CLIENT_SECRET", values.get("GATEWAY_OIDC__CLIENT_SECRET", "")),
            ("OIDC_CHAT_CLIENT_ID", "cerea"),
            ("OIDC_CHAT_CLIENT_SECRET", chat_secret),
            ("OIDC_GROUPS_CLAIM", values.get("GATEWAY_OIDC__GROUPS_CLAIM", "groups")),
            ("OIDC_AUDIENCE", values.get("GATEWAY_OIDC__ACCESS_TOKEN_AUDIENCE") or "pystino-api"),
            ("OIDC_LINK_LOCAL_BY_EMAIL", values.get("GATEWAY_OIDC__LINK_LOCAL_BY_EMAIL", "false")),
            ("PYSTINO_BOOTSTRAP_ADMIN_EMAIL", admin_email),
        ]
        staged = {
            "authelia-config/users_database.yml": users_path.read_bytes(),
            "authelia-config/keys/jwks.pem": key_path.read_bytes(),
        }
    else:
        issuer = values.get("GATEWAY_OIDC__ISSUER", "")
        if not issuer:
            raise AdoptError(
                "no bundled Authelia and no GATEWAY_OIDC__ISSUER: nothing to sign in with"
            )
        identity = [
            ("OIDC_ISSUER", issuer),
            ("OIDC_INTERNAL_BASE_URL", ""),
            ("OIDC_CONSOLE_CLIENT_ID", values.get("GATEWAY_OIDC__CLIENT_ID", "pystino-console")),
            ("OIDC_CONSOLE_CLIENT_SECRET", values.get("GATEWAY_OIDC__CLIENT_SECRET", "")),
            ("OIDC_CHAT_CLIENT_ID", values.get("CHAT_OIDC_CLIENT_ID", "cerea")),
            ("OIDC_CHAT_CLIENT_SECRET", chat_secret),
            ("OIDC_GROUPS_CLAIM", values.get("GATEWAY_OIDC__GROUPS_CLAIM", "groups")),
            ("OIDC_AUDIENCE", values.get("GATEWAY_OIDC__ACCESS_TOKEN_AUDIENCE") or "pystino-api"),
            ("PYSTINO_BOOTSTRAP_ADMIN_EMAIL", values.get("IDP_ADMIN_EMAIL", "")),
        ]

    if values.get("CODE_AGENTS_ENABLED") == "true":
        notes.append(
            "CODE_AGENTS_ENABLED is on: the relay is not part of the core stack. Attach it "
            "through the component hook (report §9) — add its compose file to COMPOSE_FILE "
            "and its Caddy snippet to proxy.d — or leave it on the old stack until the thin "
            "agent replaces it"
        )
    for gone in ("TLS_DIRECTIVE", "HTTPS_PORT", "PUBLIC_HOST", "CHAT_REPO", "SSL_CERT_FILE"):
        if gone in values:
            notes.append(f"{gone} is not carried: the new stack derives or no longer needs it")

    preset = presets.get("team" if "redaction" in profiles else "homelab")
    preset_values = dict(preset.values)
    for key in list(preset_values):
        if key in values:
            preset_values[key] = values[key]

    https_port = options.https_port or int(values.get("HTTPS_PORT") or 443)
    http_port = options.http_port or (8443 if options.tls == "upstream" else 80)
    sections: list[Section] = [
        (
            "Written by `pystino adopt` from " + str(old) + ".\n"
            "Every secret was carried over verbatim; nothing was re-minted.",
            [("PYSTINO_CONFIG_VERSION", CONFIG_VERSION)],
        ),
        (
            "Compose — the old project name, so the same volumes are mounted",
            [
                ("COMPOSE_PROJECT_NAME", options.project),
                ("COMPOSE_PROFILES", ",".join(profiles)),
                ("PYSTINO_DEPLOY_DIR", str(options.new_deploy_dir)),
                ("PYSTINO_PRESET", "adopted"),
            ],
        ),
        ("Images", images),
        (
            "Public origin and TLS (mode: " + options.tls + ")",
            [
                ("PUBLIC_ORIGIN", origin),
                ("TLS_MODE", options.tls),
                ("SITE_ADDRESS", site),
                ("TLS_DIRECTIVE", directive),
                ("HTTP_PORT", str(http_port)),
                ("HTTPS_PORT", str(https_port)),
                ("GATEWAY_PORT", values.get("GATEWAY_PORT", "8000")),
                *carry("ACME_EMAIL"),
            ],
        ),
        (
            "Database",
            [
                *carry("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"),
                ("CHAT_PG_PASSWORD", _chat_pg_password(values.get("CHAT_PG_URL", ""))),
            ],
        ),
        (
            "Gateway secrets",
            carry("GATEWAY_SECRET_KEY", "GATEWAY_SESSION_SECRET", "CHAT_SECRET_KEY"),
        ),
        ("Upstream", carry("GATEWAY_UPSTREAM__BASE_URL", "GATEWAY_UPSTREAM__API_KEY")),
        (
            "Features (the old values where the old install set them)",
            [*preset_values.items(), *carry("CODE_AGENTS_ENABLED", "CHAT_APP_NAME")],
        ),
        ("Identity: OIDC only", identity),
    ]
    if authelia:
        sections.append(
            ("Bundled Authelia, carried verbatim — the storage key above all", authelia)
        )
    return AdoptResult(sections=sections, staged=staged, notes=notes)
