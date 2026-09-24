"""Is this host able to pull the deployment's images?

While the repositories are private, their GHCR packages are private too, and
pulling needs `docker login ghcr.io` with a token that has read:packages. The
failure otherwise is a bare "denied" from deep inside `docker compose pull`;
this names the fix before anyone gets there.

It can only answer when the Docker client config is readable here — from a
checkout (`uv run pystino …`), or `doctor` on the host. Inside the gateway
image the question has already been answered: the host pulled that image.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

LOGIN_HINT = (
    "The images at {registry} are private while the repositories are private. "
    "Log this host in once, with a GitHub token that has the read:packages scope:\n"
    '    echo "$GHCR_TOKEN" | docker login ghcr.io -u <github-user> --password-stdin\n'
    "(A classic personal access token with only read:packages is enough. Once the "
    "packages are made public, no login is needed.)"
)


def docker_config_path() -> Path:
    return Path(os.environ.get("DOCKER_CONFIG") or Path.home() / ".docker") / "config.json"


def login_state(registry: str, config: Path | None = None) -> str:
    """ "ok", "missing", "unknown" (a credential store we cannot see into), or "n/a"."""
    host = registry.split("/", 1)[0]
    if host != "ghcr.io":
        return "n/a"
    path = config or docker_config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "missing"
    if host in (data.get("auths") or {}) or host in (data.get("credHelpers") or {}):
        return "ok"
    if data.get("credsStore"):
        return "unknown"
    return "missing"


def hint(registry: str, config: Path | None = None) -> str | None:
    state = login_state(registry, config)
    if state == "missing":
        return LOGIN_HINT.format(registry=registry)
    if state == "unknown":
        return (
            f"Docker uses a credential store here, so whether {registry} is logged in "
            "cannot be checked. If `docker compose pull` answers 'denied':\n"
            + LOGIN_HINT.format(registry=registry)
        )
    return None
