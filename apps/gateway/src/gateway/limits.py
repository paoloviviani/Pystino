"""Request-size limits and response headers, as plain ASGI middleware.

Both run outside routing and outside authentication, which is the point of the
size limit: FastAPI reads and parses a JSON body *before* a failing auth
dependency runs, so without this a caller with no valid key can make a worker
buffer as many bytes as they care to send (audit 2026-11, M9).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

import orjson

from gateway.config import Settings
from gateway.errors import error_payload

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def body_limit_for(path: str, settings: Settings) -> int | None:
    """The most bytes a request to *path* may carry; ``None`` for no limit."""
    limits = settings.request_limits
    if path.startswith(("/v1/audio/transcriptions", "/v1/audio/translations")):
        # The route counts the bytes itself, against its own exact cap, before
        # parsing and before any auth-dependent work, and answers with its own
        # error code. A second, looser cap here would only change that answer.
        return None
    if path.startswith("/v1/"):
        return limits.max_v1_body_bytes
    if path.startswith(("/api/", "/auth/", "/scim/")):
        return limits.max_api_body_bytes
    return None


class BodyLimitMiddleware:
    """Refuse an oversized request with 413 before anything parses or authenticates it.

    A declared ``Content-Length`` over the cap is refused without reading a
    byte. A body with no length (chunked), or one that lies, is counted as it
    streams and cut off at the cap.
    """

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = body_limit_for(scope.get("path", ""), self.settings)
        if limit is None:
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name == b"content-length":
                if value.isdigit() and int(value) > limit:
                    await self._refuse(send, limit)
                    return
                break

        seen = 0
        refused = False

        async def counted_receive() -> Message:
            nonlocal seen, refused
            if refused:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    # Answered here, not by raising: FastAPI turns any exception
                    # from a body read into its own 400. The application is told
                    # the client went away, and whatever it then tries to send is
                    # dropped.
                    refused = True
                    await self._refuse(send, limit)
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            if not refused:
                await send(message)

        await self.app(scope, counted_receive, guarded_send)

    @staticmethod
    async def _refuse(send: Send, limit: int) -> None:
        body = orjson.dumps(
            error_payload(
                f"The request body is larger than this endpoint accepts ({limit} bytes).",
                code="request_too_large",
            )
        )
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    # The client may still be sending; do not keep reading it.
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class SecurityHeadersMiddleware:
    """``no-store`` on management responses, and HSTS over HTTPS.

    ``/api`` and ``/v1/me`` responses carry one-time passwords, freshly minted
    API keys and personal data, none of which belongs in a shared or browser
    cache. A route that sets its own ``Cache-Control`` keeps it. HSTS is sent
    only when the request arrived over TLS (directly, or via the proxy's
    ``X-Forwarded-Proto``), so a plain-HTTP development stack is unaffected.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        no_store = path.startswith(("/api/", "/auth/", "/v1/me", "/scim/"))
        headers = dict(scope.get("headers", []))
        https = (
            scope.get("scheme") == "https"
            or headers.get(b"x-forwarded-proto", b"").split(b",")[0].strip() == b"https"
        )

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                out = list(message.get("headers", []))
                present = {name.lower() for name, _ in out}
                if no_store and b"cache-control" not in present:
                    out.append((b"cache-control", b"no-store"))
                if https and b"strict-transport-security" not in present:
                    out.append((b"strict-transport-security", b"max-age=31536000"))
                message = {**message, "headers": out}
            await send(message)

        await self.app(scope, receive, send_with_headers)
