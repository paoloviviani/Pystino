#!/usr/bin/env python
"""Drive the whole OIDC login flow against a real Keycloak and check the result.

The unit suite covers everything on our side of the redirect — claim resolution,
group normalisation, provisioning, membership reconciliation, session tokens. What
it cannot cover is the redirect round trip itself, because that needs a real
identity provider. This does.

Bring the stack up first:

    docker compose --env-file deploy/.env \\
      -f deploy/compose/docker-compose.yml \\
      -f deploy/compose/docker-compose.smoke.yml \\
      -f deploy/compose/docker-compose.keycloak.yml up -d --build

    ./scripts/test_oidc_flow.py

What it asserts, beyond "login works":

* the discovery document exposes ``device_authorization_endpoint``, which the
  Phase 4 ``opencode`` bootstrap depends on;
* **alice** has exactly one group, so the gateway adopts it as her default billing
  group and she can spend immediately;
* **bob** has two, so he gets no default and must choose — and choosing a group he
  is not a member of is refused;
* **carol** has none, so she can sign in but cannot bill anything;
* a session can mint an API key, and that key then works on ``/v1``, which closes
  the loop from human login to billed request.

Everything runs over plain HTTP against localhost, so it is development-only.
"""

from __future__ import annotations

import html
import http.cookiejar
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

GATEWAY = "http://localhost:8000"
# Keycloak's *frontend* URL is pinned to the public name and its backchannel is
# left dynamic (see docker-compose.keycloak.yml), so browser-facing URLs already
# point at localhost and this rewrite is a no-op. It is kept only so the script
# still works if someone pins the frontend to the compose-internal name instead.
KEYCLOAK_INTERNAL = "keycloak:8080"
KEYCLOAK_EXTERNAL = "localhost:8080"

PASSWORDS = {
    "alice": "alice-password",
    "bob": "bob-password",
    "carol": "carol-password",
    "dave": "dave-password",
    "erin": "erin-password",
}

failures: list[str] = []


def reachable(url: str) -> str:
    return url.replace(KEYCLOAK_INTERNAL, KEYCLOAK_EXTERNAL)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stop at redirects so each hop can be inspected."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class LocalhostSecureCookiePolicy(http.cookiejar.DefaultCookiePolicy):
    """Send ``Secure`` cookies to ``http://localhost``, as browsers do.

    Keycloak marks ``KC_RESTART`` and friends ``Secure; SameSite=None`` — it has to,
    because the cookie spec requires Secure whenever SameSite is None. Browsers
    treat ``http://localhost`` as a *secure context* and send such cookies anyway;
    ``http.cookiejar`` implements no such exception, so it silently withholds them
    and Keycloak answers "Restart login cookie not found".

    That is a limitation of this test client, not of the gateway or of Keycloak, so
    it is corrected here rather than by weakening anything in the stack.
    """

    def return_ok_secure(self, cookie: http.cookiejar.Cookie, request: Any) -> bool:
        host = urllib.parse.urlsplit(request.full_url).hostname or ""
        if host in {"localhost", "127.0.0.1", "::1"}:
            return True
        return bool(super().return_ok_secure(cookie, request))


def new_session() -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar(policy=LocalhostSecureCookiePolicy())
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect)


def request(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    data: dict[str, str] | None = None,
    method: str | None = None,
    json_body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    body: bytes | None = None
    headers = {"accept": "application/json, text/html"}
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers["content-type"] = "application/x-www-form-urlencoded"
    elif json_body is not None:
        body = json.dumps(json_body).encode()
        headers["content-type"] = "application/json"

    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    def lower(message: Any) -> dict[str, str]:
        # HTTP header names are case-insensitive and servers disagree: Starlette
        # sends `location`, Keycloak sends `Location`. Normalise rather than guess.
        return {key.lower(): value for key, value in message.items()}

    try:
        with opener.open(req, timeout=30) as response:
            return response.status, lower(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, lower(exc.headers), exc.read()


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)
    return condition


def login(username: str) -> urllib.request.OpenerDirector | None:
    """Complete the authorization-code flow and return an authenticated session."""
    opener = new_session()

    status, headers, _ = request(opener, f"{GATEWAY}/auth/login")
    if status != 302:
        check(f"{username}: /auth/login redirects", False, f"got HTTP {status}")
        return None
    authorize_url = headers["location"]
    if "code_challenge=" not in authorize_url or "S256" not in authorize_url:
        check(f"{username}: authorize URL carries PKCE S256", False, authorize_url[:120])
        return None

    # Keycloak's login page.
    status, _, page = request(opener, reachable(authorize_url))
    if status != 200:
        check(f"{username}: Keycloak login page", False, f"HTTP {status}")
        return None
    match = re.search(r'<form[^>]+id="kc-form-login"[^>]+action="([^"]+)"', page.decode())
    if not match:
        match = re.search(
            r'action="(https?://[^"]*login-actions/authenticate[^"]*)"', page.decode()
        )
    if not match:
        check(f"{username}: found the login form", False, "no form action in the page")
        return None
    form_action = reachable(html.unescape(match.group(1)))

    # Submit credentials.
    status, headers, body = request(
        opener,
        form_action,
        data={"username": username, "password": PASSWORDS[username], "credentialId": ""},
    )
    if status != 302:
        snippet = re.sub(r"\s+", " ", body.decode("utf-8", "replace"))[:160]
        check(f"{username}: credentials accepted", False, f"HTTP {status}: {snippet}")
        return None
    callback_url = headers["location"]
    if "code=" not in callback_url:
        check(f"{username}: authorization code returned", False, callback_url[:140])
        return None

    # Hand the code back to the gateway.
    status, headers, body = request(opener, callback_url)
    if status != 200:
        check(
            f"{username}: gateway accepted the callback",
            False,
            f"HTTP {status}: {body.decode('utf-8', 'replace')[:200]}",
        )
        return None

    payload = json.loads(body)
    print(f"  logged in as {username}: groups={payload.get('groups')}")
    return opener


def me(opener: urllib.request.OpenerDirector) -> dict[str, Any]:
    status, _, body = request(opener, f"{GATEWAY}/api/me")
    if status != 200:
        raise AssertionError(f"/api/me returned {status}: {body[:200]!r}")
    result: dict[str, Any] = json.loads(body)
    return result


def main() -> int:
    print("=== discovery ===")
    opener = new_session()
    status, _, body = request(
        opener,
        f"http://{KEYCLOAK_EXTERNAL}/realms/llm-platform/.well-known/openid-configuration",
    )
    if status != 200:
        print(f"  Keycloak is not reachable (HTTP {status}). Is the stack up?")
        return 2
    discovery = json.loads(body)
    # The issuer must be the *browser-reachable* name. Pinning it to the
    # compose-internal name makes token validation work while sending real browsers
    # somewhere they cannot resolve.
    check(
        "issuer is the public, browser-reachable hostname",
        KEYCLOAK_EXTERNAL in discovery["issuer"],
        discovery["issuer"],
    )
    check(
        "the authorization endpoint is browser-reachable",
        KEYCLOAK_INTERNAL not in discovery["authorization_endpoint"],
        discovery["authorization_endpoint"],
    )
    check(
        "device_authorization_endpoint present (Phase 4 opencode bootstrap)",
        bool(discovery.get("device_authorization_endpoint")),
    )
    check("S256 supported", "S256" in (discovery.get("code_challenge_methods_supported") or []))

    print()
    print("=== alice: one group, so the gateway adopts it as her default ===")
    alice = login("alice")
    if alice is None:
        return 1
    profile = me(alice)
    check(
        "groups came through the claim",
        [g["name"] for g in profile["groups"]] == ["research"],
        str([g["name"] for g in profile["groups"]]),
    )
    check(
        "default billing group adopted automatically",
        (profile["default_billing_group"] or {}).get("name") == "research",
        str(profile["default_billing_group"]),
    )
    check("email mapped", profile["email"] == "alice@example.org", str(profile["email"]))

    print()
    print("=== alice mints an API key and uses it on /v1 ===")
    status, _, body = request(
        alice, f"{GATEWAY}/api/me/keys", json_body={"name": "oidc-test"}, method="POST"
    )
    if not check("key minted", status == 201, f"HTTP {status}: {body[:160]!r}"):
        return 1
    minted = json.loads(body)
    secret = minted["secret"]
    check("secret returned exactly once", bool(secret) and secret.startswith("gwk_"))

    key_session = new_session()
    status, _, body = request(key_session, f"{GATEWAY}/v1/models")
    check("the key is required on /v1", status == 401, f"HTTP {status}")

    req = urllib.request.Request(
        f"{GATEWAY}/v1/models", headers={"authorization": f"Bearer {secret}"}
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        available = json.load(response)["data"]
    check("her key can list models", response.status == 200, f"{len(available)} model(s)")

    if available:
        model = available[0]["id"]
        chat = urllib.request.Request(
            f"{GATEWAY}/v1/chat/completions",
            data=json.dumps(
                {"model": model, "messages": [{"role": "user", "content": "hello from OIDC"}]}
            ).encode(),
            headers={
                "authorization": f"Bearer {secret}",
                "content-type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(chat, timeout=60) as response:
                completion = json.load(response)
            usage = completion.get("usage") or {}
            check(
                "a completion is billed to her OIDC group",
                completion.get("model") == model,
                f"model={completion.get('model')} tokens={usage.get('total_tokens')}",
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:160]
            # 429 is a correct outcome if the group's budget is already spent.
            check(
                "a completion is billed to her OIDC group",
                exc.code == 429,
                f"HTTP {exc.code}: {detail}",
            )
    else:
        print("  (no model is granted to her group, so the chat step is skipped)")

    print()
    # bob is never mutated, so this assertion holds on a re-run. erin below is the
    # mutation target. Keeping them separate matters: an earlier version used bob
    # for both and failed on the second run, because the gateway had correctly
    # *preserved* the default he had explicitly chosen the first time.
    print("=== bob: two groups, so he must choose ===")
    bob = login("bob")
    if bob is None:
        return 1
    profile = me(bob)
    check(
        "both groups present",
        sorted(g["name"] for g in profile["groups"]) == ["finance", "research"],
        str(sorted(g["name"] for g in profile["groups"])),
    )
    check(
        "no default billing group was guessed",
        profile["default_billing_group"] is None,
        str(profile["default_billing_group"]),
    )

    print()
    print("=== erin: two groups, and she changes her own billing group ===")
    erin = login("erin")
    if erin is None:
        return 1
    profile = me(erin)
    finance = next(g for g in profile["groups"] if g["name"] == "finance")
    status, _, body = request(
        erin,
        f"{GATEWAY}/api/me/default-billing-group",
        json_body={"group_id": finance["id"]},
        method="PUT",
    )
    check("she can set her own default", status == 200, f"HTTP {status}")
    if status == 200:
        check(
            "the choice stuck",
            (json.loads(body)["default_billing_group"] or {}).get("name") == "finance",
        )

    status, _, body = request(
        erin,
        f"{GATEWAY}/api/me/default-billing-group",
        json_body={"group_id": "00000000-0000-0000-0000-000000000000"},
        method="PUT",
    )
    check("a group she is not in is refused", status == 403, f"HTTP {status}")

    # Log in again: an explicit choice must survive re-authentication rather than
    # being reset by provisioning.
    erin_again = login("erin")
    if erin_again is not None:
        check(
            "her explicit choice survives a re-login",
            (me(erin_again)["default_billing_group"] or {}).get("name") == "finance",
        )

    print()
    print("=== carol: no groups, so she can sign in but cannot bill ===")
    carol = login("carol")
    if carol is None:
        return 1
    profile = me(carol)
    check("no groups", profile["groups"] == [], str(profile["groups"]))
    check("no default billing group", profile["default_billing_group"] is None)
    status, _, body = request(
        carol, f"{GATEWAY}/api/me/keys", json_body={"name": "should-fail"}, method="POST"
    )
    check(
        "minting a key without a billing group is refused with a reason",
        status == 400,
        f"HTTP {status}: {json.loads(body).get('error', {}).get('message', '')[:90]}",
    )

    print()
    print("=== session handling ===")
    status, _, _ = request(new_session(), f"{GATEWAY}/api/me")
    check("no cookie means 401", status == 401, f"HTTP {status}")
    status, _, _ = request(alice, f"{GATEWAY}/auth/logout", method="POST")
    check("logout succeeds", status == 200, f"HTTP {status}")

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s)")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("OK — the OIDC flow works end to end against a real Keycloak.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
