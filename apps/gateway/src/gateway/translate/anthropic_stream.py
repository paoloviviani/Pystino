"""Anthropic's streaming events as ``chat.completion.chunk`` frames.

Events (https://platform.claude.com/docs/en/build-with-claude/streaming):
``message_start``, then per content block ``content_block_start`` /
``content_block_delta`` (``text_delta``, ``thinking_delta``, ``signature_delta``,
``input_json_delta``) / ``content_block_stop``, then ``message_delta`` (stop
reason, *cumulative* usage), ``message_stop``; ``ping`` and ``error`` at any
point. Unknown event and delta types are ignored, as the docs ask.

OpenAI's tool-call ``index`` counts tool calls, not content blocks, so a reply
of ``thinking, text, tool_use, tool_use`` streams tool calls 0 and 1.

The translator also rebuilds the content blocks as they complete, so that the
reply's thinking blocks (with signatures) can be stored for the next request
of a tool loop. It never stores anything itself: the caller does, once the
stream has ended (``record()``).
"""

from __future__ import annotations

import time
from typing import Any

from gateway.sse import SSEEvent
from gateway.translate.anthropic_chat import (
    error_to_openai,
    finish_reason,
    merge_usage,
    thinking_record,
    usage_to_openai,
)


class MessagesToChatStream:
    """Feed Messages events in, get chat completion chunks out."""

    def __init__(self, *, client_model: str, upstream_model: str) -> None:
        self._client_model = client_model
        self._upstream_model = upstream_model
        self._id = "chatcmpl-anthropic"
        self._created = int(time.time())
        self._usage: dict[str, Any] = {}
        self._stop_reason: str | None = None
        self._blocks: dict[int, dict[str, Any]] = {}
        self._finished_blocks: list[dict[str, Any]] = []
        self._tool_index: dict[int, int] = {}
        self._tool_count = 0
        self._sent_role = False
        self.done = False

    # -- helpers ---------------------------------------------------------

    def _chunk(
        self, delta: dict[str, Any], *, finish: str | None = None, usage: bool = False
    ) -> SSEEvent:
        payload: dict[str, Any] = {
            "id": self._id,
            "object": "chat.completion.chunk",
            "created": self._created,
            "model": self._client_model,
            "choices": [] if usage else [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if usage:
            payload["usage"] = usage_to_openai(self._usage)
        return SSEEvent.from_json(payload)

    def _role_chunk(self) -> list[SSEEvent]:
        if self._sent_role:
            return []
        self._sent_role = True
        return [self._chunk({"role": "assistant", "content": ""})]

    # -- the translation -------------------------------------------------

    def feed(self, event: SSEEvent) -> list[SSEEvent]:
        """The frames for one upstream event (possibly none)."""
        if self.done:
            return []
        if event.is_comment_only():
            return []
        payload = event.json()
        kind = payload.get("type") if isinstance(payload, dict) else event.event
        if not isinstance(payload, dict):
            return []
        if kind == "ping":
            return [SSEEvent(comments=[" ping"])]
        if kind == "error":
            return self._error(payload)
        handler = getattr(self, f"_on_{kind}", None)
        return handler(payload) if handler else []

    def _on_message_start(self, payload: dict[str, Any]) -> list[SSEEvent]:
        message = payload.get("message") or {}
        self._id = message.get("id") or self._id
        merge_usage(self._usage, message.get("usage"))
        return self._role_chunk()

    def _on_content_block_start(self, payload: dict[str, Any]) -> list[SSEEvent]:
        index = payload.get("index", 0)
        block = dict(payload.get("content_block") or {})
        kind = block.get("type")
        out = self._role_chunk()
        if kind == "tool_use":
            position = self._tool_count
            self._tool_count += 1
            self._tool_index[index] = position
            block["_args"] = ""
            out.append(
                self._chunk(
                    {
                        "tool_calls": [
                            {
                                "index": position,
                                "id": block.get("id"),
                                "type": "function",
                                "function": {"name": block.get("name"), "arguments": ""},
                            }
                        ]
                    }
                )
            )
        elif kind == "text":
            block["text"] = ""
        elif kind == "thinking":
            block.setdefault("thinking", "")
            block.setdefault("signature", "")
        self._blocks[index] = block
        if kind == "text" and (initial := (payload.get("content_block") or {}).get("text")):
            block["text"] = initial
            out.append(self._chunk({"content": initial}))
        return out

    def _on_content_block_delta(self, payload: dict[str, Any]) -> list[SSEEvent]:
        index = payload.get("index", 0)
        delta = payload.get("delta") or {}
        block = self._blocks.get(index)
        kind = delta.get("type")
        if block is None:
            return []
        if kind == "text_delta" and (text := delta.get("text")):
            block["text"] = block.get("text", "") + text
            return [self._chunk({"content": text})]
        if kind == "thinking_delta":
            text = delta.get("thinking") or ""
            block["thinking"] = block.get("thinking", "") + text
            return [self._chunk({"reasoning_content": text})] if text else []
        if kind == "signature_delta":
            block["signature"] = block.get("signature", "") + (delta.get("signature") or "")
            return []
        if kind == "input_json_delta":
            partial = delta.get("partial_json") or ""
            block["_args"] = block.get("_args", "") + partial
            if not partial:
                return []
            return [
                self._chunk(
                    {
                        "tool_calls": [
                            {
                                "index": self._tool_index.get(index, 0),
                                "function": {"arguments": partial},
                            }
                        ]
                    }
                )
            ]
        return []

    def _on_content_block_stop(self, payload: dict[str, Any]) -> list[SSEEvent]:
        block = self._blocks.pop(payload.get("index", 0), None)
        if block is not None:
            block.pop("_args", None)
            self._finished_blocks.append(block)
        return []

    def _on_message_delta(self, payload: dict[str, Any]) -> list[SSEEvent]:
        delta = payload.get("delta") or {}
        if delta.get("stop_reason"):
            self._stop_reason = delta["stop_reason"]
        merge_usage(self._usage, payload.get("usage"))
        return []

    def _on_message_stop(self, payload: dict[str, Any]) -> list[SSEEvent]:
        self.done = True
        return [
            *self._role_chunk(),
            self._chunk({}, finish=finish_reason(self._stop_reason)),
            # Always sent: the ledger reads it. The route's usage stage drops it
            # again unless the client asked for stream_options.include_usage.
            self._chunk({}, usage=True),
            SSEEvent.done(),
        ]

    def _error(self, payload: dict[str, Any]) -> list[SSEEvent]:
        self.done = True
        _, body = error_to_openai(500, payload)
        return [SSEEvent.from_json(body), SSEEvent.done()]

    # -- after the stream ------------------------------------------------

    def record(self) -> dict[str, Any] | None:
        """The thinking record to store, once the stream completed."""
        blocks = []
        for block in self._finished_blocks:
            if block.get("type") == "tool_use":
                blocks.append({"type": "tool_use", "id": block.get("id")})
            else:
                blocks.append(block)
        return thinking_record(self._upstream_model, blocks)
