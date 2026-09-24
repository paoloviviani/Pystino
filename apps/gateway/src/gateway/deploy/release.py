"""`pystino release-pin`: pin the release manifest's images by digest (ADR 0087).

A tag can move; a digest cannot. The release manifest names the Cerea image
and every upstream image (Postgres, Valkey, Mongo, Playwright) the release was
tested with, so each is rewritten to `name:tag@sha256:…` — the tag stays for
humans, the digest is what Docker resolves.

Pystino's own images are the exception, and deliberately: the gateway image
carries this manifest, so it cannot contain its own digest. They are pinned by
version tag, and the image workflow refuses to overwrite a released tag.

Run by whoever cuts a release (it needs registry access: `docker login
ghcr.io` for the private images), and by CI on tags to check nothing is left
unpinned.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from gateway.deploy import envfile

#: Manifest keys that name an image reference, and how to build it when absent.
PINNED_KEYS = ("CEREA_IMAGE", "POSTGRES_IMAGE", "VALKEY_IMAGE", "MONGO_IMAGE", "PLAYWRIGHT_IMAGE")
_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")


def resolve_digest(reference: str) -> str:
    """The registry's digest for `reference` (a multi-arch index digest when there is one)."""
    out = subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["docker", "buildx", "imagetools", "inspect", reference, "--format", "{{json .Manifest}}"],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout
    digest = str(json.loads(out).get("digest", ""))
    if not digest.startswith("sha256:"):
        raise ValueError(f"no digest for {reference}")
    return digest


def strip_digest(reference: str) -> str:
    return _DIGEST.sub("", reference)


def pin(
    manifest: dict[str, str], resolver: Callable[[str], str] = resolve_digest
) -> dict[str, str]:
    """The changes that pin every image reference in `manifest` by digest."""
    changes: dict[str, str] = {}
    references = dict(manifest)
    if "CEREA_IMAGE" not in references and references.get("CEREA_VERSION"):
        registry = references.get("CEREA_REGISTRY", references.get("PYSTINO_REGISTRY", ""))
        references["CEREA_IMAGE"] = f"{registry}/cerea:{references['CEREA_VERSION']}"
    for key in PINNED_KEYS:
        reference = references.get(key)
        if not reference:
            continue
        base = strip_digest(reference)
        pinned = f"{base}@{resolver(base)}"
        if pinned != manifest.get(key):
            changes[key] = pinned
    return changes


def unpinned(manifest: dict[str, str]) -> list[str]:
    return [key for key in PINNED_KEYS if manifest.get(key) and not _DIGEST.search(manifest[key])]


def pin_file(path: Path, resolver: Callable[[str], str] = resolve_digest) -> dict[str, str]:
    changes = pin(envfile.read(path), resolver)
    if changes:
        envfile.update(path, changes)
    return changes
