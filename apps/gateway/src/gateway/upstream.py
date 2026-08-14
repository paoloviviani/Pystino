"""The upstream provider client.

One configurable OpenAI-compatible endpoint in Phase 1, behind a small protocol
so that multi-provider routing (and a different HTTP library, should httpx's
release cadence stay as slow as it has been) is a contained change.

The timeout configuration is the part that bites people. httpx's default read
timeout is five seconds, and it applies to the gap between *reads on a stream*,
not to the request as a whole. A model that thinks for six seconds before its
first token, or that pauses mid-generation, therefore has its stream killed by
the client library — intermittently, under load, in a way that looks like a
provider fault. Read must be ``None``.

Connect, write and pool timeouts stay finite: without them an unreachable
provider would hang a request forever instead of failing fast.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import orjson

from gateway.config import UpstreamSettings

logger = logging.getLogger(__name__)


class UpstreamError(Exception):
    """Transport-level failure reaching the provider (not an HTTP error status)."""

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.cause = cause


@dataclass(slots=True)
class UpstreamResponse:
    """A complete, non-streamed upstream response."""

    status_code: int
    payload: dict[str, Any] | None
    raw: bytes

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class UpstreamStream(Protocol):
    """A live streaming response."""

    status_code: int

    def aiter_bytes(self) -> AsyncIterator[bytes]: ...

    async def aread(self) -> bytes: ...


def build_http_client(settings: UpstreamSettings) -> httpx.AsyncClient:
    """Construct the shared client.

    One client for the process, so connections are pooled and TLS handshakes are
    not repeated per request.
    """
    timeout = httpx.Timeout(
        connect=settings.connect_timeout_seconds,
        # Explicitly unbounded: streams legitimately idle between tokens.
        read=None,
        write=settings.write_timeout_seconds,
        pool=settings.pool_timeout_seconds,
    )
    limits = httpx.Limits(
        max_connections=settings.max_connections,
        max_keepalive_connections=settings.max_keepalive_connections,
    )
    return httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=False)


class OpenAICompatibleUpstream:
    """Talks to any endpoint implementing the OpenAI chat completions API."""

    def __init__(self, settings: UpstreamSettings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    def _headers(self, *, request_id: str | None = None) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "accept": "application/json",
        }
        if api_key := self._settings.api_key.get_secret_value():
            headers["authorization"] = f"Bearer {api_key}"
        headers.update(self._settings.extra_headers)
        if request_id:
            # Helps correlate our ledger with a provider's own logs when
            # reconciling a disputed bill.
            headers["x-request-id"] = request_id
        return headers

    @property
    def _chat_url(self) -> str:
        return f"{self._settings.base_url}/chat/completions"

    async def chat_completion(
        self, payload: Mapping[str, Any], *, request_id: str | None = None
    ) -> UpstreamResponse:
        """Non-streaming completion."""
        try:
            response = await self._client.post(
                self._chat_url,
                content=orjson.dumps(payload),
                headers=self._headers(request_id=request_id),
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(f"upstream request failed: {exc}", cause=exc) from exc

        raw = response.content
        parsed: dict[str, Any] | None
        try:
            candidate = orjson.loads(raw) if raw else None
            parsed = candidate if isinstance(candidate, dict) else None
        except orjson.JSONDecodeError:
            parsed = None
        return UpstreamResponse(status_code=response.status_code, payload=parsed, raw=raw)

    @asynccontextmanager
    async def stream_chat_completion(
        self, payload: Mapping[str, Any], *, request_id: str | None = None
    ) -> AsyncIterator[httpx.Response]:
        """Streaming completion.

        A context manager so that the upstream connection is closed on every exit
        path — including the one that matters, where the *client* disconnects and
        the generator consuming this is thrown a CancelledError. Without that, an
        abandoned client leaks a provider connection and keeps paying for tokens
        nobody will read.
        """
        headers = self._headers(request_id=request_id)
        headers["accept"] = "text/event-stream"
        request = self._client.build_request(
            "POST",
            self._chat_url,
            content=orjson.dumps(payload),
            headers=headers,
        )
        try:
            response = await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise UpstreamError(f"upstream stream failed to start: {exc}", cause=exc) from exc

        try:
            yield response
        finally:
            await response.aclose()
