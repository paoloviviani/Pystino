#!/usr/bin/env python3
"""Drive redaction end to end against the running stack.

What only this can prove: that Presidio, with a real model, finds what the design
assumed it would — in particular that Italian identifiers are detected in the
MIT-only image, which is the finding the licence decision rests on
(docs/adr/0026-pluggable-detection.md).

It also checks the property that matters most in the request path and cannot be
seen from inside the gateway's tests: **the upstream never receives the PII.** The
fake upstream records what it was sent, so this asserts against the actual bytes
that left the process.

Usage:
    docker compose --env-file deploy/.env \\
      -f deploy/compose/docker-compose.yml \\
      -f deploy/compose/docker-compose.smoke.yml \\
      -f deploy/compose/docker-compose.redaction.yml up -d --build
    python3 scripts/test_redaction_live.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from live_session import GATEWAY, check, user_credentials

UPSTREAM = "http://localhost:8081"
# Seeded by `gateway seed` in the smoke stack; overridable for another deployment.
MODEL = os.getenv("REDACTION_CHECK_MODEL", "smoke-model")
FAILURES: list[str] = []


def expect(label: str, condition: bool, detail: str = "") -> None:
    if not check(label, condition, detail):
        FAILURES.append(label)


def post(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        body = error.read()
        try:
            return error.code, json.loads(body)
        except ValueError:
            return error.code, body.decode("utf-8", "replace")


def get(url: str, headers: dict[str, str] | None = None) -> Any:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


def mint_key() -> str | None:
    """An API key for a member, via local password sign-in like the other scripts."""
    from live_session import login, request

    credentials = user_credentials()
    if credentials is None:
        print(
            "  skipped: GATEWAY_LOCAL_USER_EMAIL/PASSWORD are not set — a redacted "
            "request needs a member of a billing group, which the admin is not"
        )
        return None
    alice = login(*credentials)
    if alice is None:
        return None
    status, _, body = request(
        alice, f"{GATEWAY}/api/me/keys", json_body={"name": "redaction-check"}, method="POST"
    )
    if status != 201:
        expect("minted an API key", False, f"HTTP {status}")
        return None
    secret: str = json.loads(body)["secret"]
    return secret


# Text with entities Presidio finds without any NER model. Fictional throughout.
PATTERN_PII = (
    "Please invoice VAT number 00743110157, card 4111 1111 1111 1111, "
    "IBAN IT60X0542811101000000123456, email luca.bianchi@example.org, "
    "phone +39 011 227 6543."
)


def main() -> int:
    print("=== the detection service is up and says what it can do ===")
    status, _health = get(f"{GATEWAY}/healthz")
    expect("gateway is up", status == 200, f"HTTP {status}")

    key = mint_key()
    if key is None:
        return 1
    auth = {"authorization": f"Bearer {key}"}

    print()
    print("=== the upstream never sees the PII ===")
    status, completion = post(
        f"{GATEWAY}/v1/chat/completions",
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": PATTERN_PII}],
            "stream": False,
        },
        auth,
    )
    expect("a completion succeeds with redaction on", status == 200, f"HTTP {status}: {completion}")

    # The fake upstream echoes the prompt it received, which is what makes this
    # checkable from outside: these are the bytes that actually left the gateway.
    status, seen = get(f"{UPSTREAM}/_last_request")
    if status != 200:
        expect(
            "the fake upstream exposes what it received",
            False,
            "start the stack with docker-compose.smoke.yml",
        )
    else:
        sent = json.dumps(seen)
        for label, needle in (
            ("VAT number", "00743110157"),
            ("card number", "4111"),
            ("IBAN", "IT60X0542811101000000123456"),
            ("email", "luca.bianchi@example.org"),
        ):
            expect(f"the {label} did not reach the upstream", needle not in sent)
        found = _placeholders(sent)
        expect(
            "placeholders did reach the upstream",
            bool(found),
            "" if found else "nothing was substituted — is the engine really 'http'?",
        )

    print()
    print("=== and the caller gets the real values back ===")
    # The fake upstream echoes the prompt, so its reply carries the placeholders.
    # Restoring them is the response half of the round trip.
    reply = ""
    if isinstance(completion, dict):
        reply = (completion.get("choices") or [{}])[0].get("message", {}).get("content", "")
    expect(
        "the response was restored for the caller",
        "luca.bianchi@example.org" in reply,
        reply[:200],
    )
    expect("no placeholder leaked to the caller", not _placeholders(reply), reply[:200])

    print()
    print("=== the same entity gets the same placeholder in a later turn ===")
    post(
        f"{GATEWAY}/v1/chat/completions",
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": "Write to luca.bianchi@example.org"}],
            "stream": False,
        },
        auth,
    )
    _, first_seen = get(f"{UPSTREAM}/_last_request")
    post(
        f"{GATEWAY}/v1/chat/completions",
        {
            "model": MODEL,
            "messages": [
                {"role": "user", "content": "Write to luca.bianchi@example.org"},
                {"role": "assistant", "content": "Done."},
                {"role": "user", "content": "Remind me who luca.bianchi@example.org is"},
            ],
            "stream": False,
        },
        auth,
    )
    _, second_seen = get(f"{UPSTREAM}/_last_request")
    placeholders_first = _placeholders(json.dumps(first_seen))
    placeholders_second = _placeholders(json.dumps(second_seen))
    expect(
        "the placeholder is stable across turns",
        bool(placeholders_first) and placeholders_first <= placeholders_second,
        f"{placeholders_first} then {placeholders_second}",
    )

    print()
    print("=== streaming ===")
    request = urllib.request.Request(
        f"{GATEWAY}/v1/chat/completions",
        data=json.dumps(
            {
                "model": MODEL,
                "messages": [{"role": "user", "content": PATTERN_PII}],
                "stream": True,
            }
        ).encode(),
        headers={"content-type": "application/json", **auth},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode()
    except urllib.error.HTTPError as error:
        # The demo stack bills a million tokens per fake completion, so a run can
        # legitimately exhaust a seeded quota. Reported rather than crashed, and
        # distinguished from a redaction failure, which is what this checks.
        body = error.read().decode("utf-8", "replace")
        expect(
            "a streamed completion succeeds",
            False,
            f"HTTP {error.code}: {body[:160]}"
            + ("  (a quota is exhausted, not a redaction failure)" if error.code == 429 else ""),
        )
        return 1 if FAILURES else 0
    expect("a streamed completion succeeds", "data:" in body, body[:200])
    expect(
        "the streamed response was restored across frame boundaries",
        "luca.bianchi@example.org" in body,
        "the upstream echoes the prompt, so the reply carries placeholders to restore",
    )
    expect("no placeholder leaked into the stream", not _placeholders(body), body[:300])
    _, streamed_seen = get(f"{UPSTREAM}/_last_request")
    expect(
        "the streamed request was redacted too",
        "00743110157" not in json.dumps(streamed_seen),
    )

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("all checks passed")
    return 0


def _placeholders(text: str) -> set[str]:
    import re

    return set(re.findall(r"<[A-Z][A-Z0-9_]*_[A-Z2-7]{4,32}>", text))


if __name__ == "__main__":
    raise SystemExit(main())
