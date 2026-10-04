"""``GET /opencode/install.sh``: the setup script for people who use opencode.

``curl -fsSL https://<gateway>/opencode/install.sh | bash`` is the whole
instruction, so the gateway serves the script itself. Caddy needs no route for
it: both proxies already send everything that is not ``/chat`` or
``/authelia`` here, and the file ships inside the image with the code, so it is
never a different version from the gateway it points at.

The one thing filled in at serve time is the origin the script was fetched
from, so the one-liner needs no address. It comes from the request's own
scheme and ``Host`` header, which a client controls, so it is checked against a
strict host pattern first and left out when it does not pass: an unvalidated
value would be interpolated into a shell script. A client that forges the
header only changes the script it is handed itself, and the script uses the
origin as a default that any argument overrides.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Request, Response

router = APIRouter(tags=["clients"], include_in_schema=False)

_SCRIPT = Path(__file__).resolve().parent.parent / "client_scripts" / "opencode-install.sh"

#: The placeholder line the script carries; exactly one, replaced whole.
_ORIGIN_LINE = "served_origin=''"

# A DNS name or IPv4 address, or a bracketed IPv6 literal, with an optional
# port. Nothing here can close a quote or start a command in shell.
_HOST = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
    r"|\[[0-9A-Fa-f:.]{2,45}\])(?::[0-9]{1,5})?$"
)


def origin_of(request: Request) -> str | None:
    """The validated ``scheme://host[:port]`` of this request, or None."""
    scheme = request.url.scheme
    host = request.headers.get("host", "")
    if scheme not in ("http", "https") or not _HOST.match(host):
        return None
    return f"{scheme}://{host}"


@router.get("/opencode/install.sh")
async def opencode_install_script(request: Request) -> Response:
    body = _SCRIPT.read_text(encoding="utf-8")
    if body.count(_ORIGIN_LINE) != 1:  # a refactor of the script must not go unnoticed
        raise RuntimeError("opencode-install.sh lost its served_origin placeholder")
    origin = origin_of(request)
    if origin is not None:
        body = body.replace(_ORIGIN_LINE, f"served_origin='{origin}'")
    return Response(
        body,
        media_type="text/x-shellscript",
        headers={
            # The script changes with the gateway; never serve a stale copy
            # to someone about to pipe it into a shell.
            "Cache-Control": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )
