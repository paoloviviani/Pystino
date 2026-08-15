"""A minimal OpenAI-compatible upstream, for `scripts/smoke_test.sh`.

Not part of the gateway and not used by the unit suite — the tests use an
in-process transport. This exists so the smoke test can exercise the streaming
path over a real socket.

Two details are deliberate:

* It **does** emit the trailing usage-only frame, so the smoke test can verify
  that accounting captures it and that it is stripped from the client's stream.
* It writes the response in **7-byte slices**, so frame boundaries land mid-JSON
  and inside the blank-line separator. That is the case a parser working on
  network chunks rather than event boundaries gets wrong.
* It **echoes the last user message back** in its reply, and remembers the last
  request at ``GET /_last_request``. Both exist for the redaction check: echoing
  sends any placeholders back through the response path so restoration is
  exercised end to end, and the recorded request is how a test asserts on the
  bytes that actually left the gateway rather than on what it believes it sent.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

MODEL = "upstream/smoke-model"
SLICE_SIZE = 7

# The most recent request body, for GET /_last_request. Single-process, single
# slot, no locking: this is a development fake, not a service.
LAST_REQUEST: dict[str, Any] = {}


def _echo(body: dict[str, Any]) -> str:
    """The last user message, so the reply carries whatever the prompt did.

    With redaction on, the prompt reaching here contains placeholders; echoing
    them means the gateway's restore path has something real to restore.
    """
    for message in reversed(body.get("messages") or []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
    return ""


def chunk(
    content: str | None = None,
    finish_reason: str | None = None,
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "chatcmpl-smoke",
        "object": "chat.completion.chunk",
        "created": 1_700_000_000,
        "model": MODEL,
        "choices": [],
    }
    if content is not None or finish_reason is not None:
        choice: dict[str, Any] = {"index": 0, "delta": {}}
        if content is not None:
            choice["delta"]["content"] = content
        if finish_reason is not None:
            choice["finish_reason"] = finish_reason
        payload["choices"] = [choice]
    if usage is not None:
        payload["usage"] = usage
    return payload


async def chat_completions(request: Request) -> JSONResponse | StreamingResponse:
    body = await request.json()
    # Printed so the smoke test can assert on what the gateway actually sent —
    # above all that stream_options.include_usage was forced.
    print("UPSTREAM RECEIVED:", json.dumps(body, sort_keys=True), flush=True)
    LAST_REQUEST.clear()
    LAST_REQUEST.update(body)
    echoed = _echo(body)

    if not body.get("stream"):
        return JSONResponse(
            {
                "id": "chatcmpl-smoke",
                "object": "chat.completion",
                "created": 1_700_000_000,
                "model": MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": f"buffered hello. You said: {echoed}",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1_000_000,
                    "completion_tokens": 500_000,
                    "total_tokens": 1_500_000,
                },
            }
        )

    async def stream() -> AsyncIterator[bytes]:
        frames = b""
        for piece in ["streamed ", "hello ", "world", ". You said: ", echoed]:
            frames += b"data: " + json.dumps(chunk(piece)).encode() + b"\n\n"
        frames += b"data: " + json.dumps(chunk(finish_reason="stop")).encode() + b"\n\n"
        frames += (
            b"data: "
            + json.dumps(
                chunk(
                    usage={
                        "prompt_tokens": 2_000_000,
                        "completion_tokens": 1_000_000,
                        "total_tokens": 3_000_000,
                    }
                )
            ).encode()
            + b"\n\n"
        )
        frames += b"data: [DONE]\n\n"

        for index in range(0, len(frames), SLICE_SIZE):
            yield frames[index : index + SLICE_SIZE]

    return StreamingResponse(stream(), media_type="text/event-stream")


async def last_request(request: Request) -> JSONResponse:
    """What this fake received most recently.

    The point of the redaction check: it asserts on the bytes that actually left
    the gateway, not on what the gateway believes it sent.
    """
    return JSONResponse(LAST_REQUEST)


async def models(request: Request) -> JSONResponse:
    """A catalogue in the Cortecs shape, for exercising `/api/admin/models/discover`.

    Deliberately mixed: one model the gateway already has, two it does not, and one
    priced in USD so the currency refusal is reachable.
    """

    def entry(
        model_id: str, inp: str, out: str, currency: str = "EUR", ctx: int = 128_000
    ) -> dict[str, Any]:
        return {
            "id": model_id,
            "context_length": ctx,
            "pricing": {"input_token": inp, "output_token": out, "currency": currency},
        }

    return JSONResponse(
        {
            "data": [
                entry(MODEL, "0.15", "0.60", ctx=8192),
                entry("upstream/haiku-ish", "0.08", "0.40", ctx=32_000),
                entry("upstream/big-model", "3.00", "15.00", ctx=200_000),
                entry("upstream/dollar-model", "1.00", "2.00", currency="USD"),
            ]
        }
    )


app = Starlette(
    routes=[
        Route("/v1/chat/completions", chat_completions, methods=["POST"]),
        Route("/v1/models", models, methods=["GET"]),
        Route("/_last_request", last_request, methods=["GET"]),
    ]
)
