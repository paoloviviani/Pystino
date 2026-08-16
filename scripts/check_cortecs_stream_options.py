#!/usr/bin/env python3
"""Does Cortecs reject ``stream_options``, or merely ignore it?

This is the one open question from ADR 0028 that cannot be answered from the
documentation. Cortecs sends usage on the last chunk of a stream
unconditionally, and ``stream_options`` is absent from their published schema —
but their schema also warns that unsupported parameters "can cause requests to
fail or limit the providers able to process them". Those two readings imply
different defaults for ``providers.forward_stream_options``, and guessing
between them is exactly what this project does not do.

So it is a script rather than a decision. **It needs a real Cortecs API key and
spends a small amount of real money** — four short completions, capped at eight
output tokens each.

    export CORTECS_API_KEY=...
    uv run python scripts/check_cortecs_stream_options.py

It answers three things:

1. whether a streamed request *with* ``stream_options`` succeeds;
2. whether a streamed request *without* it still reports usage;
3. whether the two are served by the same set of providers — the "limit the
   providers able to process them" clause, which is the failure mode that
   would never show up as an error.

Read the recommendation it prints, then set ``forward_stream_options`` on the
provider in the console to match.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx

BASE_URL = os.environ.get("CORTECS_BASE_URL", "https://api.cortecs.ai/v1")
MODEL = os.environ.get("CORTECS_MODEL", "")
MAX_TOKENS = 8
ATTEMPTS = 2


def die(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(2)


def pick_model(client: httpx.Client) -> str:
    """The cheapest chat model the key can reach, so the check costs as little as possible."""
    response = client.get(f"{BASE_URL}/models")
    response.raise_for_status()
    entries = response.json().get("data") or []

    def rate(entry: dict[str, Any]) -> float:
        pricing = entry.get("pricing") or {}
        try:
            return float(pricing.get("output_token", "inf"))
        except (TypeError, ValueError):
            return float("inf")

    usable = [
        entry
        for entry in entries
        if isinstance(entry, dict)
        and "embed" not in str(entry.get("id", "")).lower()
        and "image" not in [str(m).lower() for m in entry.get("output_modalities") or []]
    ]
    if not usable:
        die("the catalogue returned no chat models for this key")
    return str(min(usable, key=rate)["id"])


def stream_once(client: httpx.Client, model: str, *, with_options: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "Say OK."}],
        "max_tokens": MAX_TOKENS,
        "stream": True,
    }
    if with_options:
        payload["stream_options"] = {"include_usage": True}

    result: dict[str, Any] = {
        "ok": False,
        "status": None,
        "usage": None,
        "provider": None,
        "error": None,
    }
    try:
        with client.stream(
            "POST",
            f"{BASE_URL}/chat/completions",
            json=payload,
            headers={"accept": "text/event-stream"},
        ) as response:
            result["status"] = response.status_code
            if response.status_code >= 400:
                result["error"] = response.read()[:400].decode("utf-8", "replace")
                return result
            for line in response.iter_lines():
                if not line.startswith("data: "):
                    continue
                body = line[6:].strip()
                if body == "[DONE]":
                    continue
                try:
                    frame = json.loads(body)
                except json.JSONDecodeError:
                    continue
                if isinstance(frame.get("usage"), dict):
                    result["usage"] = frame["usage"]
                if isinstance(frame.get("provider"), str):
                    result.setdefault("provider", None)
                    result["provider"] = frame["provider"]
            result["ok"] = True
    except httpx.HTTPError as exc:
        result["error"] = str(exc)
    return result


def describe(label: str, result: dict[str, Any]) -> None:
    print(f"  {label}")
    print(f"    http           : {result['status']}")
    print(f"    completed      : {result['ok']}")
    print(f"    usage reported : {'yes' if result['usage'] else 'NO'}")
    print(f"    served by      : {result['provider'] or '(not reported)'}")
    if result["error"]:
        print(f"    error          : {result['error']}")


def main() -> int:
    api_key = os.environ.get("CORTECS_API_KEY")
    if not api_key:
        die("set CORTECS_API_KEY. This check calls the real API and costs real money.")

    with httpx.Client(
        timeout=httpx.Timeout(connect=10, read=60, write=30, pool=10),
        headers={"authorization": f"Bearer {api_key}", "content-type": "application/json"},
    ) as client:
        model = MODEL or pick_model(client)
        print(f"model: {model}  (max_tokens={MAX_TOKENS}, {ATTEMPTS * 2} requests)")
        print()

        with_options = [stream_once(client, model, with_options=True) for _ in range(ATTEMPTS)]
        without = [stream_once(client, model, with_options=False) for _ in range(ATTEMPTS)]

    print("=== with stream_options.include_usage ===")
    for index, result in enumerate(with_options):
        describe(f"attempt {index + 1}", result)
    print()
    print("=== without it ===")
    for index, result in enumerate(without):
        describe(f"attempt {index + 1}", result)
    print()

    rejected = any(not r["ok"] for r in with_options)
    usage_without = all(r["usage"] for r in without)
    providers_with = {r["provider"] for r in with_options if r["provider"]}
    providers_without = {r["provider"] for r in without if r["provider"]}

    print("=== conclusion ===")
    if rejected:
        print("  stream_options is REJECTED. Set forward_stream_options = false.")
        return 0
    if not usage_without:
        print("  stream_options is accepted and is REQUIRED — usage is missing without it.")
        print("  Set forward_stream_options = true.")
        return 0

    print("  stream_options is accepted, and usage arrives without it either way.")
    if providers_with and providers_without and providers_with != providers_without:
        # The failure mode with no error attached: the parameter is accepted,
        # but it narrows the routing pool and nothing says so.
        print(
            f"  Providers differ: with={sorted(providers_with)} without={sorted(providers_without)}"
        )
        print("  This is the 'limits the providers able to process them' case.")
        print("  Set forward_stream_options = false — sending it costs routing breadth")
        print("  and buys nothing.")
    else:
        print("  No routing difference observed in this sample. Either setting works;")
        print("  false is still the tidier default, since the parameter is undocumented.")
        print(f"  (Sample is small: {ATTEMPTS} attempts each. Re-run for more confidence.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
