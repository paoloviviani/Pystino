#!/usr/bin/env python3
"""Drive redaction end to end against the running stack.

What only this can prove: that Presidio, with a real model, finds what the design
assumed it would — in particular that Italian identifiers are detected in the
MIT-only image, which is the finding the licence decision rests on
(ADR 0026).

It also checks the property that matters most in the request path and cannot be
seen from inside the gateway's tests: **the upstream never receives the PII.** The
fake upstream records what it was sent, so this asserts against the actual bytes
that left the process.

Usage:
    docker compose --env-file deploy/.env \\
      -f deploy/compose.yaml -f deploy/dev/smoke.yml up -d --wait
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
    """An API key for a member, via an OIDC sign-in like the other scripts."""
    from live_session import login, request

    credentials = user_credentials()
    if credentials is None:
        print(
            "  skipped: PYSTINO_LIVE_USER/_PASSWORD are not set — a redacted "
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
            "start the stack with deploy/dev/smoke.yml and the redaction profile",
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

    visibility(auth)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("all checks passed")
    return 0


def visibility(auth: dict[str, str]) -> None:
    """What the user can see (ADR 0112): the summary, their own rule, the log."""
    from live_session import login, request

    print()
    print("=== the per-response summary is opt-in, and never carries values for a key ===")
    messages = [{"role": "user", "content": "Write to luca.bianchi@example.org"}]
    payload = {"model": MODEL, "messages": messages, "stream": False}
    _, plain = post(f"{GATEWAY}/v1/chat/completions", payload, auth)
    expect(
        "without the header the body has no summary",
        isinstance(plain, dict) and "pystino_redaction" not in plain,
    )
    status, opted = post(
        f"{GATEWAY}/v1/chat/completions",
        payload,
        {**auth, "x-pystino-redaction-summary": "1", "x-pystino-redaction-values": "1"},
    )
    summary = opted.get("pystino_redaction") if isinstance(opted, dict) else None
    expect("with the header the body carries the summary", status == 200 and bool(summary))
    if summary:
        expect("the summary says redaction applied", summary.get("applied") is True, str(summary))
        expect("an API key gets no values", "values" not in summary, str(summary))
        expect(
            "the summary names the type and the rule",
            any(e.get("type") == "EMAIL_ADDRESS" and e.get("rule") for e in summary["entities"]),
            str(summary),
        )
        expect("the summary quotes no value", "luca.bianchi" not in json.dumps(summary))

    print()
    print("=== a user's own rule, the test box and the log ===")
    credentials = user_credentials()
    session = login(*credentials) if credentials is not None else None
    if session is None:
        expect("a member session for the user's own rules", False, "no member credentials")
        return
    status, _, body = request(
        session,
        f"{GATEWAY}/api/me/redaction/rules",
        json_body={
            "kind": "pattern",
            "name": "live_project",
            "regex": "PRJ-[0-9]+",
            "mode": "redact",
        },
        method="POST",
    )
    expect("a user pattern is accepted", status == 201, f"HTTP {status}: {body[:200]!r}")
    status, _, body = request(
        session,
        f"{GATEWAY}/api/me/redaction/rules",
        json_body={"kind": "pattern", "name": "bad", "regex": "(?=x)x", "mode": "redact"},
        method="POST",
    )
    expect("a pattern RE2 refuses is refused", status == 400, f"HTTP {status}")
    status, _, body = request(
        session,
        f"{GATEWAY}/api/me/redaction/rules",
        json_body={"kind": "mode", "name": "PERSON", "mode": "off"},
        method="POST",
    )
    expect("a rule cannot be off", status == 400, f"HTTP {status}")
    status, _, body = request(
        session,
        f"{GATEWAY}/api/me/redaction/test",
        json_body={"text": "ticket PRJ-42"},
        method="POST",
    )
    result = json.loads(body) if status == 200 else {}
    expect(
        "the test box finds the user's pattern and names the rule",
        any(
            span.get("entity_type") == "LIVE_PROJECT"
            and (span.get("rule") or {}).get("owner") == "user"
            for span in result.get("spans", [])
        ),
        body[:300] if isinstance(body, str) else str(body)[:300],
    )
    expect("the test box does not echo the text", "PRJ-42" not in str(body))

    import time

    time.sleep(12)  # the other workers learn on the resolver's ten-second poll
    _status, _redacted = post(
        f"{GATEWAY}/v1/chat/completions",
        {"model": MODEL, "messages": [{"role": "user", "content": "see PRJ-42"}], "stream": False},
        auth,
    )
    _, seen = get(f"{UPSTREAM}/_last_request")
    expect(
        "the user's pattern redacts their request",
        "PRJ-42" not in json.dumps(seen),
        str(seen)[:200],
    )
    status, _, body = request(session, f"{GATEWAY}/api/me/redaction/events", method="GET")
    events = json.loads(body) if status == 200 else {}
    expect("the log lists requests where something was hidden", events.get("total", 0) >= 1)
    expect("the log holds no value", "luca.bianchi" not in str(body) and "PRJ-42" not in str(body))


def _placeholders(text: str) -> set[str]:
    import re

    return set(re.findall(r"<[A-Z][A-Z0-9_]*_[A-Z2-7]{4,32}>", text))


if __name__ == "__main__":
    raise SystemExit(main())
