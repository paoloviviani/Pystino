"""Builders for OpenAI-compatible payloads and SSE frames.

A plain module rather than fixtures in conftest, so test files can import these
directly. Kept out of conftest because pytest's conftest is not importable by
name from sibling test modules.
"""

from __future__ import annotations

import orjson


def sse(payload: dict | str) -> bytes:
    """Encode one SSE frame the way an OpenAI-compatible provider does."""
    if isinstance(payload, str):
        return f"data: {payload}\n\n".encode()
    return b"data: " + orjson.dumps(payload) + b"\n\n"


def chunk(
    content: str | None = None,
    *,
    finish_reason: str | None = None,
    usage: dict | None = None,
    model: str = "upstream/test-model",
    index: int = 0,
) -> dict:
    """Build a ``chat.completion.chunk`` payload."""
    payload: dict = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1_700_000_000,
        "model": model,
        "choices": [],
    }
    if content is not None or finish_reason is not None:
        choice: dict = {"index": index, "delta": {}}
        if content is not None:
            choice["delta"]["content"] = content
        if finish_reason is not None:
            choice["finish_reason"] = finish_reason
        payload["choices"] = [choice]
    if usage is not None:
        payload["usage"] = usage
    return payload


def usage_only_frame(prompt: int, completion: int, model: str = "upstream/test-model") -> dict:
    """The trailing frame OpenAI sends when ``include_usage`` is set.

    Note the empty ``choices``: this is the frame that must be dropped whole when
    the client did not ask for usage.
    """
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 1_700_000_000,
        "model": model,
        "choices": [],
        "usage": usage_payload(prompt, completion),
    }


def usage_payload(prompt: int, completion: int, **extra: object) -> dict:
    payload: dict = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }
    payload.update(extra)
    return payload


def completion_body(
    content: str = "hello",
    *,
    prompt: int = 10,
    completion: int = 5,
    model: str = "upstream/test-model",
    finish_reason: str = "stop",
) -> dict:
    """A complete, non-streamed chat completion response."""
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage_payload(prompt, completion),
    }
