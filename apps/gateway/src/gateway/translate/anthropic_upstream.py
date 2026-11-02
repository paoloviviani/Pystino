"""The seam: an upstream that serves chat completions through ``/v1/messages``.

``routers/chat.py`` talks to an upstream through two calls
(``chat_completion`` and ``stream_chat_completion``) and reads OpenAI shapes
from them. This wrapper offers the same two calls over an
``OpenAICompatibleUpstream`` whose plugin speaks the Anthropic protocol (``protocol``), so
redaction, quota, accounting and the response stages are untouched: they run
on the OpenAI request before it reaches the wrapper and on the OpenAI response
that leaves it. Redaction therefore applies before translation, and
placeholder restoration applies to the translated-back text, reasoning and
tool-call arguments, exactly as for any other provider.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import orjson

from gateway.config import AnthropicSettings
from gateway.errors import error_payload
from gateway.plugins.base import PROTOCOL_ANTHROPIC, protocol_of
from gateway.sse import SSEEvent, iter_sse_events
from gateway.translate.anthropic_chat import (
    TranslatedRequest,
    TranslationError,
    assistant_tool_ids,
    build_request,
    error_to_openai,
    message_to_chat_completion,
    thinking_record,
)
from gateway.translate.anthropic_stream import MessagesToChatStream
from gateway.translate.thinking_store import ThinkingStore
from gateway.upstream import OpenAICompatibleUpstream, UpstreamResponse

logger = logging.getLogger(__name__)


def serves_chat_via_messages(upstream: OpenAICompatibleUpstream) -> bool:
    """Whether the provider's protocol is Anthropic's (``plugin.protocol``)."""
    return protocol_of(upstream.plugin) == PROTOCOL_ANTHROPIC


class _TranslatedStream:
    """What ``stream_chat_completion`` yields: status, headers, bytes."""

    def __init__(
        self,
        status_code: int,
        headers: Mapping[str, str],
        chunks: AsyncIterator[bytes],
        error_body: bytes | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers
        self._chunks = chunks
        self._error_body = error_body

    def aiter_bytes(self) -> AsyncIterator[bytes]:
        return self._chunks

    async def aread(self) -> bytes:
        return self._error_body or b""


class MessagesBackedChatUpstream:
    """``chat_completion`` / ``stream_chat_completion`` over ``/v1/messages``."""

    def __init__(
        self,
        upstream: OpenAICompatibleUpstream,
        *,
        settings: AnthropicSettings,
        store: ThinkingStore,
        owner: str,
        client_model: str,
        max_output_tokens: int | None,
    ) -> None:
        self._upstream = upstream
        self._settings = settings
        self._store = store
        self._owner = owner
        self._client_model = client_model
        self._max_output_tokens = max_output_tokens

    async def _translate(self, payload: Mapping[str, Any]) -> TranslatedRequest:
        model = str(payload.get("model", ""))
        records: dict[int, Mapping[str, Any]] = {}
        for position, message in enumerate(payload.get("messages") or []):
            if (
                isinstance(message, dict)
                and message.get("role") == "assistant"
                and (ids := assistant_tool_ids(message))
                and (record := await self._store.get(self._owner, ids, model=model)) is not None
            ):
                records[position] = record
        return build_request(
            payload,
            settings=self._settings,
            max_output_tokens=self._max_output_tokens,
            records=records,
        )

    @staticmethod
    def _refusal(exc: TranslationError) -> UpstreamResponse:
        body = error_payload(exc.message, param=exc.param)
        return UpstreamResponse(status_code=400, payload=body, raw=orjson.dumps(body))

    async def chat_completion(
        self, payload: Mapping[str, Any], *, request_id: str | None = None
    ) -> UpstreamResponse:
        try:
            request = await self._translate(payload)
        except TranslationError as exc:
            return self._refusal(exc)
        response = await self._upstream.post_json(
            "/messages",
            request.body,
            request_id=request_id,
            extra_headers=request.headers or None,
        )
        if not response.ok:
            status, body = error_to_openai(response.status_code, response.payload)
            return UpstreamResponse(
                status_code=status,
                payload=body,
                raw=orjson.dumps(body),
                headers=response.headers,
            )
        message = response.payload or {}
        await self._remember(message.get("content"), str(payload.get("model", "")))
        translated = message_to_chat_completion(message, client_model=self._client_model)
        return UpstreamResponse(
            status_code=response.status_code,
            payload=translated,
            raw=orjson.dumps(translated),
            headers=response.headers,
        )

    async def _remember(self, content: Any, model: str) -> None:
        record = thinking_record(model, [b for b in content or [] if isinstance(b, dict)])
        if record is not None:
            await self._store.put(
                self._owner,
                record["tool_ids"],
                record,
                ttl_seconds=self._settings.thinking_ttl_seconds,
            )

    def stream_chat_completion(
        self, payload: Mapping[str, Any], *, request_id: str | None = None
    ) -> AbstractAsyncContextManager[Any]:
        return self._stream(payload, request_id)

    @asynccontextmanager
    async def _stream(
        self, payload: Mapping[str, Any], request_id: str | None
    ) -> AsyncIterator[_TranslatedStream]:
        try:
            request = await self._translate(payload)
        except TranslationError as exc:
            body = orjson.dumps(error_payload(exc.message, param=exc.param))
            yield _TranslatedStream(400, {}, _empty(), body)
            return
        async with self._upstream.stream(
            "/messages",
            request.body,
            request_id=request_id,
            extra_headers=request.headers or None,
        ) as response:
            if response.status_code >= 400:
                raw = await response.aread()
                try:
                    parsed = orjson.loads(raw) if raw else None
                except orjson.JSONDecodeError:
                    parsed = None
                status, error = error_to_openai(
                    response.status_code, parsed if isinstance(parsed, dict) else None
                )
                yield _TranslatedStream(status, response.headers, _empty(), orjson.dumps(error))
                return
            translator = MessagesToChatStream(
                client_model=self._client_model,
                upstream_model=str(payload.get("model", "")),
            )
            yield _TranslatedStream(
                response.status_code,
                response.headers,
                self._frames(response.aiter_bytes(), translator),
            )

    async def _frames(
        self, upstream_bytes: AsyncIterator[bytes], translator: MessagesToChatStream
    ) -> AsyncIterator[bytes]:
        async for event in iter_sse_events(upstream_bytes):
            frames = translator.feed(event)
            if translator.done and (record := translator.record()) is not None:
                # Before the last frames go out, so a client that hangs up on
                # [DONE] cannot beat the write.
                try:
                    await self._store.put(
                        self._owner,
                        record["tool_ids"],
                        record,
                        ttl_seconds=self._settings.thinking_ttl_seconds,
                    )
                except Exception:
                    logger.exception("could not store thinking blocks")
            for frame in frames:
                yield frame.encode()
            if translator.done:
                return
        # The connection ended before message_stop: say so rather than leave the
        # client with a stream that stops mid-sentence and looks complete.
        body = error_payload(
            "The upstream provider ended the stream before the reply was complete.",
            type_="api_error",
            code="stream_interrupted",
        )
        yield SSEEvent.from_json(body).encode()
        yield SSEEvent.done().encode()


async def _empty() -> AsyncIterator[bytes]:
    nothing: list[bytes] = []
    for chunk in nothing:
        yield chunk
