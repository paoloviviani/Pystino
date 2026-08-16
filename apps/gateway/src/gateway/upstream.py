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
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import orjson

from gateway.config import UpstreamSettings

#: Anthropic requires an API version on every request and rejects one without
#: it. Harmless on providers that proxy the shape and ignore the header, so it
#: is sent unconditionally rather than made configurable; a provider's own
#: `extra_headers` still wins, since those are applied after.
ANTHROPIC_HEADERS = {"anthropic-version": "2023-06-01"}

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
    """Talks to a provider endpoint.

    Named for the shape most of it speaks; it also carries the Anthropic
    Messages route, which the reference provider serves from the same base URL
    with the same credentials (ADR 0030). Splitting it in two would mean two
    connection pools to the same host.
    """

    def __init__(self, settings: UpstreamSettings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    def _headers(self, *, request_id: str | None = None) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "accept": "application/json",
        }
        if api_key := self._settings.api_key.get_secret_value():
            if self._settings.auth_scheme == "x_api_key":
                # Anthropic's own API authenticates this way and rejects a
                # bearer token. The reference provider uses bearer for every
                # route including /v1/messages, so this is per provider rather
                # than per route (ADR 0030).
                headers["x-api-key"] = api_key
            else:
                headers["authorization"] = f"Bearer {api_key}"
        headers.update(self._settings.extra_headers)
        if request_id:
            # Helps correlate our ledger with a provider's own logs when
            # reconciling a disputed bill.
            headers["x-request-id"] = request_id
        return headers

    async def post_json(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        request_id: str | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> UpstreamResponse:
        """POST a JSON body to one of the provider's routes and parse the reply.

        Every non-streaming route goes through here, so they cannot drift on
        auth, timeouts, or what a non-JSON error body does (it becomes a null
        payload with the raw bytes kept, rather than an exception that would
        lose the provider's own diagnostics).
        """
        headers = self._headers(request_id=request_id)
        if extra_headers:
            headers.update(extra_headers)
        try:
            response = await self._client.post(
                f"{self._settings.base_url}{path}",
                content=orjson.dumps(payload),
                headers=headers,
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

    async def embeddings(
        self, payload: Mapping[str, Any], *, request_id: str | None = None
    ) -> UpstreamResponse:
        """Create embeddings. Never streams — the response is one JSON body."""
        return await self.post_json("/embeddings", payload, request_id=request_id)

    async def images(
        self, payload: Mapping[str, Any], *, request_id: str | None = None
    ) -> UpstreamResponse:
        """Generate images. Never streams here, whatever the provider offers.

        Partial-image streaming exists upstream and is not forwarded: a partial
        image is not billable output, and the route would gain a streaming
        pipeline for no accounting benefit.
        """
        return await self.post_json("/images/generations", payload, request_id=request_id)

    async def list_models(self) -> UpstreamResponse:
        """Ask the provider what it offers.

        Used by catalogue discovery and by the provider connection test. Kept on
        the client rather than written inline at both call sites so the auth
        header, the base URL and the error shape are the same ones a real
        completion would use — a test that builds its own request can pass while
        the thing it is testing does not work.
        """
        try:
            response = await self._client.get(
                f"{self._settings.base_url}/models", headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(f"upstream request failed: {exc}", cause=exc) from exc

        raw = response.content
        try:
            candidate = orjson.loads(raw) if raw else None
            parsed = candidate if isinstance(candidate, dict) else None
        except orjson.JSONDecodeError:
            parsed = None
        return UpstreamResponse(status_code=response.status_code, payload=parsed, raw=raw)

    async def chat_completion(
        self, payload: Mapping[str, Any], *, request_id: str | None = None
    ) -> UpstreamResponse:
        """Non-streaming completion."""
        return await self.post_json("/chat/completions", payload, request_id=request_id)

    async def responses(
        self, payload: Mapping[str, Any], *, request_id: str | None = None
    ) -> UpstreamResponse:
        """Non-streaming Responses call."""
        return await self.post_json("/responses", payload, request_id=request_id)

    async def messages(
        self, payload: Mapping[str, Any], *, request_id: str | None = None
    ) -> UpstreamResponse:
        """Non-streaming Anthropic Messages call."""
        return await self.post_json(
            "/messages", payload, request_id=request_id, extra_headers=ANTHROPIC_HEADERS
        )

    @asynccontextmanager
    async def stream(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        request_id: str | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> AsyncIterator[httpx.Response]:
        """Open a streaming POST against one of the provider's routes.

        A context manager so that the upstream connection is closed on every exit
        path — including the one that matters, where the *client* disconnects and
        the generator consuming this is thrown a CancelledError. Without that, an
        abandoned client leaks a provider connection and keeps paying for tokens
        nobody will read.
        """
        headers = self._headers(request_id=request_id)
        headers["accept"] = "text/event-stream"
        if extra_headers:
            headers.update(extra_headers)
        request = self._client.build_request(
            "POST",
            f"{self._settings.base_url}{path}",
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

    def stream_chat_completion(
        self, payload: Mapping[str, Any], *, request_id: str | None = None
    ) -> AbstractAsyncContextManager[httpx.Response]:
        return self.stream("/chat/completions", payload, request_id=request_id)

    def stream_responses(
        self, payload: Mapping[str, Any], *, request_id: str | None = None
    ) -> AbstractAsyncContextManager[httpx.Response]:
        return self.stream("/responses", payload, request_id=request_id)

    def stream_messages(
        self, payload: Mapping[str, Any], *, request_id: str | None = None
    ) -> AbstractAsyncContextManager[httpx.Response]:
        return self.stream(
            "/messages", payload, request_id=request_id, extra_headers=ANTHROPIC_HEADERS
        )
