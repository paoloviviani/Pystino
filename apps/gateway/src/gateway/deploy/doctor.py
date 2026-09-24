"""`pystino doctor`: what is wrong with this deployment directory, all at once.

Static checks — it reads `.env` and the files it names, and asks the Docker CLI
for its Compose version when one is on PATH. With `--against-running` it also
compares that with the running containers (`running.py`). It never starts or
stops anything, so it is safe to run against a live install.
"""

from __future__ import annotations

import json
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from gateway.deploy import envfile, registry, running, stackfiles

MIN_COMPOSE = (2, 24)

#: Keys every deployment needs, then the ones each profile adds.
REQUIRED = (
    "PYSTINO_CONFIG_VERSION",
    "COMPOSE_PROJECT_NAME",
    "COMPOSE_FILE",
    "PUBLIC_ORIGIN",
    "SITE_ADDRESS",
    "POSTGRES_PASSWORD",
)
REQUIRED_BY_PROFILE = {
    "gateway": (
        "GATEWAY_SECRET_KEY",
        "GATEWAY_SESSION_SECRET",
        "OIDC_ISSUER",
        "OIDC_CONSOLE_CLIENT_SECRET",
    ),
    "chat": ("CHAT_PG_PASSWORD", "CHAT_SECRET_KEY", "CEREA_VERSION", "OIDC_CHAT_CLIENT_SECRET"),
    # The gateway refuses to start with engine=http and no placeholder key.
    "redaction": ("REDACTION_PLACEHOLDER_KEY",),
    "authelia": (
        "AUTHELIA_COOKIE_DOMAIN",
        "AUTHELIA_SESSION_SECRET",
        "AUTHELIA_HMAC_SECRET",
        "AUTHELIA_STORAGE_KEY",
        "AUTHELIA_CONSOLE_CLIENT_DIGEST",
        "AUTHELIA_CHAT_CLIENT_DIGEST",
        "AUTHELIA_ADMIN_PASSWORD_DIGEST",
    ),
}
#: Variables the old stack read that mean something is half-migrated.
RETIRED = {
    "GATEWAY_IDP__ENABLED": "the house IdP is gone (ADR 0088)",
    "CHAT_REPO": "chat images are pulled or built from CEREA_SRC now",
    "TLS_DIRECTIVE_EDGE": "the edge shape is TLS_MODE=upstream",
    "GATEWAY_OIDC__ADMIN_GROUPS": "removed by ADR 0069",
    "GATEWAY_LOCAL_AUTH__ENABLED": "the password door is gone; use `pystino admin grant`",
}
BACKUP = (
    ".env (every secret: GATEWAY_SECRET_KEY decrypts provider keys, "
    "AUTHELIA_STORAGE_KEY every user's subject)",
    "volume postgres-data (gateway ledger, users, the chat's knowledge store)",
    "volume chat-mongo-data (conversations)",
    "volume authelia-config (signing key, users file) and authelia-data (subjects, consents)",
)


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def compose_version() -> tuple[int, int] | None:
    if shutil.which("docker") is None:
        return None
    try:
        out = subprocess.run(
            ["docker", "compose", "version", "--short"],  # noqa: S607 — docker from PATH
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(\d+)\.(\d+)", out)
    return (int(match.group(1)), int(match.group(2))) if match else None


def check(deploy_dir: Path, *, probe_docker: bool = True) -> Report:
    report = Report()
    env_path = deploy_dir / ".env"
    if not env_path.is_file():
        report.errors.append(f"no .env in {deploy_dir} — run `pystino init` first")
        return report
    try:
        values = envfile.read(env_path)
    except envfile.EnvFileError as exc:
        report.errors.append(f".env does not parse: {exc}")
        return report

    mode = stat.S_IMODE(env_path.stat().st_mode)
    if mode & 0o077:
        report.warnings.append(f".env is mode {oct(mode)}; it holds every secret — chmod 600 it")

    profiles = {p for p in values.get("COMPOSE_PROFILES", "").split(",") if p}
    for key in REQUIRED:
        if not values.get(key):
            report.errors.append(f"{key} is missing or empty")
    for profile in sorted(profiles):
        for key in REQUIRED_BY_PROFILE.get(profile, ()):
            if not values.get(key):
                report.errors.append(f"{key} is missing or empty (needed by profile {profile!r})")
    for key, why in RETIRED.items():
        if key in values:
            report.warnings.append(f"{key} is set but no longer read: {why}")

    if not values.get("PUBLIC_ORIGIN", "").startswith("https://"):
        report.errors.append("PUBLIC_ORIGIN must be an https:// origin")
    if values.get("TLS_MODE") == "internal":
        report.notes.append("TLS_MODE=internal: browsers will warn; fine for development only")

    for name in values.get("COMPOSE_FILE", "").split(":"):
        if not name:
            continue
        path = Path(name) if Path(name).is_absolute() else deploy_dir / name
        if not path.is_file():
            report.errors.append(f"COMPOSE_FILE names {name}, which does not exist")
    # The directory as seen here: under the shim that is /deploy, while
    # PYSTINO_DEPLOY_DIR is the host's path to the same place.
    proxy_d = deploy_dir / "proxy.d"
    if not proxy_d.is_dir():
        report.errors.append(f"{proxy_d} is missing (the proxy mounts it; create it empty)")

    if probe_docker:
        version = compose_version()
        if version is None:
            report.notes.append("docker compose not found here; skipped the version check")
        elif version < MIN_COMPOSE:
            report.errors.append(
                f"docker compose {version[0]}.{version[1]} is too old; "
                f"{MIN_COMPOSE[0]}.{MIN_COMPOSE[1]}+ is needed "
                "(profiles with required: false, --wait)"
            )

    reg = values.get("PYSTINO_REGISTRY", "")
    if not values.get("PYSTINO_SRC") and stackfiles.stack_dir() != stackfiles.IMAGE_STACK_DIR:
        note = registry.hint(reg)
        if note:
            report.warnings.append(note)

    report.notes.append("back up: " + "; ".join(BACKUP))
    return report


def check_running(deploy_dir: Path, report: Report, *, inspect: str | None = None) -> None:
    """Add what differs between `.env` and the running containers to the report."""
    values = envfile.read(deploy_dir / ".env")
    first = (values.get("COMPOSE_FILE") or "compose.yaml").split(":")[0]
    compose_path = Path(first) if Path(first).is_absolute() else deploy_dir / first
    if not compose_path.is_file():
        report.errors.append(f"--against-running: {compose_path} is not readable here")
        return
    if inspect == "-":
        data = json.load(sys.stdin)
    elif inspect:
        data = json.loads(Path(inspect).read_text(encoding="utf-8"))
    else:
        data = running.collect(deploy_dir)
    if data is None:
        report.errors.append(
            "--against-running needs `docker inspect` data: run it through ./pystino, "
            "which collects it on the host, or pass --inspect FILE"
        )
        return
    wanted = running.expected_services(yaml.safe_load(compose_path.read_text()), values)
    found = running.compare(wanted, data, values.get("COMPOSE_PROJECT_NAME", ""))
    report.errors += found.errors
    report.warnings += found.warnings
    report.notes += found.notes
    if not (found.errors or found.warnings):
        report.notes.append(f"the running containers match .env ({len(wanted)} services)")
