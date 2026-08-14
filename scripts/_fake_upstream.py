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
                        "message": {"role": "assistant", "content": "buffered hello"},
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
        for piece in ["streamed ", "hello ", "world"]:
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


app = Starlette(routes=[Route("/v1/chat/completions", chat_completions, methods=["POST"])])
