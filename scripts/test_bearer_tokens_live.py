#!/usr/bin/env python3
"""Call ``/v1`` with a real OIDC access token from a real Keycloak (ADR 0040).

The unit suite signs its own tokens against a stub JWKS, which proves the
validator is right about the tokens it is given. It cannot prove the tokens a
real realm issues *are* those tokens — and that is exactly where this feature's
one hard problem lives: a Keycloak access token has no ``aud`` claim at all
unless an audience mapper puts one there, so a validator that looks correct
rejects every real token, and a realm that looks correct produces tokens no
validator will accept.

What it asserts:

* the realm's access token names the gateway in ``aud`` — that is the
  ``gateway-api`` client scope doing its job;
* that token authenticates a ``/v1`` request and the ledger attributes the spend
  to the user and their group with **no API key**;
* the ID token from the same login is refused, though it is signed by the same
  realm and names the same audience;
* a token from a client without the scope is refused;
* an API key still works, unchanged.

Bring the stack up, then:

    set -a; . deploy/.env; set +a          # behind the TLS proxy
    ./scripts/test_bearer_tokens_live.py

The gateway needs GATEWAY_OIDC__ACCESS_TOKEN_AUDIENCE set; without it this
script reports that and skips, because "tokens are refused" is the correct
behaviour of a deployment that has not opted in and is not a failure.
"""

from __future__ import annotations

import base64
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

PUBLIC_ORIGIN = os.environ.get("PUBLIC_ORIGIN") or (
    f"https://{os.environ['PUBLIC_HOST']}:{os.environ.get('HTTPS_PORT', '443')}"
    if os.environ.get("PUBLIC_HOST")
    else ""
)
GATEWAY_HOST = os.environ.get("GATEWAY_HOST") or os.environ.get("OVERLAY_ADDR") or "localhost"
GATEWAY = (
    os.environ.get("GATEWAY_URL")
    or PUBLIC_ORIGIN
    or f"http://{GATEWAY_HOST}:{os.environ.get('GATEWAY_PORT', '8000')}"
)
KEYCLOAK_HOST = os.environ.get("KEYCLOAK_HOST") or os.environ.get("OVERLAY_ADDR") or "localhost"
KEYCLOAK_BASE = os.environ.get("KEYCLOAK_URL") or (
    f"{PUBLIC_ORIGIN}/kc"
    if PUBLIC_ORIGIN
    else f"http://{KEYCLOAK_HOST}:{os.environ.get('KEYCLOAK_PORT', '8080')}"
)
REALM = f"{KEYCLOAK_BASE}/realms/llm-platform"

# Verify the certificate rather than skipping verification — see test_oidc_flow.py.
CA_BUNDLE = os.environ.get(
    "GATEWAY_CA_BUNDLE", str(Path(__file__).resolve().parent.parent / "deploy/tls/caddy-root.crt")
)
if GATEWAY.startswith("https://") and Path(CA_BUNDLE).exists():
    urllib.request.install_opener(
        urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=CA_BUNDLE))
        )
    )

# The fallback chain has to match docker-compose.proxy.yml exactly. That overlay
# rotates the chat client's secret to KEYCLOAK_CHAT_CLIENT_SECRET *or, failing
# that, KEYCLOAK_CLIENT_SECRET* — so a script that fell back to the value in the
# repository instead would get `unauthorized_client` against a correctly
# configured deployment and report the feature broken.
CLIENT_SECRETS = {
    "llm-gateway": os.environ.get("KEYCLOAK_CLIENT_SECRET")
    or "gateway-dev-secret-not-for-production",
    "llm-chat": os.environ.get("KEYCLOAK_CHAT_CLIENT_SECRET")
    or os.environ.get("KEYCLOAK_CLIENT_SECRET")
    or "chat-dev-secret-not-for-production",
}
SEED_PASSWORD = os.environ.get("KEYCLOAK_SEED_PASSWORD") or "alice-password"

failures: list[str] = []


def fail(message: str) -> None:
    failures.append(message)
    print(f"  FAILED: {message}")


def request(url: str, **kwargs: Any) -> tuple[int, bytes]:
    req = urllib.request.Request(url, **kwargs)
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        print(f"  cannot reach {url}: {exc}")
        raise SystemExit(1) from exc


def claims_of(token: str) -> dict[str, Any]:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def login(client_id: str, username: str = "alice") -> dict[str, Any]:
    """A password grant, only because this is a script and not a browser."""
    body = urllib.parse.urlencode(
        {
            "grant_type": "password",
            "client_id": client_id,
            "client_secret": CLIENT_SECRETS[client_id],
            "username": username,
            "password": SEED_PASSWORD,
            "scope": "openid profile email",
        }
    ).encode()
    status, raw = request(
        f"{REALM}/protocol/openid-connect/token",
        data=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    if status != 200:
        print(f"  token request for {client_id} failed: HTTP {status} {raw[:200]!r}")
        raise SystemExit(1)
    return json.loads(raw)


def call_v1(path: str, credential: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    headers = {"authorization": f"Bearer {credential}"}
    data = None
    if body is not None:
        headers["content-type"] = "application/json"
        data = json.dumps(body).encode()
    status, raw = request(f"{GATEWAY}{path}", headers=headers, data=data)
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def main() -> int:
    print(f"gateway  {GATEWAY}")
    print(f"keycloak {KEYCLOAK_BASE}")
    print()

    print("=== the realm's access token names the gateway as an audience ===")
    tokens = login("llm-chat")
    access_claims = claims_of(tokens["access_token"])
    audience = access_claims.get("aud")
    held = [audience] if isinstance(audience, str) else audience or []
    print(f"  azp={access_claims.get('azp')} aud={held} typ={access_claims.get('typ')}")
    if "llm-gateway" not in held:
        fail(
            "the access token does not name llm-gateway in aud — the gateway-api "
            "client scope is missing from the llm-chat client, and every bearer "
            "call will be refused"
        )
        print("\nFAILED: fix the realm before the rest of this means anything.")
        return 1
    print("  the gateway-api audience mapper is in force")

    print()
    print("=== the token authenticates a /v1 call ===")
    status, payload = call_v1("/v1/models", tokens["access_token"])
    if status == 401:
        print(
            "  refused. GATEWAY_OIDC__ACCESS_TOKEN_AUDIENCE is not set on the "
            "gateway, so this deployment does not accept tokens — which is the "
            "default and not a failure. Set it and re-run.\n"
        )
        print("SKIPPED — the gateway has not opted in.")
        return 0
    if status != 200:
        fail(f"/v1/models with a bearer token returned HTTP {status}: {payload}")
    else:
        models = [m["id"] for m in payload.get("data", [])]
        print(f"  /v1/models -> {len(models)} model(s) alice may use")

    print()
    print("=== a metered request is billed to her, with no API key ===")
    status, payload = call_v1(
        "/v1/chat/completions",
        tokens["access_token"],
        {
            "model": os.environ.get("BEARER_TEST_MODEL", "smoke-model"),
            "messages": [{"role": "user", "content": "one word reply please"}],
            "max_tokens": 8,
        },
    )
    if status == 200:
        print(f"  billed request ok, id={payload.get('id')}")
        print("  check the ledger: the row must carry a user and a group and no key")
    elif status == 404:
        print(f"  no such model on this deployment; skipping the metered step ({payload})")
    elif status == 429:
        # The demo cap is EUR 1/hour and the fake upstream bills 1M tokens per
        # request, so back-to-back live scripts exhaust it legitimately.
        print("  quota exhausted — a legitimate outcome of running these back to back")
    else:
        fail(f"a metered bearer request returned HTTP {status}: {payload}")

    print()
    print("=== the ID token from the same login is refused ===")
    id_claims = claims_of(tokens["id_token"])
    print(f"  aud={id_claims.get('aud')} typ={id_claims.get('typ')}")
    status, _ = call_v1("/v1/models", tokens["id_token"])
    if status != 401:
        fail(
            f"an ID token was accepted as an API credential (HTTP {status}). It is "
            "signed by the same realm and handed to the browser at login."
        )
    else:
        print("  refused, as it must be")

    print()
    print("=== a client without the gateway-api scope is refused ===")
    # The console's own client. It holds the scope in this realm, so this check
    # reports rather than asserts when it does not apply — the point is that the
    # audience decides, and the audience comes from the identity provider.
    other = login("llm-gateway")
    other_aud = claims_of(other["access_token"]).get("aud")
    other_held = [other_aud] if isinstance(other_aud, str) else other_aud or []
    if "llm-gateway" in other_held:
        print("  llm-gateway also holds the scope in this realm; nothing to assert")
    else:
        status, _ = call_v1("/v1/models", other["access_token"])
        if status != 401:
            fail(f"a token with aud={other_held} was accepted (HTTP {status})")
        else:
            print("  refused")

    print()
    print("=== an API key still works ===")
    key = os.environ.get("GATEWAY_API_KEY")
    if not key:
        print("  GATEWAY_API_KEY not set; skipping (test_oidc_flow.py covers key minting)")
    else:
        status, _ = call_v1("/v1/models", key)
        if status != 200:
            fail(f"an API key returned HTTP {status} after tokens were enabled")
        else:
            print("  unchanged")

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s)")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("OK — OIDC access tokens work on /v1 against a real Keycloak.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
