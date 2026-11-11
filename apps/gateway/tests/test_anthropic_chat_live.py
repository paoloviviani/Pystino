"""Live check of chat completions over Anthropic's Messages API.

Skipped unless ``ANTHROPIC_API_KEY`` is set, so CI never calls Anthropic::

    ANTHROPIC_API_KEY=sk-ant-... uv run pytest apps/gateway/tests/test_anthropic_chat_live.py -s -q

It runs the real gateway (ASGI, SQLite ledger, the translation seam) against
api.anthropic.com, with a handful of small requests on Haiku 5.5 and one
tool loop on Sonnet 5.5. Set ``ANTHROPIC_LIVE_MODEL`` / ``ANTHROPIC_LIVE_THINKING_MODEL``
to use others. It prints what it observed; the key is never printed.

``test_5`` is the one to run for prompt caching across a tool loop (buffered and
streamed)::

    ANTHROPIC_API_KEY=sk-ant-... uv run pytest apps/gateway/tests/test_anthropic_chat_live.py \\
        -k test_5 -s -q
"""

from __future__ import annotations

import base64
import os
import uuid
import zlib
from collections.abc import AsyncIterator
from decimal import Decimal
from itertools import pairwise
from typing import Any

import httpx
import orjson
import pytest
from conftest import Seeded
from gateway.models import GroupModelAccess, ModelDef, Provider, UsageRecord
from gateway.secrets import SecretBox, hint_for
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_anthropic_cache_prefix import breakpoint_labels, hits

KEY = os.environ.get("ANTHROPIC_API_KEY", "")
pytestmark = pytest.mark.skipif(not KEY, reason="ANTHROPIC_API_KEY not set")

FAST = os.environ.get("ANTHROPIC_LIVE_MODEL", "claude-haiku-5-5")
# The 4.5 generation thinks whenever it is given a budget; the 5.x models decide
# for themselves and skip thinking on an easy question, which would leave the
# signature store unexercised.
THINKER = os.environ.get("ANTHROPIC_LIVE_THINKING_MODEL", "claude-sonnet-4-5-20250929")


#: Every response from /v1/messages, raw, so the check can set Anthropic's own
#: usage beside what the gateway recorded.
WIRE: list[bytes] = []
#: The matching request bodies, to see what the gateway sent back on the second call.
REQUESTS: list[bytes] = []


class _Recording(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self._inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # Uncompressed, so the recording is readable.
        request.headers["accept-encoding"] = "identity"
        response = await self._inner.handle_async_request(request)
        if not str(request.url).endswith("/messages"):
            return response
        captured = bytearray()
        REQUESTS.append(request.content)
        WIRE.append(b"")
        slot = len(WIRE) - 1
        inner_stream = response.stream

        class Tee(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                async for chunk in inner_stream:  # type: ignore[union-attr]
                    captured.extend(chunk)
                    WIRE[slot] = bytes(captured)
                    yield chunk

            async def aclose(self) -> None:
                await inner_stream.aclose()  # type: ignore[union-attr]

        response.stream = Tee()
        return response

    async def aclose(self) -> None:
        # The app closes its upstream client at shutdown, and httpx closes the transport it was
        # given. Without this the inner pool's sockets outlive the run, and pytest ends it
        # with an ExceptionGroup of ResourceWarnings after the tests passed.
        await self._inner.aclose()


class LiveUpstream:
    """Stands in for the ``fake_upstream`` fixture: a real HTTP client."""

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=_Recording(),
            timeout=httpx.Timeout(connect=10, read=None, write=10, pool=10),
        )


def provider_usage(index: int = -1) -> dict[str, Any]:
    """Anthropic's own usage for one /v1/messages call (JSON or SSE)."""
    raw = WIRE[index].decode()
    if raw.lstrip().startswith("{"):
        return dict(orjson.loads(raw)["usage"])
    merged: dict[str, Any] = {}
    for line in raw.splitlines():
        if not line.startswith("data: "):
            continue
        frame = orjson.loads(line[6:])
        found = (
            (frame.get("message") or {}).get("usage")
            if frame["type"] == "message_start"
            else (frame.get("usage") if frame["type"] == "message_delta" else None)
        )
        for key, value in (found or {}).items():
            if value is not None:
                merged[key] = value
    return merged


@pytest.fixture
def fake_upstream() -> LiveUpstream:
    return LiveUpstream()


@pytest.fixture
async def live(
    app: Any,
    seeded: Seeded,
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, str]:
    """The seeded provider made an Anthropic one, and two models adopted through
    the real discovery and import routes, so the prices are the built-in table's."""
    from test_admin import as_user, make_admin

    box = SecretBox(["test-encryption-key-not-for-production"])
    async with session_factory() as db:
        await db.execute(
            update(Provider)
            .where(Provider.id == seeded.provider.id)
            .values(
                plugin="anthropic",
                base_url="https://api.anthropic.com/v1",
                api_key_encrypted=box.encrypt(KEY),
                api_key_hint=hint_for(KEY),
            )
        )
        await db.commit()
    app.state.settings.billing_currency = "USD"
    as_user(app, await make_admin(session_factory, seeded))
    discovered = (
        await client.get(f"/api/admin/models/discover?provider_id={seeded.provider.id}")
    ).json()
    print("\n[discovery] GET /v1/models with built-in prices (USD per Mtok):")
    for row in discovered["available"]:
        print(
            f"  {row['upstream_model']:<28} ctx={row['context_window']} "
            f"out={row['max_output_tokens']} "
            f"{row['input_per_mtok']}/{row['output_per_mtok']} src={row['price_source']} "
            f"in={','.join(row['input_modalities'])} feat={','.join(row['supported_features'])}"
        )
    names = {"fast": "live-fast", "think": "live-think"}
    imported = await client.post(
        f"/api/admin/models/import?provider_id={seeded.provider.id}",
        json={
            "models": [
                {"upstream_model": FAST, "name": names["fast"]},
                {"upstream_model": THINKER, "name": names["think"]},
            ]
        },
    )
    assert imported.status_code == 201, imported.text
    assert all(r["imported"] for r in imported.json()["results"]), imported.text
    async with session_factory() as db:
        for name in names.values():
            row = (await db.execute(select(ModelDef).where(ModelDef.name == name))).scalar_one()
            db.add(GroupModelAccess(group_id=seeded.group.id, model_id=row.id))
        await db.commit()
    # the admin override above is dropped so the seeded API key authenticates the calls
    app.dependency_overrides.clear()
    return names


def frames(text: str) -> list[dict[str, Any]]:
    return [
        orjson.loads(line[6:])
        for line in text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


async def last_row(session_factory: async_sessionmaker[AsyncSession]) -> UsageRecord:
    async with session_factory() as db:
        return (
            await db.execute(select(UsageRecord).order_by(UsageRecord.created_at.desc()).limit(1))
        ).scalar_one()


#: USD per Mtok from the built-in table: input, output, cache read, 5-minute write.
PRICES = {
    "claude-haiku-5-5": ("0.50", "2.50", "0.05", "0.625"),
    "claude-sonnet-5-5": ("2", "10", "0.10", "2.5"),
    "claude-sonnet-4-5-20250929": ("3", "15", "0.30", "3.75"),
}


async def show(
    label: str, model: str, session_factory: async_sessionmaker[AsyncSession], wire: int = -1
) -> UsageRecord:
    """Anthropic's usage, the ledger row and the hand computation, side by side."""
    raw = provider_usage(wire)
    row = await last_row(session_factory)
    print(f"[{label}] provider usage : {orjson.dumps(raw).decode()}")
    print(
        f"[{label}] ledger buckets : prompt={row.prompt_tokens} cached={row.cached_prompt_tokens} "
        f"cache_write={row.cache_write_tokens} completion={row.completion_tokens}"
    )
    read = raw.get("cache_read_input_tokens", 0)
    write = raw.get("cache_creation_input_tokens", 0)
    uncached = raw.get("input_tokens", 0)
    assert row.prompt_tokens == uncached + read + write
    assert (row.cached_prompt_tokens, row.cache_write_tokens) == (read, write)
    assert row.completion_tokens == raw["output_tokens"]
    if model in PRICES:
        p_in, p_out, p_read, p_write = (Decimal(x) for x in PRICES[model])
        one_hour = (raw.get("cache_creation") or {}).get("ephemeral_1h_input_tokens", 0)
        expected = (
            uncached * p_in
            + read * p_read
            + (write - one_hour) * p_write
            + one_hour * p_write * Decimal("1.6")
            + raw["output_tokens"] * p_out
        ) / 1_000_000
        print(f"[{label}] cost           : ledger={row.cost} by hand={expected} USD")
        assert abs(Decimal(str(row.cost)) - expected) < Decimal("0.00000001")
    return row


def png(width: int = 64, height: int = 64) -> str:
    """A solid red PNG, made here so the check needs no fixture file."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return len(data).to_bytes(4, "big") + body + zlib.crc32(body).to_bytes(4, "big")

    rows = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    raw = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(
            b"IHDR", width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x02\x00\x00\x00"
        )
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )
    return base64.b64encode(raw).decode()


async def test_1_a_plain_streamed_answer(
    client: httpx.AsyncClient,
    seeded: Seeded,
    live: dict[str, str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": live["fast"],
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
        },
        headers=seeded.auth,
    )
    assert response.status_code == 200, response.text
    chunks = frames(response.text)
    text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks if c["choices"])
    print("\n[1] streamed text:", repr(text), "| usage:", chunks[-1].get("usage"))
    await show("1", FAST, session_factory)
    print(
        "[1] finish_reason:",
        [
            c["choices"][0]["finish_reason"]
            for c in chunks
            if c["choices"] and c["choices"][0]["finish_reason"]
        ],
    )
    assert "pong" in text.lower()
    assert chunks[-1]["usage"]["completion_tokens"] > 0


async def test_2_tool_loop_with_thinking_proves_the_signature_store(
    client: httpx.AsyncClient,
    seeded: Seeded,
    live: dict[str, str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tool = {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": "What is the weather in Rome, and what is 17 * 23 plus that "
            "temperature in Celsius? Use the tool.",
        }
    ]
    request = {
        "model": live["think"],
        "tools": [tool],
        "reasoning_effort": "medium",
        "max_tokens": 6000,
    }
    first = await client.post(
        "/v1/chat/completions",
        json={**request, "stream": True, "messages": messages},
        headers=seeded.auth,
    )
    assert first.status_code == 200, first.text
    calls: dict[int, dict[str, Any]] = {}
    reasoning = ""
    for chunk in frames(first.text):
        for choice in chunk["choices"]:
            reasoning += choice["delta"].get("reasoning_content") or ""
            for call in choice["delta"].get("tool_calls", []):
                slot = calls.setdefault(call["index"], {"arguments": ""})
                slot.update({k: call[k] for k in ("id",) if k in call})
                slot["name"] = call.get("function", {}).get("name", slot.get("name"))
                slot["arguments"] += call.get("function", {}).get("arguments") or ""
    print("\n[2] first reply: reasoning", repr(reasoning[:80]), "| tool calls:", calls)
    await show("2a", THINKER, session_factory)
    assert calls, "the model did not call the tool"
    messages.append(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": c["id"],
                    "type": "function",
                    "function": {"name": c["name"], "arguments": c["arguments"] or "{}"},
                }
                for c in calls.values()
            ],
        }
    )
    for c in calls.values():
        messages.append({"role": "tool", "tool_call_id": c["id"], "content": "18 C and sunny"})
    # The second request carries no signature at all: the gateway must supply the
    # thinking blocks it stored, or Anthropic answers 400 on this generation.
    second = await client.post(
        "/v1/chat/completions", json={**request, "messages": messages}, headers=seeded.auth
    )
    print(
        "[2] second status:",
        second.status_code,
        "| text:",
        repr(second.json()["choices"][0]["message"]["content"]),
    )
    assert second.status_code == 200, second.text
    await show("2b", THINKER, session_factory)
    # Anthropic's first reply carried a thinking block with a signature; the second
    # request, which the client sent without one, must have carried the same block.
    first_blocks = [
        orjson.loads(line[6:])
        for line in WIRE[-2].decode().splitlines()
        if line.startswith("data: ")
    ]
    signatures = [
        f["delta"]["signature"]
        for f in first_blocks
        if f.get("type") == "content_block_delta" and f["delta"].get("type") == "signature_delta"
    ]
    sent = orjson.loads(REQUESTS[-1])["messages"][1]["content"]
    replayed = [b for b in sent if b["type"] in ("thinking", "redacted_thinking")]
    print(
        f"[2] thinking blocks in the first reply: {len(signatures)}; "
        f"replayed in the second request: {len(replayed)}; "
        f"signature (first 12 chars) {signatures[0][:12] if signatures else None!r} == "
        f"{replayed[0].get('signature', '')[:12] if replayed else None!r}"
    )
    assert signatures, "the model did not think; raise the effort or the difficulty"
    assert replayed[0]["signature"] == signatures[0]
    print(
        "[2] request thinking config:",
        orjson.loads(REQUESTS[-1]).get("thinking"),
        orjson.loads(REQUESTS[-1]).get("output_config"),
    )
    assert "18" in second.json()["choices"][0]["message"]["content"]


async def test_3_prompt_caching_reads_and_is_billed_at_cache_prices(
    client: httpx.AsyncClient,
    seeded: Seeded,
    live: dict[str, str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Well over the minimum cacheable prefix (512 to 4096 tokens by model).
    # A fresh prefix each run: an earlier run inside the 5-minute TTL would
    # otherwise turn the first call into a cache read.
    system = f"You answer in one word. Run {uuid.uuid4()}. Reference notes: " + " ".join(
        f"Note {n}: the quick brown fox number {n} jumps over the lazy dog." for n in range(900)
    )
    body = {
        "model": live["fast"],
        "max_tokens": 16,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": "Say ok."},
        ],
    }
    usages = []
    for label in ("first", "second"):
        response = await client.post("/v1/chat/completions", json=body, headers=seeded.auth)
        assert response.status_code == 200, response.text
        usage = response.json()["usage"]
        print(f"\n[3] {label}: chat usage={usage}")
        row = await show(f"3 {label}", FAST, session_factory)
        usages.append((usage, row))
    first_usage, _ = usages[0]
    second_usage, second_row = usages[1]
    assert first_usage["prompt_tokens_details"]["cache_write_tokens"] > 0
    assert second_usage["prompt_tokens_details"]["cached_tokens"] > 0
    assert second_row.cached_prompt_tokens == second_usage["prompt_tokens_details"]["cached_tokens"]


async def test_4_an_image_part(
    client: httpx.AsyncClient,
    seeded: Seeded,
    live: dict[str, str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": live["fast"],
            "max_tokens": 32,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What single colour is this image? One word."},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{png()}"},
                        },
                    ],
                }
            ],
        },
        headers=seeded.auth,
    )
    assert response.status_code == 200, response.text
    answer = response.json()["choices"][0]["message"]["content"]
    print("\n[4] image answer:", repr(answer))
    await show("4", FAST, session_factory)
    assert "red" in answer.lower()


def tool_calls_of(
    response: httpx.Response, *, stream: bool
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(assistant message, usage)`` from a buffered or a streamed chat completion."""
    if not stream:
        body = response.json()
        return body["choices"][0]["message"], body["usage"]
    calls: dict[int, dict[str, Any]] = {}
    text = ""
    usage: dict[str, Any] = {}
    for chunk in frames(response.text):
        usage = chunk.get("usage") or usage
        for choice in chunk["choices"]:
            text += choice["delta"].get("content") or ""
            for call in choice["delta"].get("tool_calls", []):
                slot = calls.setdefault(call["index"], {"arguments": ""})
                slot.update({k: call[k] for k in ("id",) if k in call})
                slot["name"] = call.get("function", {}).get("name", slot.get("name"))
                slot["arguments"] += call.get("function", {}).get("arguments") or ""
    assistant = {
        "role": "assistant",
        "content": text or None,
        "tool_calls": [
            {
                "id": c["id"],
                "type": "function",
                "function": {"name": c["name"], "arguments": c["arguments"] or "{}"},
            }
            for c in calls.values()
        ],
    }
    return assistant, usage


@pytest.mark.parametrize("stream", [False, True], ids=["buffered", "streamed"])
async def test_5_a_tool_loop_reads_more_of_the_cache_at_every_step(
    stream: bool,
    client: httpx.AsyncClient,
    seeded: Seeded,
    live: dict[str, str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Four steps of a forced tool loop: ``cached_tokens`` must grow at every step.

    Step 1 writes the prefix (tools, system, first turn). Step 2 reads up to the first
    turn, which step 1's newest-turn breakpoint wrote, and writes the tool exchange.
    Step 3 reads through step 2's, and so on: that is the whole point of the
    "previous user turn" breakpoint. Two things are checked, in this order, because
    they have different owners:

    1. what the gateway sent: every breakpoint of the request before is a byte prefix
       of the request after (``test_anthropic_cache_prefix.py`` does this offline;
       here it is the real wire). If this fails, the gateway changed the prefix.
    2. what Anthropic did with it: ``cached_tokens`` rises at each step, ``cache_write_tokens``
       is positive at each, the chat usage equals the provider's own, and the ledger
       row matches (``show``). If only this fails, rerun once before suspecting the
       gateway: a cache entry is only readable once the response that wrote it has
       started, and Anthropic does not promise a hit.

    The system prompt and the tool description carry a fresh uuid, so no earlier run can
    have written any part of the prefix and step 1 is a clean miss. ``tool_choice`` is
    ``required`` so the model calls the tool on every step without being asked in words,
    and it never changes, since a changed ``tool_choice`` invalidates the message cache.
    """
    run = uuid.uuid4()
    system = f"You look notes up with the tool. Run {run}. Reference notes: " + " ".join(
        f"Note {n}: the quick brown fox number {n} jumps over the lazy dog." for n in range(400)
    )
    tool = {
        "type": "function",
        "function": {
            "name": "lookup_note",
            "description": f"Look a note up by its number (run {run}).",
            "parameters": {
                "type": "object",
                "properties": {"number": {"type": "integer"}},
                "required": ["number"],
            },
        },
    }
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": "Look up note 1, then keep looking up the next note."},
    ]
    sent: list[dict[str, Any]] = []
    usages: list[dict[str, Any]] = []
    label = "streamed" if stream else "buffered"
    for step in range(1, 5):
        request: dict[str, Any] = {
            "model": live["fast"],
            "max_tokens": 128,
            "tools": [tool],
            "tool_choice": "required",
            "messages": messages,
        }
        if stream:
            request |= {"stream": True, "stream_options": {"include_usage": True}}
        response = await client.post("/v1/chat/completions", json=request, headers=seeded.auth)
        assert response.status_code == 200, response.text
        assistant, usage = tool_calls_of(response, stream=stream)
        sent.append(orjson.loads(REQUESTS[-1]))
        usages.append(usage)
        details = usage["prompt_tokens_details"]
        print(
            f"\n[5 {label}] step {step}: prompt={usage['prompt_tokens']} "
            f"cached={details['cached_tokens']} written={details['cache_write_tokens']} "
            f"breakpoints={breakpoint_labels(sent[-1])}"
        )
        await show(f"5 {label} step {step}", FAST, session_factory)
        assert assistant["tool_calls"], "tool_choice=required, yet the model called no tool"
        messages.append(assistant)
        for call in assistant["tool_calls"]:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": f"Note {step}: " + "the answer is forty-two. " * 60,
                }
            )

    # 1. the gateway: a stable prefix, within Anthropic's four breakpoints
    for step, body in enumerate(sent, start=1):
        assert 1 <= len(breakpoint_labels(body)) <= 4, (step, breakpoint_labels(body))
    for step, (earlier, later) in enumerate(pairwise(sent), start=1):
        got = hits(earlier, later)
        assert all(got.values()), f"step {step} to {step + 1}: the prefix moved, {got}"

    # 2. Anthropic: the cache is read further at every step
    cached = [u["prompt_tokens_details"]["cached_tokens"] for u in usages]
    written = [u["prompt_tokens_details"]["cache_write_tokens"] for u in usages]
    print(f"[5 {label}] cached tokens per step: {cached}; written: {written}")
    assert cached[0] == 0, "step 1 read a cache that nothing could have written"
    assert all(w > 0 for w in written), written
    assert all(later > earlier for earlier, later in pairwise(cached)), cached
    for usage in usages:
        details = usage["prompt_tokens_details"]
        assert details["cached_tokens"] + details["cache_write_tokens"] <= usage["prompt_tokens"]
