"""Server-side memory of Anthropic's thinking blocks between two requests.

Anthropic requires the ``thinking`` / ``redacted_thinking`` blocks of an
assistant turn (with their ``signature``) to come back, unmodified and in
order, when the tool results for that turn are sent. An OpenAI-shaped client
cannot carry them: the chat completions message has no place for a signature.
So the gateway keeps them: when a reply holds thinking and tool calls, the
blocks are stored under each tool call id, and the next request whose assistant
message carries one of those ids gets them back in front of its ``tool_use``
blocks.

Keyed by a hash of the caller as well as the tool call id, so one caller can
never retrieve (or be handed) another's blocks by guessing an id, and holding a
model name, because a signature is only valid for the model that wrote it.

Valkey when the deployment has it (``app.state.valkey``), a bounded
process-local dict otherwise, and a miss on any failure: a lost block degrades
the request (it is sent without them), it never fails it.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import OrderedDict
from collections.abc import Iterable
from typing import Any

import orjson

logger = logging.getLogger(__name__)

_PREFIX = "pystino:anthropic:thinking"
_MAX_LOCAL_ENTRIES = 10_000


def principal_key(user_id: object) -> str:
    """A short stable hash of the caller, for a key that is safe to log."""
    return hashlib.sha256(f"pystino-thinking:{user_id}".encode()).hexdigest()[:24]


class ThinkingStore:
    """``put`` after a reply, ``get`` before the next request."""

    def __init__(self, valkey: Any | None = None, *, clock: Any = time.monotonic) -> None:
        self._valkey = valkey
        self._clock = clock
        self._local: OrderedDict[str, tuple[float, bytes]] = OrderedDict()

    @staticmethod
    def _key(owner: str, tool_call_id: str) -> str:
        return f"{_PREFIX}:{owner}:{tool_call_id}"

    async def put(
        self,
        owner: str,
        tool_call_ids: Iterable[str],
        record: dict[str, Any],
        *,
        ttl_seconds: int,
    ) -> None:
        data = orjson.dumps(record)
        for tool_call_id in tool_call_ids:
            key = self._key(owner, tool_call_id)
            if self._valkey is not None:
                try:
                    await self._valkey.set(key, data, ex=ttl_seconds)
                    continue
                except Exception:
                    logger.warning("thinking store: valkey write failed; keeping it in process")
            self._local[key] = (self._clock() + ttl_seconds, data)
            self._local.move_to_end(key)
            while len(self._local) > _MAX_LOCAL_ENTRIES:
                self._local.popitem(last=False)

    async def get(
        self, owner: str, tool_call_ids: Iterable[str], *, model: str
    ) -> dict[str, Any] | None:
        """The record stored under any of the ids, if it was written for *model*."""
        for tool_call_id in tool_call_ids:
            key = self._key(owner, tool_call_id)
            raw: bytes | str | None = None
            if self._valkey is not None:
                try:
                    raw = await self._valkey.get(key)
                except Exception:
                    logger.warning("thinking store: valkey read failed")
            if raw is None and (entry := self._local.get(key)) is not None:
                if entry[0] > self._clock():
                    raw = entry[1]
                else:
                    self._local.pop(key, None)
            if raw is None:
                continue
            try:
                record = orjson.loads(raw)
            except orjson.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("model") == model:
                return record
        return None


def store_for(app_state: Any) -> ThinkingStore:
    """The process's store, made on first use from ``app.state.valkey``."""
    store = getattr(app_state, "thinking_store", None)
    if store is None:
        store = ThinkingStore(getattr(app_state, "valkey", None))
        app_state.thinking_store = store
    return store
