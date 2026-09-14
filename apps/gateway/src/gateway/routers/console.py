"""Serving the console SPA.

The gateway did not serve HTML before this, and the three things that come with
doing so are all easy to leave out and hard to notice missing (ADR 0023):

* **A Content Security Policy.** The console is same-origin with the API and
  carries the session cookie, so an injected script here is an injected script
  with the operator's authority. The policy below is restrictive enough that the
  build had to be adjusted to fit it — Vite's inline module-preload polyfill is
  switched off in `vite.config.ts` — rather than the policy loosened to fit the
  build. That direction is the whole point.
* **Cache headers.** Hashed assets are immutable and cached for a year;
  ``index.html`` must never be, or a browser holding yesterday's copy asks for
  asset URLs that no longer exist and the console breaks on deploy for exactly
  the people who use it most.
* **A SPA fallback.** ``/console/anything`` has to return ``index.html`` so the
  client-side router can handle it, while still 404ing a missing *asset* —
  returning HTML for a missing ``.js`` turns a deploy mistake into a parse error
  in the browser console with no clue where it came from.

Mounting is conditional on the assets existing **and** the setting allowing it.
An image built without the console cannot half-serve one.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import FileResponse
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from gateway.config import Settings

logger = logging.getLogger(__name__)

MOUNT_PATH = "/console"

# Content types Starlette would otherwise guess wrong, registered explicitly.
#
# `StaticFiles` asks `mimetypes.guess_type`, which reads the *operating system's*
# mime database when there is one. There is one on a developer's machine and there
# is not one in the slim runtime image, so the self-hosted woff2 was served as
# `font/woff2` locally and `application/octet-stream` in the container — the works
# here / broken there shape that only a live check finds. Browsers happen to load
# a font regardless of its content type, so the symptom would have been nothing
# more than a wrong header, indefinitely.
for _suffix, _type in (
    (".woff2", "font/woff2"),
    (".woff", "font/woff"),
):
    mimetypes.add_type(_type, _suffix)

# Immutable assets: the filename contains a content hash, so a changed file is a
# changed URL and a year-long cache can never serve a stale one.
_IMMUTABLE = "public, max-age=31536000, immutable"
# The entry document, which is *not* hashed. `no-cache` means revalidate every
# time, not "do not store" — the browser keeps its copy and asks whether it is
# still good, which is one cheap 304 rather than a full download per navigation.
_ENTRY = "no-cache, must-revalidate"

# Content Security Policy.
#
# `script-src 'self'` with no `'unsafe-inline'` and no `'unsafe-eval'`: every
# script is a file served from this origin. `connect-src 'self'` because the
# console talks only to this gateway. `frame-ancestors 'none'` prevents the
# console being framed by another site, which is the clickjacking defence that
# matters for a page with administrative buttons on it.
#
# `style-src` needs `'unsafe-inline'`: React writes inline `style` attributes for
# anything computed at runtime, and nothing here has a nonce to offer them. That
# is a genuine, if small, loosening — inline *styles* cannot execute, so the
# exposure is defacement rather than script execution.
_CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "object-src 'none'",
    ]
)

_SECURITY_HEADERS = {
    "content-security-policy": _CSP,
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
    # Belt and braces with frame-ancestors, for anything that still reads this.
    "x-frame-options": "DENY",
}


def default_console_dir() -> Path:
    """Where the Dockerfile puts the built assets."""
    return Path(__file__).resolve().parent.parent / "console_static"


def resolve_console_dir(settings: Settings) -> Path | None:
    """The directory to serve, or None if there is nothing to serve.

    Returns None rather than raising when the assets are absent: that is the
    normal state of the headless image, not an error.
    """
    directory = Path(settings.console_dir) if settings.console_dir else default_console_dir()
    if not (directory / "index.html").is_file():
        return None
    return directory


class ConsoleStatics(StaticFiles):
    """The build's ``assets/`` directory, cached hard.

    Everything under this mount is content-hashed by Vite, so ``immutable`` is
    correct for all of it — a changed file is a changed URL. The entry document
    is *not* served here precisely because it is not hashed; it has its own route
    with revalidation headers.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["cache-control"] = _IMMUTABLE
        for header, value in _SECURITY_HEADERS.items():
            response.headers[header] = value
        return response


def build_router(directory: Path) -> APIRouter:
    """Routes for the entry document and the client-side routes beneath it."""
    router = APIRouter(include_in_schema=False)
    index = directory / "index.html"

    def entry() -> FileResponse:
        return FileResponse(
            index,
            media_type="text/html",
            headers={"cache-control": _ENTRY, **_SECURITY_HEADERS},
        )

    @router.get(MOUNT_PATH)
    async def console_root() -> FileResponse:
        return entry()

    @router.get(f"{MOUNT_PATH}/{{spa_path:path}}")
    async def console_spa(spa_path: str, request: Request) -> Response:
        """Any client-side route returns the entry document.

        Except assets: a request for a missing ``.js`` or ``.css`` must 404
        rather than receive HTML, or the browser reports a syntax error in a
        file that does not exist and the real cause — a bad deploy — is invisible.
        """
        del request
        if spa_path.startswith("assets/") or "." in spa_path.rsplit("/", 1)[-1]:
            return Response(status_code=404)
        return entry()

    return router


def mount_console(app: FastAPI, settings: Settings) -> bool:
    """Attach the console if it is both present and enabled.

    Returns True if it was mounted. Logs the reason either way: "why is there no
    console on this deployment" should be answerable from the startup log rather
    than by inspecting the image.
    """
    if not settings.console_enabled:
        logger.info("console disabled by configuration (GATEWAY_CONSOLE_ENABLED=false)")
        return False

    directory = resolve_console_dir(settings)
    if directory is None:
        logger.info(
            "no console assets in this image; serving the API only. "
            "Build with --build-arg INCLUDE_CONSOLE=true to include it."
        )
        return False

    # Order matters, and the wrong way round fails in a way that looks like a
    # build problem: Starlette matches routes in the order they were added, and
    # the SPA fallback below is a catch-all under the same prefix. Registered
    # first, it swallows every asset request and the page loads with no scripts.
    # So the asset mount goes on first, and the fallback only sees what it misses.
    app.mount(
        f"{MOUNT_PATH}/assets",
        ConsoleStatics(directory=directory / "assets"),
        name="console-assets",
    )
    app.include_router(build_router(directory))
    # What is served, not just where from: the entry document's mtime and the
    # content-hashed asset names identify the build exactly, so a stale deploy
    # — yesterday's bundle served by today's image, or vice versa — is visible
    # in the startup log rather than debuggable from symptoms.
    try:
        entry_mtime = (directory / "index.html").stat().st_mtime
        assets = sorted(path.name for path in (directory / "assets").iterdir())
    except OSError:
        entry_mtime, assets = 0.0, []
    logger.info(
        "console mounted at %s from %s (index.html mtime %d, assets %s)",
        MOUNT_PATH,
        directory,
        int(entry_mtime),
        ",".join(assets),
    )
    return True
