"""Where the release's stack files are: in the image, or in a checkout."""

from __future__ import annotations

import os
from pathlib import Path

from gateway.deploy import envfile

#: Baked into the gateway image by its Dockerfile (`COPY deploy/stack …`).
IMAGE_STACK_DIR = Path("/app/deploy/stack")


def stack_dir() -> Path:
    """The directory holding compose.yaml and release.env for *this* version.

    ``PYSTINO_STACK_DIR`` wins (tests, unusual layouts); then the copy baked into
    the image; then the checkout this module was imported from, which is the
    development case (`uv run pystino …`).
    """
    override = os.environ.get("PYSTINO_STACK_DIR")
    if override:
        return Path(override)
    if (IMAGE_STACK_DIR / "compose.yaml").is_file():
        return IMAGE_STACK_DIR
    # src/gateway/deploy/stackfiles.py → repository root is five levels up.
    checkout = Path(__file__).resolve().parents[5] / "deploy" / "stack"
    if (checkout / "compose.yaml").is_file():
        return checkout
    raise FileNotFoundError(
        "cannot find the stack files (compose.yaml, release.env); set PYSTINO_STACK_DIR"
    )


def release() -> dict[str, str]:
    """The compatibility manifest: pinned versions and upstream images."""
    return envfile.read(stack_dir() / "release.env")


def source_checkout() -> Path | None:
    """The Pystino checkout this code runs from, if any (development mode)."""
    root = Path(__file__).resolve().parents[5]
    return root if (root / "deploy" / "stack" / "compose.yaml").is_file() else None
