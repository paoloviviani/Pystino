"""Serving the chat SPA.

The same arrangement the gateway uses for the console, and the same three
reasons: hashed assets cached for a year, the entry document revalidated every
time, and a fallback so a deep link into a client-side route returns the app
rather than a 404.

Mounted under ``MOUNT_PATH``, and the SPA is built with the matching Vite
``base``. The two have to agree: an app built for the root and served under a
prefix loads its entry document and then requests every script from ``/assets/…``
— which behind the proxy is the *gateway*. The page is blank, both logs are
clean, and the 404s name files that exist.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse
from starlette.staticfiles import StaticFiles

from chat_api.config import Settings
from chat_api.routers import MOUNT_PATH

# Guessed wrong in a slim runtime image, which has no OS mime database — the
# works-here/broken-there shape that only a live check finds.
for _suffix, _type in ((".woff2", "font/woff2"), (".woff", "font/woff")):
    mimetypes.add_type(_type, _suffix)

_IMMUTABLE = "public, max-age=31536000, immutable"
_ENTRY = "no-cache, must-revalidate"

#: `connect-src 'self'` is the load-bearing line: the browser talks only to this
#: service, which then talks to the gateway. A chat UI that called /v1 directly
#: would need the gateway's origin here — and would also need the access token
#: in the browser, which is the arrangement this service exists to avoid.
_CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
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
    "x-frame-options": "DENY",
}


def default_spa_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "web_static"


def resolve_spa_dir(settings: Settings) -> Path | None:
    """The directory to serve, or None when the image was built without assets."""
    if not settings.spa_enabled:
        return None
    directory = default_spa_dir()
    return directory if (directory / "index.html").is_file() else None


class _Assets(StaticFiles):
    def file_response(self, *args: object, **kwargs: object) -> Response:
        response = super().file_response(*args, **kwargs)  # type: ignore[arg-type]
        response.headers["cache-control"] = _IMMUTABLE
        return response


def build_router(directory: Path) -> APIRouter:
    router = APIRouter()
    index = directory / "index.html"

    @router.get(MOUNT_PATH, include_in_schema=False)
    @router.get(f"{MOUNT_PATH}/{{spa_path:path}}", include_in_schema=False)
    async def spa(request: Request, spa_path: str = "") -> Response:
        # A path with a file extension that got this far is a missing asset,
        # not a client-side route. Returning index.html for it would answer a
        # missing script with an HTML page, and the browser's error would name
        # the wrong problem.
        if spa_path.startswith("assets/") or "." in spa_path.rsplit("/", 1)[-1]:
            return Response(status_code=404)
        return FileResponse(
            index, headers={"cache-control": _ENTRY, **_SECURITY_HEADERS}
        )

    return router


def mount(app: object, directory: Path) -> None:
    from fastapi import FastAPI

    assert isinstance(app, FastAPI)
    app.mount(f"{MOUNT_PATH}/assets", _Assets(directory=directory / "assets"), name="assets")
