"""`pystino upgrade <version>`: move a distributed install to another release.

Run from the *target* version's image (the host shim does that), so the new
release's own code writes the new release's files. It:

1. refuses a development install (those upgrade with `git pull`), and refuses
   when the image it runs from is not the version asked for;
2. backs up `compose.yaml` and `.env` as `*.bak-<old version>`;
3. writes the release's `compose.yaml` and sets every pin in `.env` from the
   release manifest — PYSTINO_VERSION, CEREA_VERSION and the upstream images —
   keeping every other value, secrets included, exactly as it was;
4. migrates `.env` keys across PYSTINO_CONFIG_VERSION when a release needs it.

It never runs `docker compose` itself: the operator pulls and brings the stack
up, and the `migrate` and `bootstrap` services apply the rest on that `up`.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from gateway.deploy import envfile, stackfiles
from gateway.deploy.init import CONFIG_VERSION

#: Keys a release manifest owns; everything else in .env belongs to the operator.
PINNED = (
    "PYSTINO_VERSION",
    "CEREA_VERSION",
    "POSTGRES_IMAGE",
    "VALKEY_IMAGE",
    "MONGO_IMAGE",
    "PLAYWRIGHT_IMAGE",
    "PLAYWRIGHT_VERSION",
)


class UpgradeError(RuntimeError):
    pass


@dataclass
class UpgradePlan:
    old_version: str
    new_version: str
    changes: dict[str, str]
    #: Pins the operator overrode on purpose (e.g. a newer CEREA_VERSION), kept.
    kept_overrides: dict[str, str]


def _migrate_config(values: dict[str, str]) -> dict[str, str]:
    """Key renames between config versions. Version 1 is the first."""
    current = values.get("PYSTINO_CONFIG_VERSION", "")
    if current == CONFIG_VERSION:
        return {}
    if not current:
        raise UpgradeError(".env has no PYSTINO_CONFIG_VERSION: adopt it first (`pystino adopt`)")
    raise UpgradeError(f"unknown PYSTINO_CONFIG_VERSION {current!r}")


def plan(
    deploy_dir: Path,
    target: str,
    release: dict[str, str],
    *,
    keep_overrides: bool = False,
) -> UpgradePlan:
    values = envfile.read(deploy_dir / ".env")
    if values.get("PYSTINO_SRC"):
        raise UpgradeError("a development install upgrades with `git pull` in its checkouts")
    if release.get("PYSTINO_VERSION") != target:
        raise UpgradeError(
            f"this image carries release {release.get('PYSTINO_VERSION')!r}, not {target!r}; "
            "run the upgrade through ./pystino so it uses the target version's image"
        )
    old_manifest_cerea = values.get("PYSTINO_RELEASE_CEREA_VERSION", "")
    changes: dict[str, str] = {}
    kept: dict[str, str] = {}
    for key in PINNED:
        if key not in release:
            continue
        current = values.get(key)
        # A CEREA_VERSION the operator moved off the old manifest on purpose is
        # kept when asked; otherwise the new release's tested pair wins.
        if (
            keep_overrides
            and key == "CEREA_VERSION"
            and old_manifest_cerea
            and current != old_manifest_cerea
        ):
            kept[key] = current or ""
            continue
        if current != release[key]:
            changes[key] = release[key]
    changes["PYSTINO_RELEASE_CEREA_VERSION"] = release.get("CEREA_VERSION", "")
    changes.update(_migrate_config(values))
    return UpgradePlan(
        old_version=values.get("PYSTINO_VERSION", "unknown"),
        new_version=target,
        changes=changes,
        kept_overrides=kept,
    )


def apply(deploy_dir: Path, upgrade: UpgradePlan, stack_dir: Path | None = None) -> list[str]:
    stack = stack_dir or stackfiles.stack_dir()
    written: list[str] = []
    suffix = f".bak-{upgrade.old_version}"
    for name in ("compose.yaml", ".env"):
        path = deploy_dir / name
        if path.exists():
            backup = path.with_name(path.name + suffix)
            shutil.copy2(path, backup)
            written.append(str(backup))
    shutil.copyfile(stack / "compose.yaml", deploy_dir / "compose.yaml")
    written.append(str(deploy_dir / "compose.yaml"))
    envfile.update(deploy_dir / ".env", upgrade.changes)
    written.append(str(deploy_dir / ".env"))
    return written
