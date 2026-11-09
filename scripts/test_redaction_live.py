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

from live_session import GATEWAY, check, grant_model, user_credentials

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
    from live_session import admin_credentials, login

    credentials = admin_credentials()
    admin = login(*credentials) if credentials else None
    if admin is None:
        print("FAILED: PYSTINO_LIVE_ADMIN_PASSWORD is not set (see scripts/live_session.py)")
        return 1
    try:
        credentials = user_credentials()
        member = login(*credentials) if credentials else None
        if member is not None and not grant_model(admin, member, MODEL):
            expect(f"{MODEL} is catalogued for the member's group", False, "see grant_model")
        if not install_admin_redaction(admin):
            return 1
        return check_redaction()
    finally:
        remove_admin_redaction(admin)


def check_redaction() -> int:
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
    print("=== a user's own policy and pipeline, the test box and the log ===")
    credentials = user_credentials()
    session = login(*credentials) if credentials is not None else None
    if session is None:
        expect("a member session for the user's own policy", False, "no member credentials")
        return
    mine = f"{GATEWAY}/api/me/privacy"
    status, _, body = request(
        session,
        f"{mine}/policies",
        json_body={
            "name": "live-check mine",
            "entries": [
                {"kind": "pattern", "name": "live_project", "regex": "PRJ-[0-9]+", "mode": "redact"}
            ],
        },
        method="POST",
    )
    expect("a user policy is accepted", status == 201, f"HTTP {status}: {body[:200]!r}")
    policy_id = json.loads(body).get("id") if status == 201 else None
    status, _, body = request(
        session,
        f"{mine}/policies",
        json_body={
            "name": "live-check bad",
            "entries": [{"kind": "pattern", "name": "bad", "regex": "(?=x)x", "mode": "redact"}],
        },
        method="POST",
    )
    expect("a pattern RE2 refuses is refused", status in (400, 422), f"HTTP {status}")
    status, _, body = request(
        session,
        f"{mine}/policies",
        json_body={
            "name": "live-check off",
            "entries": [{"kind": "mode", "name": "PERSON", "mode": "off"}],
        },
        method="POST",
    )
    expect("a user policy cannot switch a type off", status in (400, 422), f"HTTP {status}")
    if policy_id is None:
        return
    pipeline_id = None
    try:
        status, _, body = request(
            session,
            f"{mine}/pipelines",
            json_body={
                "kind": "chat",
                "name": "live-check mine",
                "rules": [],
                "otherwise": {"level": "pass", "redact_with": policy_id},
            },
            method="POST",
        )
        expect("a user chat pipeline applies it", status == 201, f"HTTP {status}: {body[:200]!r}")
        pipeline_id = json.loads(body).get("id") if status == 201 else None
        status, _, body = request(
            session,
            f"{mine}/test",
            json_body={"kind": "chat", "text": "ticket PRJ-42"},
            method="POST",
        )
        result = json.loads(body) if status == 200 else {}
        expect("the test box answers", status == 200, f"HTTP {status}: {body[:200]!r}")
        expect(
            "the test box says the user's policy redacts",
            result.get("outcome") == "redact"
            and "live-check mine" in json.dumps(result.get("policies")),
            str(result)[:300],
        )
        expect("the test box does not echo the text", "PRJ-42" not in str(body))

        import time

        time.sleep(12)  # the other workers learn on the resolver's ten-second poll
        post(
            f"{GATEWAY}/v1/chat/completions",
            {
                "model": MODEL,
                "messages": [{"role": "user", "content": "see PRJ-42"}],
                "stream": False,
            },
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
        expect(
            "the log holds no value", "luca.bianchi" not in str(body) and "PRJ-42" not in str(body)
        )
    finally:
        if pipeline_id:
            request(session, f"{mine}/pipelines/{pipeline_id}", method="DELETE")
        request(session, f"{mine}/policies/{policy_id}", method="DELETE")


ADMIN_POLICY = "live-check redaction"
ADMIN_FORMATS = ("CREDIT_CARD", "IBAN_CODE", "EMAIL_ADDRESS", "IT_VAT_CODE", "PHONE_NUMBER")


def install_admin_redaction(admin: Any, formats: tuple[str, ...] = ADMIN_FORMATS) -> bool:
    """Write the administrator's policy and chat pipeline this check relies on.

    The shipped default protects nothing (ADR 0113): a fresh stack redacts nothing
    until an administrator writes a policy and a pipeline that applies it. So the
    check writes its own, and :func:`remove_admin_redaction` takes it away again.
    """
    from test_providers_live import api

    remove_admin_redaction(admin)
    status, policy = api(
        admin,
        "/api/admin/privacy/policies",
        json_body={
            "name": ADMIN_POLICY,
            "policy": {"formats": {name: {"mode": "anonymise_restore"} for name in formats}},
            "shared_with_users": True,
        },
        method="POST",
    )
    expect("an administrator's policy saves", status == 201, f"HTTP {status}: {policy}")
    if status != 201:
        return False
    status, pipe = api(
        admin,
        "/api/admin/privacy/pipelines",
        json_body={
            "kind": "chat",
            "name": ADMIN_POLICY,
            "rules": [],
            "otherwise": {"level": "pass", "redact_with": policy["id"]},
        },
        method="POST",
    )
    expect("a chat pipeline on everything applies it", status == 201, f"HTTP {status}: {pipe}")
    return status == 201


def remove_admin_redaction(admin: Any) -> None:
    from test_providers_live import api

    for item in api(admin, "/api/admin/privacy/pipelines?limit=200")[1].get("items", []):
        if item["name"] == ADMIN_POLICY:
            api(admin, f"/api/admin/privacy/pipelines/{item['id']}", method="DELETE")
    for item in api(admin, "/api/admin/privacy/policies?limit=200")[1].get("items", []):
        if item["name"] == ADMIN_POLICY:
            api(admin, f"/api/admin/privacy/policies/{item['id']}", method="DELETE")


def _placeholders(text: str) -> set[str]:
    import re

    return set(re.findall(r"<[A-Z][A-Z0-9_]*_[A-Z2-7]{4,32}>", text))


if __name__ == "__main__":
    raise SystemExit(main())
