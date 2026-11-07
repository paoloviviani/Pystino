"""Request body limits and response headers (audit M9, L8)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from conftest import Seeded
from gateway.config import Settings
from gateway.limits import BodyLimitMiddleware, SecurityHeadersMiddleware, body_limit_for


class _Reader:
    """An inner ASGI app that reads the whole body, as FastAPI would, then answers."""

    def __init__(self) -> None:
        self.read = 0
        self.answered = False

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            self.read += len(message.get("body", b""))
            if not message.get("more_body"):
                break
        self.answered = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def _call(
    app: Any, path: str, chunks: list[bytes], headers: list[tuple[bytes, bytes]] | None = None
) -> tuple[int, dict[bytes, bytes], bytes]:
    out: list[dict[str, Any]] = []
    pending = list(chunks)

    async def receive() -> dict[str, Any]:
        body = pending.pop(0) if pending else b""
        return {"type": "http.request", "body": body, "more_body": bool(pending)}

    async def send(message: dict[str, Any]) -> None:
        out.append(message)

    scope = {"type": "http", "path": path, "headers": headers or [], "scheme": "http"}
    await app(scope, receive, send)
    start = next(m for m in out if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in out if m["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), body


def _settings(v1: int = 100, api: int = 10) -> Settings:
    s = Settings(environment="dev")
    s.request_limits.max_v1_body_bytes = v1
    s.request_limits.max_api_body_bytes = api
    return s


async def test_a_declared_length_over_the_cap_is_refused_unread() -> None:
    inner = _Reader()
    app = BodyLimitMiddleware(inner, settings=_settings())
    status, _, body = await _call(
        app, "/v1/chat/completions", [b"x"], [(b"content-length", b"101")]
    )
    assert status == 413
    assert b"request_too_large" in body
    assert inner.read == 0


async def test_a_streamed_body_with_no_length_is_cut_off_at_the_cap() -> None:
    inner = _Reader()
    app = BodyLimitMiddleware(inner, settings=_settings())
    status, _, _ = await _call(app, "/v1/chat/completions", [b"x" * 60, b"x" * 60, b"x" * 60])
    assert status == 413
    assert not inner.answered
    assert inner.read <= 120


async def test_a_lying_content_length_does_not_help() -> None:
    inner = _Reader()
    app = BodyLimitMiddleware(inner, settings=_settings())
    status, _, _ = await _call(
        app, "/v1/chat/completions", [b"x" * 150], [(b"content-length", b"5")]
    )
    assert status == 413


async def test_a_body_within_the_cap_passes() -> None:
    inner = _Reader()
    app = BodyLimitMiddleware(inner, settings=_settings())
    status, _, _ = await _call(app, "/v1/chat/completions", [b"x" * 100])
    assert status == 200
    assert inner.read == 100


async def test_management_routes_have_the_smaller_cap() -> None:
    app = BodyLimitMiddleware(_Reader(), settings=_settings())
    assert (await _call(app, "/api/admin/groups", [b"x" * 11]))[0] == 413
    assert (await _call(app, "/api/admin/groups", [b"x" * 10]))[0] == 200
    assert (await _call(app, "/health", [b"x" * 1000]))[0] == 200


def test_transcription_keeps_its_own_cap() -> None:
    s = _settings()
    assert body_limit_for("/v1/audio/transcriptions", s) is None
    assert body_limit_for("/v1/chat/completions", s) == 100
    assert body_limit_for("/console/x", s) is None


async def test_no_store_and_hsts() -> None:
    app = SecurityHeadersMiddleware(_Reader())
    _, headers, _ = await _call(app, "/api/me/keys", [])
    assert headers[b"cache-control"] == b"no-store"
    assert b"strict-transport-security" not in headers
    _, headers, _ = await _call(app, "/v1/models", [], [(b"x-forwarded-proto", b"https")])
    assert b"strict-transport-security" in headers
    assert b"cache-control" not in headers


async def test_the_real_app_refuses_an_oversized_body_before_auth(
    client: httpx.AsyncClient, settings: Settings
) -> None:
    settings.request_limits.max_v1_body_bytes = 1000
    # No credentials at all: the size is refused before auth is even asked.
    response = await client.post("/v1/chat/completions", content=b"x" * 2000)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    # A small bad-key request still reaches authentication.
    response = await client.post("/v1/chat/completions", content=b"{}")
    assert response.status_code in (400, 401)


async def test_a_streamed_oversize_body_is_refused_by_the_real_app(
    client: httpx.AsyncClient, settings: Settings, seeded: Seeded
) -> None:
    settings.request_limits.max_v1_body_bytes = 1000

    async def chunks() -> Any:
        for _ in range(5):
            yield b"x" * 500

    response = await client.post(
        "/v1/chat/completions",
        content=chunks(),
        headers={"authorization": "Bearer nope"},
    )
    assert response.status_code == 413


@pytest.mark.parametrize("path", ["/api/me/keys", "/v1/me"])
async def test_management_responses_are_no_store(client: httpx.AsyncClient, path: str) -> None:
    response = await client.get(path)
    assert response.headers["cache-control"] == "no-store"


def test_an_unusable_request_id_is_replaced_not_refused() -> None:
    from gateway.routers._metered import request_id_for

    class _Req:
        def __init__(self, value: str | None) -> None:
            self.headers = {} if value is None else {"x-request-id": value}

    s = Settings(environment="dev")
    assert request_id_for(_Req("abc-123"), s) == "abc-123"  # type: ignore[arg-type]
    for bad in ("x" * 65, "a\nb", "é", None):
        out = request_id_for(_Req(bad), s)  # type: ignore[arg-type]
        assert len(out) == 32 and out != bad
