#!/usr/bin/env python3
"""Check that a citation survives redaction (docs/adr/0059-citation-offsets.md).

What only this can prove: the whole chain, with the real detection service
finding a real name, the real placeholder derivation, and a provider that
computes its citation offsets against its own output the way OpenAI does.

The unit suite fakes the detector. Here Presidio finds the name, the gateway
substitutes a placeholder whose length it does not choose, the fake upstream
cites a word *after* that placeholder, and the caller must get back the real
name **and** a citation that still quotes the cited word.

**Needs redaction switched on.** With no active rule nothing is substituted, so
nothing moves and this would pass without testing anything — it says so and
refuses rather than reporting a false success.

Usage:
    docker compose --env-file deploy/.env \\
      -f deploy/compose/docker-compose.yml \\
      -f deploy/compose/docker-compose.smoke.yml \\
      -f deploy/compose/docker-compose.redaction.yml up -d --build
    set -a; . deploy/.env; set +a; python3 scripts/test_citations_live.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from live_session import admin_credentials, check, login, user_credentials
from test_providers_live import api
from test_surfaces_live import call, key_for

FAILURES: list[str] = []

#: The name the detector has to find. Italian, because the deployment's engine
#: is the English model and this is the shape that caught a real bug before.
NAME = "Luca Bianchi"
#: What the fake upstream appends and cites. Must stay in step with `CITED` in
#: scripts/_fake_upstream.py.
CITED = "source"
TOOL = {"type": "web_search_20250305", "name": "web_search"}


def expect(label: str, condition: bool, detail: str = "") -> None:
    if not check(label, condition, detail):
        FAILURES.append(label)


def main() -> int:
    credentials = admin_credentials()
    people = user_credentials()
    if credentials is None or people is None:
        print("set GATEWAY_LOCAL_ADMIN_PASSWORD and GATEWAY_LOCAL_USER_* in deploy/.env")
        return 1
    session = login(*credentials)
    user = login(*people)
    if session is None or user is None:
        print("login failed")
        return 1

    # Refuse to report success against a deployment that redacts nothing.
    status, rules = api(session, "/api/admin/redaction/rules?limit=50")
    active = [r for r in rules.get("items", []) if r.get("is_active")]
    if status != 200 or not active:
        print(
            "No active redaction rule, so nothing would be substituted and this "
            "check cannot mean anything. Enable one and run it again."
        )
        return 1
    print(f"active rules: {[r['name'] for r in active]}")

    secret = key_for(user, "citations-live")
    if secret is None:
        return 1

    _, offered = call(secret, "/v1/models", {}, method="GET")
    names = [m["id"] for m in (offered or {}).get("data", [])]
    status, models = api(session, "/api/admin/models?limit=100")
    model = next(
        (
            m
            for m in models.get("items", [])
            if m["kind"] == "chat" and m["is_active"] and m["name"] in names
        ),
        None,
    )
    if model is None:
        print(f"no chat model this caller may use; offered {names}")
        return 1
    print(f"model: {model['name']}")

    status, body = call(
        secret,
        "/v1/chat/completions",
        {
            "model": model["name"],
            "messages": [{"role": "user", "content": f"Did {NAME} win the match?"}],
            "tools": [TOOL],
        },
    )
    expect("the request is served", status == 200, f"HTTP {status}: {body}")
    if status != 200:
        return 1

    message: dict[str, Any] = body["choices"][0]["message"]
    content = str(message.get("content") or "")
    annotations = message.get("annotations") or []

    expect(
        "the answer came back with the real name, not a placeholder",
        NAME in content and "<PERSON_" not in content,
        f"content={content!r}",
    )
    expect("the provider's citation survived the proxy", bool(annotations), f"{message}")
    if not annotations:
        return 1

    citation = annotations[0].get("url_citation") or {}
    start, end = citation.get("start_index"), citation.get("end_index")
    expect(
        "the citation carries offsets",
        isinstance(start, int) and isinstance(end, int),
        f"citation={citation}",
    )
    if not (isinstance(start, int) and isinstance(end, int)):
        return 1

    quoted = content[start:end]
    expect(
        "the citation still quotes the word it cited",
        quoted == CITED,
        f"it quotes {quoted!r} instead of {CITED!r} — the offsets did not move "
        f"with the text (content={content!r}, span={start}:{end})",
    )

    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
