"""Talking to the gateway.

Everything this service knows about the gateway is in this file, and it is
deliberately very little: a base URL, and the fact that ``/v1`` is
OpenAI-shaped. No shared types, no imported client — the boundary is HTTP, and
keeping the knowledge in one module is what makes that checkable.

The one rule that matters: **every call carries the access token of the person
whose request it is.** There is no service credential to fall back to, so a bug
that loses the token produces a 401 rather than a request nobody is billed for.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class GatewayError(Exception):
    """The gateway refused or could not be reached."""

    def __init__(self, message: str, *, status: int = 502, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True, slots=True)
class Model:
    id: str
    owned_by: str | None = None


class GatewayClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str) -> None:
        self._http = http
        self._base = base_url.rstrip("/")

    def _headers(self, access_token: str) -> dict[str, str]:
        return {"authorization": f"Bearer {access_token}"}

    async def models(self, access_token: str) -> list[Model]:
        """What this person may use — the gateway has already filtered it.

        No caching. The list is per-caller and changes when an administrator
        changes access; a cache here would show someone a model they cannot use
        and call it a gateway error when they tried.
        """
        try:
            response = await self._http.get(
                f"{self._base}/v1/models", headers=self._headers(access_token)
            )
        except httpx.HTTPError as exc:
            raise GatewayError(f"gateway unreachable: {exc}") from exc
        if response.status_code != 200:
            raise self._error_from(response)
        payload = response.json()
        return [
            Model(id=item["id"], owned_by=item.get("owned_by"))
            for item in payload.get("data", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]

    async def stream_chat(
        self, *, access_token: str, body: dict[str, Any]
    ) -> AsyncIterator[tuple[str, str | None]]:
        """Relay a streamed completion, yielding ``(raw_sse_line, request_id)``.

        Streamed rather than buffered even though buffering would be simpler,
        because the gateway's own comment about `flush_interval` applies one
        layer up: a buffered stream produces no error, and all the bytes are
        correct and all of them are late.

        `stream_options.include_usage` is requested so the terminal frame
        carries token counts. The gateway meters regardless — it does not trust
        us — but a client that never sees usage cannot show it.
        """
        payload = dict(body, stream=True)
        payload.setdefault("stream_options", {"include_usage": True})
        try:
            async with self._http.stream(
                "POST",
                f"{self._base}/v1/chat/completions",
                headers=self._headers(access_token),
                json=payload,
            ) as response:
                request_id = response.headers.get("x-request-id")
                if response.status_code != 200:
                    await response.aread()
                    raise self._error_from(response)
                async for line in response.aiter_lines():
                    yield line, request_id
        except httpx.HTTPError as exc:
            raise GatewayError(f"gateway unreachable: {exc}") from exc

    @staticmethod
    def _error_from(response: httpx.Response) -> GatewayError:
        """Carry the gateway's own words through.

        Rewriting them is how "you have exceeded your monthly budget" becomes
        "something went wrong" on the screen the user actually reads.
        """
        message = f"gateway returned HTTP {response.status_code}"
        code = None
        try:
            body = response.json()
            error = body.get("error") if isinstance(body, dict) else None
            if isinstance(error, dict):
                message = str(error.get("message") or message)
                code = error.get("code")
        except ValueError:
            pass
        return GatewayError(message, status=response.status_code, code=code)
