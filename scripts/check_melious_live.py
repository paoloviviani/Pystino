#!/usr/bin/env python3
"""What Melious serves, per kind, against the real API (the plugin's evidence).

Direct to the vendor, no stack needed: the plugin's measured facts
(``plugins/melious.py``) are re-checked one request per kind. Spends well under
EUR 0.001. The key is read from ``MELIOUS_API_KEY_FILE`` (a path) or
``MELIOUS_API_KEY``, sent only to Melious, and never printed.

    MELIOUS_API_KEY_FILE=/path/to/key uv run python scripts/check_melious_live.py

Never run by CI: it needs a real key. Exit status 1 when a check fails.
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import wave
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps" / "gateway" / "src"))

from gateway.models import ModelKind
from gateway.plugins.registry import resolve
from gateway.pricing import kinds_by_id, parse_catalogue
from gateway.translate.decisions import translate_request, translate_response

BASE = os.environ.get("MELIOUS_BASE_URL", "https://api.melious.ai/v1")
FAILURES: list[str] = []


def key() -> str:
    path = os.environ.get("MELIOUS_API_KEY_FILE")
    value = Path(path).read_text().strip() if path else os.environ.get("MELIOUS_API_KEY", "")
    if not value:
        sys.exit("Set MELIOUS_API_KEY_FILE (a path) or MELIOUS_API_KEY.")
    return value


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILURES.append(label)


def silence() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 16000)
    return buffer.getvalue()


async def main() -> None:
    secret = key()
    headers = {"authorization": f"Bearer {secret}"}
    async with httpx.AsyncClient(timeout=120, headers=headers) as http:

        async def post(path: str, **kwargs: object) -> httpx.Response:
            return await http.post(f"{BASE}{path}", **kwargs)  # type: ignore[arg-type]

        print("catalogue: kinds and prices come from /models?include_meta=true")
        payload = await resolve("melious").fetch_catalogue(http, f"{BASE}/models", secret)  # type: ignore[attr-defined]
        kinds = kinds_by_id(payload)
        listed = {m["id"] for m in (await http.get(f"{BASE}/models")).json()["data"]}
        check("some models listed", len(listed) > 10, str(len(listed)))
        for model, want in {
            "bge-m3": ModelKind.EMBEDDING,
            "flux-2-klein-4b": ModelKind.IMAGE,
            "whisper-large-v3-turbo": ModelKind.TRANSCRIPTION,
            "clef-flash": ModelKind.DECISION,
            "mistral-small-3.2-24b-instruct": ModelKind.CHAT,
        }.items():
            check(f"{model} is {want.value}", kinds.get(model) is want, str(kinds.get(model)))
        prices = {p.model_id: p for p in parse_catalogue(payload)[0]}
        check("chat model priced", prices["mistral-small-3.2-24b-instruct"].input_per_mtok > 0)
        check("image priced per image", (prices["flux-2-klein-4b"].per_image or 0) > 0)
        check("whisper priced per minute", (prices["whisper-large-v3-turbo"].per_minute or 0) > 0)
        check("decision model priced", prices["clef-flash"].input_per_mtok > 0)
        check("the reranker is left out", not any("rerank" in m for m in kinds))
        check(
            "every listed model is classified or deliberately left out",
            len(kinds) >= len(listed) - 2,
        )

        print("chat: streaming usage, tools, reasoning")
        chat = {"model": "qwen3.6-27b", "max_tokens": 2000}
        tool = {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Weather for a city",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }
        r = await post(
            "/chat/completions",
            json={
                **chat,
                "messages": [{"role": "user", "content": "Weather in Paris? Use the tool."}],
                "tools": [tool],
            },
        )
        body = r.json() if r.status_code == 200 else {}
        message = (body.get("choices") or [{}])[0].get("message", {})
        check("tool call", bool(message.get("tool_calls")), f"HTTP {r.status_code}")
        check("reasoning_content", bool(message.get("reasoning_content")))
        check("usage with reasoning tokens", "reasoning_tokens" in (body.get("usage") or {}))
        stream = await post(
            "/chat/completions",
            json={
                "model": "mistral-small-3.2-24b-instruct",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
                "max_tokens": 10,
            },
        )
        frames = [ln for ln in stream.text.splitlines() if ln.startswith("data: {")]
        check(
            "stream ends with a usage frame, unasked", '"usage":{' in (frames[-1] if frames else "")
        )

        print("embeddings, rerank, images, audio")
        r = await post("/embeddings", json={"model": "bge-m3", "input": "hello"})
        check("embeddings", r.status_code == 200 and bool(r.json()["data"][0]["embedding"]))
        r = await post(
            "/rerank",
            json={"model": "qwen3-reranker-4b", "query": "a", "documents": ["a", "b"]},
        )
        check("rerank at /v1/rerank (no gateway surface)", r.status_code == 200, str(r.status_code))
        r = await post(
            "/images/generations",
            json={"model": "flux-2-klein-4b", "prompt": "a red dot", "size": "512x512"},
        )
        check("image (base64 only)", r.status_code == 200 and bool(r.json()["data"][0]["b64_json"]))
        r = await post(
            "/audio/transcriptions",
            data={"model": "whisper-large-v3-turbo"},
            files={"file": ("s.wav", silence(), "audio/wav")},
        )
        check("transcription", r.status_code == 200 and "text" in r.json(), str(r.status_code))

        print("decision models: System One only")
        r = await post(
            "/systemone",
            json={
                "model": "clef-flash",
                "state": {"action": "runs the unit tests"},
                "questions": {"q1": {"type": "noul", "instructions": "Is it routine and safe?"}},
            },
        )
        ok = r.status_code == 200 and "noul" in r.json()["answers"]["q1"]
        check("/systemone answers a noul question", ok, str(r.status_code))
        check("usage.input_tokens reported", bool(r.json().get("usage", {}).get("input_tokens")))
        r = await post(
            "/decisions",
            json={
                "model": "clef-flash",
                "input": "x",
                "questions": [{"type": "predicate", "name": "q", "instructions": "x?"}],
            },
        )
        check("/decisions is a 404 upstream", r.status_code == 404, str(r.status_code))

        print("/v1/decisions input form, translated to System One")
        questions = [
            {
                "type": "predicate",
                "name": "safe",
                "instructions": "Is the action routine and safe?",
            },
            {
                "type": "choice",
                "name": "team",
                "instructions": "Which team?",
                "choices": [{"value": "ops", "description": "operations"}, {"value": "dev"}],
            },
            {
                "type": "score",
                "name": "risk",
                "instructions": "How risky?",
                "levels": [{"label": "low"}, {"label": "medium"}, {"label": "high"}],
            },
        ]
        wire, plan = translate_request(
            {"model": "clef-flash", "input": "The agent runs `npm test`.", "questions": questions},
            source="decisions",
            target=resolve("melious").decisions.dialect,  # type: ignore[attr-defined]
        )
        r = await post("/systemone", json=wire)
        check("System One accepts the translated body", r.status_code == 200, r.text[:200])
        if r.status_code == 200:
            answers = translate_response(r.json(), plan)["answers"]
            check(
                "three answers, in order", [a["name"] for a in answers] == ["safe", "team", "risk"]
            )
            check("predicate probability in [0, 1]", 0 <= answers[0]["probability"] <= 1)
            check("choice is one of ours", answers[1]["choice"] in ("ops", "dev"))
            check(
                "score has labelled probabilities", answers[2]["probabilities"][0]["label"] == "low"
            )


asyncio.run(main())
print("\nFAILED: " + ", ".join(FAILURES) if FAILURES else "\nall checks passed")
sys.exit(1 if FAILURES else 0)
