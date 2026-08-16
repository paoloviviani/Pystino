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


async def embeddings(request: Request) -> JSONResponse:
    """Embeddings, in the Cortecs response shape.

    Reports `provider` and `model` like the real one, so the gateway's
    record-what-served-it path is exercised rather than assumed.
    """
    body = await request.json()
    LAST_REQUEST.clear()
    LAST_REQUEST.update(body)

    raw = body.get("input")
    inputs = [raw] if isinstance(raw, str) else list(raw or [])
    return JSONResponse(
        {
            "id": "emb-smoke",
            "object": "list",
            "created": 1_700_000_000,
            "provider": "fake-provider",
            "model": body.get("model", "upstream/embed-model"),
            "data": [
                {"index": index, "object": "embedding", "embedding": [0.1, 0.2, 0.3]}
                for index, _ in enumerate(inputs)
            ],
            # Cortecs documents completion_tokens as always 0 for embeddings.
            "usage": {
                "prompt_tokens": 1000 * max(1, len(inputs)),
                "completion_tokens": 0,
                "total_tokens": 1000 * max(1, len(inputs)),
            },
        }
    )


async def responses(request: Request) -> JSONResponse | StreamingResponse:
    """The Responses API, in the Cortecs shape.

    Usage is flat — `input_tokens`/`output_tokens`/`total_tokens` with no
    nested detail objects — because that is what the reference provider
    returns, and a fake that reports the richer OpenAI shape would let a bug in
    the flat path through.
    """
    body = await request.json()
    LAST_REQUEST.clear()
    LAST_REQUEST.update(body)

    raw = body.get("input")
    echoed = raw if isinstance(raw, str) else _echo({"messages": raw or []})
    text = f"echo: {echoed}"

    def envelope(status: str = "completed") -> dict[str, Any]:
        return {
            "id": "resp-smoke",
            "object": "response",
            "created_at": 1_700_000_000,
            "status": status,
            "provider": "fake-provider",
            "model": body.get("model", MODEL),
            "output": [
                {
                    "id": "msg-1",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                }
            ],
            "output_text": text,
            "usage": {
                "input_tokens": 1_000_000,
                "output_tokens": 500_000,
                "total_tokens": 1_500_000,
            },
        }

    if not body.get("stream"):
        return JSONResponse(envelope())

    async def stream() -> AsyncIterator[bytes]:
        events: list[dict[str, Any]] = [
            {"type": "response.created", "response": {**envelope("in_progress"), "usage": None}},
            {"type": "response.output_text.delta", "delta": text, "item_id": "msg-1"},
            {"type": "response.output_text.done", "text": text, "item_id": "msg-1"},
            {"type": "response.completed", "response": envelope()},
        ]
        frames = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
        for index in range(0, len(frames), SLICE_SIZE):
            yield frames[index : index + SLICE_SIZE]

    return StreamingResponse(stream(), media_type="text/event-stream")


async def messages(request: Request) -> JSONResponse | StreamingResponse:
    """Anthropic Messages, with usage split across two frames when streaming.

    The split is the whole point of having this fake: `message_start` carries
    the input count and `message_delta` the output count, and a gateway that
    let the last frame win would record a prompt of zero.

    `input_tokens` here is the *uncached remainder*, matching Anthropic — the
    prompt is that plus the two cache figures.
    """
    body = await request.json()
    LAST_REQUEST.clear()
    LAST_REQUEST.update(body)

    text = f"echo: {_echo(body)}"
    model = body.get("model", MODEL)
    usage_in = {
        "input_tokens": 600_000,
        "cache_creation_input_tokens": 100_000,
        "cache_read_input_tokens": 300_000,
    }

    if not body.get("stream"):
        return JSONResponse(
            {
                "id": "msg-smoke",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {**usage_in, "output_tokens": 500_000},
            }
        )

    async def stream() -> AsyncIterator[bytes]:
        events: list[tuple[str, dict[str, Any]]] = [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg-smoke",
                        "type": "message",
                        "role": "assistant",
                        "model": model,
                        "content": [],
                        "stop_reason": None,
                        "usage": {**usage_in, "output_tokens": 1},
                    },
                },
            ),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    # Only the output count, as Anthropic sends it.
                    "usage": {"output_tokens": 500_000},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
        frames = "".join(
            f"event: {name}\ndata: {json.dumps(event)}\n\n" for name, event in events
        ).encode()
        for index in range(0, len(frames), SLICE_SIZE):
            yield frames[index : index + SLICE_SIZE]

    return StreamingResponse(stream(), media_type="text/event-stream")


async def images(request: Request) -> JSONResponse:
    """Image generation.

    Returns `n` images and *no* usage object, which is the per-image-priced
    case — the one where the gateway has to bill from the picture count alone.
    """
    body = await request.json()
    LAST_REQUEST.clear()
    LAST_REQUEST.update(body)

    count = int(body.get("n") or 1)
    return JSONResponse(
        {
            "created": 1_700_000_000,
            "size": body.get("size") or "1024x1024",
            "data": [
                {
                    # A one-pixel PNG: small enough to log, real enough to decode.
                    "b64_json": (
                        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
                        "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
                    ),
                    "revised_prompt": f"a picture of {body.get('prompt', '')}",
                }
                for _ in range(count)
            ],
        }
    )


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
        model_id: str,
        inp: str,
        out: str,
        currency: str = "EUR",
        ctx: int = 128_000,
        modalities: tuple[str, ...] = ("text",),
        accepts: tuple[str, ...] = ("text",),
        features: tuple[str, ...] = ("tools", "json_mode"),
    ) -> dict[str, Any]:
        return {
            "id": model_id,
            # `context_size`, which is the key the real catalogue uses. Spelled
            # the same way here on purpose: the importer used to look only for
            # `context_length` and silently imported every model with no
            # context window at all (ADR 0031).
            "context_size": ctx,
            "pricing": {"input_token": inp, "output_token": out, "currency": currency},
            # Cortecs derives these from model tags; the importer reads
            # `output_modalities` to tell an embedding model from a chat one
            # (ADR 0028) and carries all three through as capabilities.
            "input_modalities": list(accepts),
            "output_modalities": list(modalities),
            "supported_features": list(features),
        }

    return JSONResponse(
        {
            "data": [
                entry(MODEL, "0.15", "0.60", ctx=8192),
                entry("upstream/haiku-ish", "0.08", "0.40", ctx=32_000),
                # Multimodal input and reasoning, so discovery has something
                # richer than "text in, text out" to report.
                entry(
                    "upstream/big-model",
                    "3.00",
                    "15.00",
                    ctx=200_000,
                    accepts=("text", "image"),
                    features=("tools", "json_mode", "reasoning"),
                ),
                entry("upstream/dollar-model", "1.00", "2.00", currency="USD"),
                entry(
                    "upstream/embed-model",
                    "0.02",
                    "0",
                    ctx=8192,
                    modalities=("embeddings",),
                    features=(),
                ),
                entry(
                    "upstream/image-model",
                    "0",
                    "0",
                    ctx=0,
                    modalities=("image",),
                    features=(),
                ),
            ]
        }
    )


app = Starlette(
    routes=[
        Route("/v1/chat/completions", chat_completions, methods=["POST"]),
        Route("/v1/embeddings", embeddings, methods=["POST"]),
        Route("/v1/responses", responses, methods=["POST"]),
        Route("/v1/messages", messages, methods=["POST"]),
        Route("/v1/images/generations", images, methods=["POST"]),
        Route("/v1/models", models, methods=["GET"]),
        Route("/_last_request", last_request, methods=["GET"]),
    ]
)
