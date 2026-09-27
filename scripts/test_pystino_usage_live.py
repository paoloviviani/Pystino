#!/usr/bin/env python3
"""``GET /v1/pystino/usage`` against the running stack (ADR 0074).

What the unit suite cannot reach, and why this exists as a live script rather
than more of the same in `apps/gateway/tests/test_pystino_usage.py`:

* **A real API key**, minted through `/api/me/keys` the way a person actually
  gets one, rather than inserted straight into SQLite.
* **A real OIDC access token**, from whatever identity provider this
  deployment is configured against, through the same validation path a
  chat client's bearer request takes — the unit suite mints its own with a
  local key and never exercises discovery or a live JWKS fetch.
* That both credential kinds resolve to a `Principal` whose `user.group_ids()`
  is a real, committed set of `Membership` rows — the unit tests use a
  single-process SQLite file where "effective membership" is easy to get
  right by accident.

The endpoint itself spends nothing — it is a read of `usage_records` and
`limit_rules`, not a metered `/v1` route — so the demo user's EUR 1/hour cap
(CLAUDE.md) does not constrain this file the way it constrains the surface
scripts. Nothing here calls a model.

Usage:
    docker compose --env-file deploy/.env \\
      -f deploy/compose/docker-compose.yml \\
      -f deploy/compose/docker-compose.smoke.yml \\
      -f deploy/compose/docker-compose.redaction.yml up -d --build
    set -a; . deploy/.env; set +a; python3 scripts/test_pystino_usage_live.py

The access-token half additionally needs an identity provider with a direct
grant enabled — `deploy/keycloak/setup.sh` configures one — and
KEYCLOAK_TEST_USER / KEYCLOAK_TEST_PASSWORD for a real person to sign in as.
Without them that half is reported skipped, not failed: the API-key half
alone still proves the route.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from live_session import GATEWAY, admin_credentials, check, login, request, skip

FAILURES: list[str] = []


def expect(label: str, condition: bool, detail: str = "") -> None:
    if not check(label, condition, detail):
        FAILURES.append(label)


def api(opener: Any, path: str, **kwargs: Any) -> tuple[int, Any]:
    status, _, body = request(opener, f"{GATEWAY}{path}", **kwargs)
    try:
        return status, json.loads(body)
    except (ValueError, TypeError):
        return status, body.decode("utf-8", "replace")


def key_for(opener: Any, name: str) -> str | None:
    status, created = api(opener, "/api/me/keys", json_body={"name": name}, method="POST")
    if status != 201:
        expect(f"minted an API key ({name})", False, f"HTTP {status}: {created}")
        return None
    return str(created["secret"])


def bearer_get(credential: str | None, path: str) -> tuple[int, Any]:
    """A `/v1` request straight over HTTP, no cookie jar.

    Not `live_session.request`: that carries a browser session, and the whole
    point of `/v1` is that it authenticates from the bearer alone.
    """
    headers = {"accept": "application/json"}
    if credential is not None:
        headers["authorization"] = f"Bearer {credential}"
    req = urllib.request.Request(f"{GATEWAY}{path}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw.decode("utf-8", "replace")


def access_token() -> str | None:
    """An OIDC access token, minted by password grant.

    Built from ``GATEWAY_OIDC__ISSUER`` / ``__CLIENT_ID`` / ``__CLIENT_SECRET``
    on purpose, not from ``CHAT_OPENID_CONFIG``: that variable is JSON, and
    ``set -a; . deploy/.env; set +a`` strips its quotes, leaving a value no
    JSON parser accepts (the trap CLAUDE.md records under "Sourcing
    deploy/.env mangles CHAT_OPENID_CONFIG"). The three ``GATEWAY_OIDC__*``
    variables are plain strings and survive sourcing unharmed. The gateway's
    own console client works for this because `deploy/keycloak/setup.sh` sets
    ``directAccessGrantsEnabled`` on every client it creates, this one
    included.
    """
    issuer = os.environ.get("GATEWAY_OIDC__ISSUER", "")
    client_id = os.environ.get("GATEWAY_OIDC__CLIENT_ID", "")
    client_secret = os.environ.get("GATEWAY_OIDC__CLIENT_SECRET", "")
    user = os.environ.get("KEYCLOAK_TEST_USER", "")
    password = os.environ.get("KEYCLOAK_TEST_PASSWORD", "")
    if not (issuer and client_id and client_secret and user and password):
        return None

    body = urllib.parse.urlencode(
        {
            "grant_type": "password",
            "client_id": client_id,
            "client_secret": client_secret,
            "username": user,
            "password": password,
            "scope": "openid",
        }
    ).encode()
    req = urllib.request.Request(
        f"{issuer}/protocol/openid-connect/token",
        data=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            token = json.loads(response.read()).get("access_token")
            return str(token) if token else None
    except urllib.error.HTTPError:
        return None


def check_document(label: str, status: int, body: Any) -> None:
    expect(f"{label}: 200", status == 200, f"HTTP {status}: {body if status != 200 else ''}")
    if status != 200 or not isinstance(body, dict):
        return

    expect(
        f"{label}: carries limits, usage and groups",
        {"limits", "usage", "groups"} <= body.keys(),
    )

    usage = body.get("usage", {})
    expect(
        f"{label}: usage is one window with a cost and a currency",
        {"window_seconds", "requests", "total_tokens", "cost", "currency"} <= usage.keys(),
        str(usage),
    )

    for rule in body.get("limits", []):
        expect(
            f"{label}: a limit rule has the MyLimitResponse shape",
            {"id", "name", "scope", "metric", "limit_value", "current_value"} <= rule.keys(),
            str(rule),
        )
        # Money is a string end to end, in this envelope as everywhere else —
        # a float here would mean something upstream parsed it into one.
        expect(
            f"{label}: limit_value is a string",
            isinstance(rule.get("limit_value"), str),
            repr(rule.get("limit_value")),
        )

    for name, summary in body.get("groups", {}).items():
        expect(
            f"{label}: group {name!r} usage has a cost",
            isinstance(summary, dict) and "cost" in summary,
            str(summary),
        )


def main() -> int:
    print(f"gateway: {GATEWAY}\n")

    print("=== no credential at all ===")
    status, body = bearer_get(None, "/v1/pystino/usage")
    expect("refused with no bearer", status == 401, f"HTTP {status}")

    print("\n=== a credential that is not a real key ===")
    status, body = bearer_get("gwk_not-a-real-key", "/v1/pystino/usage")
    expect("a bad key is refused", status == 401, f"HTTP {status}")

    admin = admin_credentials()
    if admin is None:
        skip(
            "the credentialed checks",
            "set PYSTINO_LIVE_ADMIN_PASSWORD (see scripts/live_session.py)",
        )
        print()
        if FAILURES:
            print(f"FAILED: {len(FAILURES)} check(s) — " + "; ".join(FAILURES))
            return 1
        print("OK (partial — only the two refusals were checked)")
        return 0

    print("\n=== an API key reads its own usage ===")
    opener = login(*admin)
    if opener is None:
        return 1
    secret = key_for(opener, "pystino-usage-live-check")
    if secret is not None:
        status, body = bearer_get(secret, "/v1/pystino/usage")
        check_document("api key", status, body)

    print("\n=== an OIDC access token reads its own usage ===")
    token = access_token()
    if token is None:
        skip(
            "the access-token case",
            "set KEYCLOAK_TEST_USER / KEYCLOAK_TEST_PASSWORD, with an OIDC "
            "provider configured (deploy/keycloak/setup.sh)",
        )
    else:
        status, body = bearer_get(token, "/v1/pystino/usage")
        check_document("access token", status, body)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s) — " + "; ".join(FAILURES))
        return 1
    print("OK — a bearer caller, key or token, reads its own quotas and spend as one document.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
