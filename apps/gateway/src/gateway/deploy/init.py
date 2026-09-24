"""`pystino init`: from a handful of answers to a complete `.env`.

`build_env` is pure — options in, sections out — so every rule is a unit test,
not a run on a live box. The command around it only asks questions, writes the
file atomically at mode 0600, and prints what to do next.

What it deliberately does not do: start anything, or ask for a secret it can
mint. The only secrets it takes are the ones only the operator knows (an
upstream provider key, an external IdP's client secret).
"""

from __future__ import annotations

import dataclasses
import ipaddress
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from argon2 import PasswordHasher

from gateway.deploy import presets
from gateway.deploy.envfile import Section

CONFIG_VERSION = "1"
AUTHELIA_INTERNAL = "http://authelia:9091/authelia"
TLS_MODES = ("acme", "internal", "upstream")
MODES = ("dev", "dist")


class InitError(ValueError):
    """An answer that would produce a deployment which cannot work."""


@dataclass
class InitOptions:
    origin: str
    admin_email: str
    preset: str = "team"
    mode: str = "dist"
    tls: str = "acme"
    idp: str = "authelia"  # "authelia" | "external"
    project: str = "pystino"
    deploy_dir: Path = field(default_factory=Path.cwd)
    # Development mode only.
    pystino_src: Path | None = None
    cerea_src: Path | None = None
    build_revision: str = "unknown"
    # Ports on the host. None means "the default for the TLS mode".
    http_port: int | None = None
    https_port: int | None = None
    gateway_port: int = 8000
    acme_email: str = ""
    # Bundled Authelia's first person.
    admin_user: str = "admin"
    admin_name: str = ""
    admin_password: str = ""  # empty → minted, and returned for printing once
    # External IdP.
    oidc_issuer: str = ""
    oidc_internal_base_url: str = ""
    oidc_console_client_id: str = "pystino-console"
    oidc_console_client_secret: str = ""
    oidc_chat_client_id: str = "cerea"
    oidc_chat_client_secret: str = ""
    oidc_groups_claim: str = "groups"
    # Satellite preset: the central Pystino's origin (its /v1 and its IdP).
    central_url: str = ""
    # Upstream inference provider (the gateway's; the chat's own in `generic`).
    upstream_base_url: str = "https://api.cortecs.ai/v1"
    upstream_api_key: str = ""
    # Image origin; defaults come from release.env.
    cerea_version: str = ""
    cerea_image: str = ""
    # The agent-machine component: the /code panel and machine enrolment.
    agents: bool = False


@dataclass
class InitResult:
    sections: list[Section]
    #: The minted first password, shown once and stored nowhere in plaintext.
    admin_password: str | None


def _token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def _hex(nbytes: int = 32) -> str:
    return secrets.token_hex(nbytes)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _default_acme_email(host: str) -> str:
    return "admin@example.org" if _is_ip(host) else f"admin@{host}"


def _origin_parts(origin: str) -> tuple[str, str, int | None]:
    parts = urlsplit(origin.strip())
    if parts.scheme != "https":
        raise InitError(
            f"the public origin must be https:// (got {origin!r}); every mode, "
            "upstream included, is served to browsers over TLS"
        )
    if not parts.hostname or parts.path not in ("", "/") or parts.query:
        raise InitError(f"the public origin must be scheme://host[:port], got {origin!r}")
    return f"https://{parts.netloc}", parts.hostname, parts.port


def build_env(
    options: InitOptions,
    release: dict[str, str],
    *,
    token: Callable[[int], str] = _token,
    hexkey: Callable[[int], str] = _hex,
    hasher: PasswordHasher | None = None,
) -> InitResult:
    """Every value the stack reads, derived from the answers. Pure."""
    options = dataclasses.replace(options)  # presets may adjust it; never the caller's
    hasher = hasher or PasswordHasher()
    if options.mode not in MODES:
        raise InitError(f"mode must be one of {MODES}")
    if options.tls not in TLS_MODES:
        raise InitError(f"tls must be one of {TLS_MODES}")
    preset = presets.get(options.preset)
    origin, host, origin_port = _origin_parts(options.origin)
    if "@" not in options.admin_email or "." not in options.admin_email.split("@")[-1]:
        # The chat refuses an issuer-local address such as admin@local with an
        # error that reads like a broken login; refuse it here instead.
        raise InitError("the administrator email must be a real address (user@domain.tld)")

    # --- TLS and ports -------------------------------------------------------
    if options.tls == "acme":
        if _is_ip(host) or "." not in host:
            raise InitError(
                "tls=acme needs a public DNS name; use tls=internal for an IP or dev box"
            )
        site, directive = f"https://{host}", ""
    elif options.tls == "internal":
        site, directive = f"https://{host}", "tls internal"
    else:
        # TLS ends in front of us (the NetBird edge): plain HTTP inside.
        site, directive = "http://:80", ""
    https_port = options.https_port or origin_port or 443
    http_port = options.http_port or 80

    # --- Cerea without a local gateway (ADR 0082) --------------------------------
    standalone = "gateway" not in preset.profiles
    chat_backend: list[tuple[str, str]] = []
    if preset.name == "satellite":
        central = options.central_url.strip().rstrip("/")
        if not central.startswith("https://"):
            raise InitError("the satellite preset needs --central-url https://<central pystino>")
        if options.upstream_api_key:
            # A stored key would bill every user on this site to one account.
            raise InitError("a satellite stores no API key: every call carries the user's token")
        if options.idp != "external":
            # One directory: the central gateway only accepts its own issuer's
            # tokens on /v1 (ADR 0082).
            options.idp = "external"
        if not options.oidc_issuer:
            options.oidc_issuer = f"{central}/authelia"
        chat_backend = [
            ("CHAT_OPENAI_BASE_URL", f"{central}/v1"),
            ("CHAT_OPENAI_API_KEY", ""),
            ("CHAT_USE_USER_TOKEN", "true"),
        ]
    elif preset.name == "generic":
        if not options.upstream_api_key:
            raise InitError(
                "the generic preset needs the endpoint's API key (PYSTINO_UPSTREAM_API_KEY)"
            )
        chat_backend = [
            ("CHAT_OPENAI_BASE_URL", options.upstream_base_url),
            ("CHAT_OPENAI_API_KEY", options.upstream_api_key),
            # Forced, not offered: user-token mode would send the person's IdP
            # access token to a third party (ADR 0082).
            ("CHAT_USE_USER_TOKEN", "false"),
        ]

    # --- profiles --------------------------------------------------------------
    profiles = list(preset.profiles)
    if options.idp == "authelia":
        profiles.append("authelia")
    elif options.idp != "external":
        raise InitError("idp must be 'authelia' or 'external'")

    # --- images ----------------------------------------------------------------
    image_items: list[tuple[str, str]] = []
    if options.mode == "dev":
        src = options.pystino_src
        if src is None:
            raise InitError("development mode needs the Pystino checkout (--pystino-src)")
        stack = Path(src) / "deploy" / "stack"
        compose_files = [stack / "compose.yaml", stack / "compose.build.yaml"]
        if options.cerea_src is not None:
            compose_files.append(stack / "compose.build-cerea.yaml")
        image_items += [
            ("COMPOSE_FILE", ":".join(str(p) for p in compose_files)),
            ("PYSTINO_SRC", str(src)),
            # `local/…:dev` can never collide with, or be pushed over, a
            # published tag.
            ("PYSTINO_REGISTRY", "local"),
            ("PYSTINO_VERSION", "dev"),
            ("BUILD_REVISION", options.build_revision),
        ]
        if options.cerea_src is not None:
            image_items += [
                ("CEREA_SRC", str(options.cerea_src)),
                ("CEREA_REGISTRY", "local"),
                ("CEREA_VERSION", "dev"),
            ]
        else:
            image_items += [
                ("CEREA_REGISTRY", release.get("CEREA_REGISTRY", "ghcr.io/paoloviviani")),
                ("CEREA_VERSION", options.cerea_version or release["CEREA_VERSION"]),
            ]
    else:
        image_items += [
            ("COMPOSE_FILE", "compose.yaml"),
            ("PYSTINO_REGISTRY", release["PYSTINO_REGISTRY"]),
            ("PYSTINO_VERSION", release["PYSTINO_VERSION"]),
            ("CEREA_REGISTRY", release.get("CEREA_REGISTRY", release["PYSTINO_REGISTRY"])),
            ("CEREA_VERSION", options.cerea_version or release["CEREA_VERSION"]),
            # What the manifest pinned, so `upgrade` can tell a deliberate
            # CEREA_VERSION override from a pin that simply went stale.
            ("PYSTINO_RELEASE_CEREA_VERSION", release["CEREA_VERSION"]),
        ]
    if options.cerea_image:
        image_items.append(("CEREA_IMAGE", options.cerea_image))
    if "CEREA_IMAGE" in release and not options.cerea_image and options.cerea_src is None:
        # The manifest's digest-pinned chat image: what this release was tested with.
        image_items.append(("CEREA_IMAGE", release["CEREA_IMAGE"]))
    for key in (
        "POSTGRES_IMAGE",
        "VALKEY_IMAGE",
        "MONGO_IMAGE",
        "PLAYWRIGHT_IMAGE",
        "PLAYWRIGHT_VERSION",
    ):
        if key in release:
            image_items.append((key, release[key]))

    # --- identity ---------------------------------------------------------------
    minted_password: str | None = None
    identity: list[tuple[str, str]] = [("PYSTINO_BOOTSTRAP_ADMIN_EMAIL", options.admin_email)]
    authelia: list[tuple[str, str]] = []
    if options.idp == "authelia":
        if _is_ip(host) or "." not in host:
            # Authelia needs a Domain cookie, and browsers refuse one on an IP
            # or a dotless name: nobody would stay signed in (ADR 0084).
            raise InitError(
                "the bundled Authelia needs a dotted host name, not an IP; "
                "add a DNS or hosts entry, or bring an external IdP"
            )
        console_secret, chat_secret = token(32), token(32)
        password = options.admin_password or token(18)
        if not options.admin_password:
            minted_password = password
        identity += [
            ("OIDC_ISSUER", f"{origin}/authelia"),
            ("OIDC_INTERNAL_BASE_URL", AUTHELIA_INTERNAL),
            ("OIDC_CONSOLE_CLIENT_ID", "pystino-console"),
            ("OIDC_CONSOLE_CLIENT_SECRET", console_secret),
            ("OIDC_CHAT_CLIENT_ID", "cerea"),
            ("OIDC_CHAT_CLIENT_SECRET", chat_secret),
            ("OIDC_GROUPS_CLAIM", "groups"),
            ("OIDC_AUDIENCE", "pystino-api"),
            # The provider row the gateway seeds is then an Authelia one: the
            # users-file sync (unconfirmed until a first dry run) and the
            # console's user management (D10).
            ("OIDC_KIND", "authelia"),
            # Authelia publishes no end_session_endpoint; the chat signs out
            # through the portal's own /logout, `{redirect}` = where to land.
            ("OIDC_LOGOUT_URL", f"{origin}/authelia/logout?rd={{redirect}}"),
        ]
        authelia = [
            ("AUTHELIA_COOKIE_DOMAIN", host),
            ("AUTHELIA_SESSION_SECRET", hexkey(32)),
            ("AUTHELIA_HMAC_SECRET", hexkey(32)),
            # Never rotated: it decrypts the store holding every user's opaque
            # subject, which is their identity downstream.
            ("AUTHELIA_STORAGE_KEY", hexkey(32)),
            ("AUTHELIA_CONSOLE_CLIENT_DIGEST", hasher.hash(console_secret)),
            ("AUTHELIA_CHAT_CLIENT_DIGEST", hasher.hash(chat_secret)),
            ("AUTHELIA_ADMIN_USER", options.admin_user),
            ("AUTHELIA_ADMIN_EMAIL", options.admin_email),
            ("AUTHELIA_ADMIN_NAME", options.admin_name or options.admin_user),
            # The digest, not the password: bootstrap writes it into the users
            # file on first run, and the plaintext is shown once by init.
            ("AUTHELIA_ADMIN_PASSWORD_DIGEST", hasher.hash(password)),
        ]
    else:
        if not options.oidc_issuer:
            raise InitError("an external IdP needs --oidc-issuer")
        if not standalone and not options.oidc_console_client_secret:
            raise InitError("an external IdP needs the console client secret")
        if "chat" in profiles and not options.oidc_chat_client_secret:
            raise InitError("an external IdP with the chat needs the chat client secret")
        identity += [
            ("OIDC_ISSUER", options.oidc_issuer.rstrip("/")),
            ("OIDC_INTERNAL_BASE_URL", options.oidc_internal_base_url.rstrip("/")),
            ("OIDC_CONSOLE_CLIENT_ID", options.oidc_console_client_id),
            ("OIDC_CONSOLE_CLIENT_SECRET", options.oidc_console_client_secret),
            ("OIDC_CHAT_CLIENT_ID", options.oidc_chat_client_id),
            ("OIDC_CHAT_CLIENT_SECRET", options.oidc_chat_client_secret),
            ("OIDC_GROUPS_CLAIM", options.oidc_groups_claim),
            ("OIDC_AUDIENCE", "pystino-api"),
        ]

    sections: list[Section] = [
        (
            "Written by `pystino init`. The deployment's one configuration file:\n"
            "edit values here (or with `pystino set`), then `docker compose up -d --wait`.\n"
            "Mode 0600 — it holds every secret. Back it up with the volumes.",
            [("PYSTINO_CONFIG_VERSION", CONFIG_VERSION)],
        ),
        (
            "Compose: project, which files, which add-ons",
            [
                ("COMPOSE_PROJECT_NAME", options.project),
                ("COMPOSE_PROFILES", ",".join(profiles)),
                ("PYSTINO_DEPLOY_DIR", str(options.deploy_dir)),
                ("PYSTINO_PRESET", preset.name),
            ],
        ),
        ("Images — where they come from is the only difference between modes", image_items),
        (
            "Public origin and TLS (mode: " + options.tls + ")",
            [
                ("PUBLIC_ORIGIN", origin),
                ("TLS_MODE", options.tls),
                ("SITE_ADDRESS", site),
                ("TLS_DIRECTIVE", directive),
                ("HTTP_PORT", str(http_port)),
                ("HTTPS_PORT", str(https_port)),
                ("GATEWAY_PORT", str(options.gateway_port)),
                ("ACME_EMAIL", options.acme_email or _default_acme_email(host)),
                # What `/` serves: the gateway, or — with no gateway here — a
                # redirect to the chat.
                ("PROXY_DEFAULT", "chat" if standalone else "gateway"),
            ],
        ),
        (
            "Database (the chat's database lives on the same instance)",
            [
                ("POSTGRES_USER", "gateway"),
                ("POSTGRES_PASSWORD", token(24)),
                ("POSTGRES_DB", "gateway"),
                ("CHAT_PG_PASSWORD", token(24)),
            ],
        ),
        (
            "Gateway secrets. GATEWAY_SECRET_KEY encrypts provider keys at rest:\n"
            "losing it means re-entering every provider credential.",
            [
                ("GATEWAY_SECRET_KEY", token(32)),
                ("GATEWAY_SESSION_SECRET", token(32)),
                ("CHAT_SECRET_KEY", token(32)),
                # The HMAC key redaction derives placeholders from. Minted even
                # when redaction is off, so switching the profile on later needs
                # no new secret; it must then stay stable for as long as the
                # transcripts it labelled are kept.
                ("REDACTION_PLACEHOLDER_KEY", token(32)),
            ],
        ),
        (
            "Upstream inference provider (more are added in the console)",
            [
                ("GATEWAY_UPSTREAM__BASE_URL", options.upstream_base_url),
                # Standalone presets hand the key to the chat instead (below).
                ("GATEWAY_UPSTREAM__API_KEY", "" if standalone else options.upstream_api_key),
            ],
        ),
        ("Preset: " + preset.name, [*preset.values, *chat_backend]),
        (
            "Agent machines (report §9): the thin agent dials the chat over WSS with its\n"
            "enrolment token (client opencode-enrollment). No relay, no extra service.",
            [("CODE_AGENTS_ENABLED", "true" if options.agents else "")],
        ),
        ("Identity: OIDC only (ADR 0088)", identity),
    ]
    if authelia:
        sections.append(
            ("Bundled Authelia. Client secrets appear here only as argon2id digests.", authelia)
        )
    return InitResult(sections=sections, admin_password=minted_password)
