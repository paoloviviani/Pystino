"""`pystino doctor --against-running`: does what runs match what `.env` says?

Read-only. It works out what `docker compose up` would create from the
deployment's compose file and `.env` (the image each service names, the
environment it is given, the named volumes it mounts), then compares that with
the project's containers as `docker inspect` reports them:

- an image that is not the one `.env` names, or a tag rebuilt or pulled since
  the container started;
- an environment key whose value differs (reported by name only — values are
  secrets);
- a named volume the service should mount and does not, or mounts under
  another name (a different project name means different volumes);
- a service that should run and does not, is unhealthy, or (one-shot) failed.

The inspection data comes from the Docker CLI when this process has one (a
development install, run on the host). From the gateway image it has none,
and mounting the Docker socket would hand the container the host: the
`./pystino` shim runs `docker inspect` on the host instead and pipes the JSON
in (`--inspect -`). `.env` is the only source of values; a shell variable set
when `docker compose up` ran is invisible here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The org.opencontainers label the image builds stamp with the source commit.
REVISION_LABEL = "org.opencontainers.image.revision"


def interpolate(text: str, env: dict[str, str]) -> str:
    """Compose's `${X}`, `${X:-default}`, `${X-default}`, `${X:?err}`, `$$`; defaults nest."""
    out: list[str] = []
    i = 0
    while i < len(text):
        if text.startswith("$$", i):
            out.append("$")
            i += 2
        elif text.startswith("${", i):
            value, i = _expression(text, i + 2, env)
            out.append(value)
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _expression(text: str, i: int, env: dict[str, str]) -> tuple[str, int]:
    start = i
    while i < len(text) and (text[i].isalnum() or text[i] == "_"):
        i += 1
    name = text[start:i]
    op = ""
    for candidate in (":-", ":?", "-", "?"):
        if text.startswith(candidate, i):
            op, i = candidate, i + len(candidate)
            break
    # The default runs to the matching brace; it may hold ${…} of its own.
    depth, begin = 0, i
    while i < len(text):
        if text.startswith("${", i):
            depth, i = depth + 1, i + 2
            continue
        if text[i] == "}":
            if depth == 0:
                break
            depth -= 1
        i += 1
    arg, i = text[begin:i], i + 1
    present = name in env
    value = env.get(name, "")
    if op == ":-":
        return (value if value else interpolate(arg, env)), i
    if op == "-":
        return (value if present else interpolate(arg, env)), i
    return value, i  # ${X}, and :? / ? (the error is compose's to raise)


@dataclass
class Expected:
    service: str
    image: str
    environment: dict[str, str]
    volumes: list[str]  # the docker volume names it should mount
    one_shot: bool


def expected_services(compose: dict[str, Any], env: dict[str, str]) -> dict[str, Expected]:
    project = env.get("COMPOSE_PROJECT_NAME", "")
    profiles = {p for p in env.get("COMPOSE_PROFILES", "").split(",") if p}
    declared = compose.get("volumes") or {}
    wanted: dict[str, Expected] = {}
    for name, service in (compose.get("services") or {}).items():
        if service.get("profiles") and not set(service["profiles"]) & profiles:
            continue
        raw_env = service.get("environment") or {}
        if isinstance(raw_env, list):
            raw_env = dict(item.split("=", 1) for item in raw_env if "=" in item)
        environment = {
            key: interpolate(_scalar(value), env)
            for key, value in raw_env.items()
            if value is not None
        }
        volumes = []
        for mount in service.get("volumes") or []:
            source = mount.get("source") if isinstance(mount, dict) else str(mount).split(":")[0]
            if source in declared:
                custom = (declared[source] or {}).get("name")
                volumes.append(interpolate(custom, env) if custom else f"{project}_{source}")
        wanted[name] = Expected(
            service=name,
            image=interpolate(str(service.get("image", "")), env),
            environment=environment,
            volumes=volumes,
            one_shot=str(service.get("restart", "")) == "no",
        )
    return wanted


def _scalar(value: Any) -> str:
    # What compose passes for a YAML scalar: booleans as true/false.
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def collect(deploy_dir: Path) -> dict[str, Any] | None:
    """What the shim collects, collected here — only where a Docker CLI exists."""
    if shutil.which("docker") is None:
        return None

    def run(*args: str) -> str:
        return subprocess.run(  # noqa: S603 — fixed argv
            ["docker", *args],  # noqa: S607 — docker from PATH
            capture_output=True,
            text=True,
            timeout=60,
            cwd=deploy_dir,
            check=False,
        ).stdout

    ids = run("compose", "ps", "-aq").split()
    containers = json.loads(run("inspect", *ids) or "[]") if ids else []
    refs = sorted({c["Config"]["Image"] for c in containers} | {c["Image"] for c in containers})
    images = json.loads(run("image", "inspect", *refs) or "[]") if refs else []
    return {"containers": containers, "images": images}


@dataclass
class Findings:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def compare(wanted: dict[str, Expected], data: dict[str, Any], project: str) -> Findings:
    found = Findings()
    images = {i["Id"]: i for i in data.get("images", [])}
    for ref in data.get("images", []):
        for tag in ref.get("RepoTags") or []:
            images.setdefault(tag, ref)
    by_service: dict[str, dict[str, Any]] = {}
    for container in data.get("containers", []):
        labels = container.get("Config", {}).get("Labels") or {}
        if labels.get("com.docker.compose.project") != project:
            continue
        by_service[labels.get("com.docker.compose.service", "")] = container

    for name, want in sorted(wanted.items()):
        container = by_service.get(name)
        if container is None:
            if not want.one_shot:
                found.warnings.append(f"{name}: no container (docker compose up -d starts it)")
            continue
        state = container.get("State", {})
        runs = container.get("Config", {}).get("Image", "")
        if runs != want.image:
            found.warnings.append(f"{name}: runs {runs}, .env names {want.image}")
        else:
            current = images.get(runs, {}).get("Id")
            if current and current != container.get("Image"):
                found.warnings.append(
                    f"{name}: {runs} was rebuilt or pulled after the container started"
                )
        labels = images.get(container.get("Image", ""), {}).get("Config", {}).get("Labels") or {}
        if labels.get(REVISION_LABEL):
            found.notes.append(f"{name}: {runs} (revision {labels[REVISION_LABEL]})")

        env_list = container.get("Config", {}).get("Env") or []
        have = dict(item.split("=", 1) for item in env_list if "=" in item)
        drifted = sorted(k for k, v in want.environment.items() if have.get(k) != v)
        if drifted:
            found.warnings.append(
                f"{name}: environment differs from what .env gives now: {', '.join(drifted)}"
            )

        mounts = container.get("Mounts") or []
        mounted = {m.get("Name") for m in mounts if m.get("Type") == "volume"}
        for volume in want.volumes:
            if volume not in mounted:
                found.errors.append(f"{name}: does not mount volume {volume}")

        status = state.get("Status")
        health = (state.get("Health") or {}).get("Status")
        if want.one_shot:
            if status == "exited" and state.get("ExitCode", 0) != 0:
                found.errors.append(f"{name}: exited with code {state.get('ExitCode')}")
        elif status != "running":
            found.errors.append(f"{name}: {status}")
        elif health == "unhealthy":
            found.errors.append(f"{name}: unhealthy")

    for name in sorted(set(by_service) - set(wanted)):
        found.notes.append(f"{name}: running, but not in the active profiles")
    return found
