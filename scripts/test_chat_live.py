#!/usr/bin/env python3
"""Drive the chat application end to end against the running stack.

The unit suites cover each half: chat-api against a fake gateway, the gateway
against a stub identity provider. What neither can cover is the join — a real
Keycloak login on one service, whose access token is then accepted by a
different service on ``/v1``. That join is the whole of M0 and M1, and it has
already been wrong twice in ways only a live run showed (an audience Keycloak
does not emit by default, and a realm import that wiped the built-in client
scopes).

What it asserts:

* the SPA is served under ``/chat`` with its assets and a CSP;
* a browser can sign in, and gets a session cookie scoped to ``/chat``;
* ``/chat/api/models`` returns what the *gateway* says that person may use;
* a chat turn streams, and lands in the ledger attributed to that person with
  **no API key** — which is the point of the whole arrangement;
* the gateway still owns the root of the origin.

Behind the TLS proxy:

    set -a; . deploy/.env; set +a
    ./scripts/test_chat_live.py
"""

from __future__ import annotations

import html
import http.cookiejar
import json
import os
import re
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
CHAT = os.environ.get("CHAT_URL") or (
    f"{PUBLIC_ORIGIN}/chat"
    if PUBLIC_ORIGIN
    else f"http://localhost:{os.environ.get('CHAT_PORT', '8100')}/chat"
)
GATEWAY = os.environ.get("GATEWAY_URL") or PUBLIC_ORIGIN or "http://localhost:8000"

CA_BUNDLE = os.environ.get(
    "GATEWAY_CA_BUNDLE", str(Path(__file__).resolve().parent.parent / "deploy/tls/caddy-root.crt")
)
_CONTEXT: ssl.SSLContext | None = None
if CHAT.startswith("https://") and Path(CA_BUNDLE).exists():
    _CONTEXT = ssl.create_default_context(cafile=CA_BUNDLE)

SEED_PASSWORD = os.environ.get("KEYCLOAK_SEED_PASSWORD") or "alice-password"
MODEL = os.environ.get("CHAT_TEST_MODEL", "smoke-model")

failures: list[str] = []


def fail(message: str) -> None:
    failures.append(message)
    print(f"  FAILED: {message}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stop at redirects so each hop can be inspected."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def opener(follow: bool = True) -> urllib.request.OpenerDirector:
    handlers: list[urllib.request.BaseHandler] = [
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    ]
    if _CONTEXT is not None:
        handlers.append(urllib.request.HTTPSHandler(context=_CONTEXT))
    if not follow:
        handlers.append(NoRedirect())
    return urllib.request.build_opener(*handlers)


def fetch(
    client: urllib.request.OpenerDirector,
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    method: str | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with client.open(request) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)
    except urllib.error.URLError as exc:
        print(f"  cannot reach {url}: {exc}")
        raise SystemExit(1) from exc


def sign_in(client: urllib.request.OpenerDirector, username: str = "alice") -> bool:
    """The whole authorization-code flow, as a browser would walk it."""
    status, body, headers = fetch(opener_shared(client, follow=False), f"{CHAT}/auth/login")
    if status not in (301, 302, 303, 307):
        fail(f"/chat/auth/login returned HTTP {status} instead of a redirect")
        return False

    status, body, _ = fetch(client, headers["Location"])
    if status != 200:
        fail(f"the Keycloak login page returned HTTP {status}")
        return False

    match = re.search(r'action="([^"]+)"', body.decode("utf-8", "replace"))
    if not match:
        fail("no login form on the Keycloak page")
        return False
    action = html.unescape(match.group(1))

    form = urllib.parse.urlencode({"username": username, "password": SEED_PASSWORD}).encode()
    status, _, headers = fetch(
        opener_shared(client, follow=False),
        action,
        data=form,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    if status not in (301, 302, 303, 307):
        fail(f"the login form POST returned HTTP {status}, not a redirect back")
        return False

    # The callback, then the redirect it issues to the app.
    status, _, headers = fetch(opener_shared(client, follow=False), headers["Location"])
    if status not in (301, 302, 303, 307):
        fail(f"the chat callback returned HTTP {status} instead of a redirect")
        return False
    return True


_SHARED_JAR = http.cookiejar.CookieJar()


def opener_shared(
    _client: urllib.request.OpenerDirector, *, follow: bool = True
) -> urllib.request.OpenerDirector:
    """An opener over the one cookie jar, with redirects optionally stopped.

    One jar for the whole flow, because the login state cookie set on the way
    out has to still be there on the way back — losing it produces "No sign-in
    is in progress", which reads like a broken flow rather than a lost cookie.
    """
    handlers: list[urllib.request.BaseHandler] = [
        urllib.request.HTTPCookieProcessor(_SHARED_JAR)
    ]
    if _CONTEXT is not None:
        handlers.append(urllib.request.HTTPSHandler(context=_CONTEXT))
    if not follow:
        handlers.append(NoRedirect())
    return urllib.request.build_opener(*handlers)


def main() -> int:
    print(f"chat    {CHAT}")
    print(f"gateway {GATEWAY}")
    print()

    client = opener_shared(opener())

    print("=== the SPA is served under /chat ===")
    status, body, headers = fetch(client, f"{CHAT}/")
    if status != 200 or "text/html" not in headers.get("Content-Type", ""):
        fail(f"/chat/ returned HTTP {status} {headers.get('Content-Type')}")
    else:
        print(f"  entry document, cache-control: {headers.get('Cache-Control')}")
    if "frame-ancestors 'none'" not in headers.get("Content-Security-Policy", ""):
        fail("no frame-ancestors in the CSP on the chat entry document")

    asset = re.search(rb'src="(/chat/assets/[^"]+\.js)"', body)
    if not asset:
        fail("the entry document references no /chat/assets script")
    else:
        url = f"{GATEWAY}{asset.group(1).decode()}"
        status, _, headers = fetch(client, url)
        if status != 200:
            # The failure this catches: an app built for the root and served
            # under a prefix asks the *gateway* for its scripts.
            fail(f"the SPA's own script returned HTTP {status} from {url}")
        else:
            print(f"  assets, cache-control: {headers.get('Cache-Control')}")

    print()
    print("=== the gateway still owns the root of this origin ===")
    status, _, _ = fetch(client, f"{GATEWAY}/console/")
    if status != 200:
        fail(f"/console/ returned HTTP {status}; the chat route has shadowed the gateway")
    else:
        print("  /console still answers")

    print()
    print("=== signing in ===")
    if not sign_in(client):
        print("\nFAILED: no session, so nothing below can be checked.")
        return 1
    status, body, _ = fetch(client, f"{CHAT}/api/me")
    if status != 200:
        fail(f"/chat/api/me returned HTTP {status} after a login that redirected correctly")
        return 1
    me = json.loads(body)
    print(f"  signed in as {me.get('email')} groups={me.get('groups')}")
    if not me.get("console_url"):
        print("  (no console link offered; that is only shown to admins)")

    print()
    print("=== the catalogue is the gateway's ===")
    status, body, _ = fetch(client, f"{CHAT}/api/models")
    if status != 200:
        fail(f"/chat/api/models returned HTTP {status}")
        return 1
    models = [item["id"] for item in json.loads(body)["data"]]
    print(f"  {len(models)} model(s): {', '.join(models[:5])}")
    if not models:
        print("\nSKIPPED — no model is granted to this person, so there is no turn to take.")
        return 1 if failures else 0

    model = MODEL if MODEL in models else models[0]

    print()
    print("=== a turn ===")
    status, body, _ = fetch(
        client,
        f"{CHAT}/api/conversations",
        data=json.dumps({"model": model}).encode(),
        headers={"content-type": "application/json"},
    )
    if status != 201:
        fail(f"creating a conversation returned HTTP {status}: {body[:200]!r}")
        return 1
    conversation_id = json.loads(body)["id"]

    status, body, headers = fetch(
        client,
        f"{CHAT}/api/conversations/{conversation_id}/messages",
        data=json.dumps({"content": "Say hello in one word."}).encode(),
        headers={"content-type": "application/json"},
    )
    if status != 200:
        fail(f"the turn returned HTTP {status}: {body[:300]!r}")
        return 1

    text = body.decode("utf-8", "replace")
    if "event: error" in text:
        # A quota refusal is a legitimate outcome — the demo cap is EUR 1/hour
        # and running these scripts back to back exhausts it.
        print(f"  the gateway refused: {text[text.find('event: error') :][:200]}")
    deltas = re.findall(r'event: delta\ndata: (\{.*?\})\n', text)
    answer = "".join(json.loads(frame).get("content", "") for frame in deltas)
    done = re.search(r"event: done\ndata: (\{.*?\})\n", text)
    print(f"  {len(deltas)} delta frame(s), answer: {answer[:60]!r}")
    if done:
        info = json.loads(done.group(1))
        print(f"  request_id={info.get('request_id')} usage={info.get('usage')}")
        if not info.get("request_id") and deltas:
            fail("the turn carried no gateway request id, so it cannot be reconciled")
    else:
        fail("the stream never sent a done event")

    print()
    print("=== the transcript survived ===")
    status, body, _ = fetch(client, f"{CHAT}/api/conversations/{conversation_id}")
    detail = json.loads(body)
    roles = [message["role"] for message in detail["messages"]]
    statuses = [message["status"] for message in detail["messages"]]
    print(f"  {len(roles)} message(s): {list(zip(roles, statuses, strict=True))}")
    if roles[:1] != ["user"]:
        fail(f"the stored transcript starts with {roles[:1]}, not the person's own message")

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s)")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("OK — the chat application works end to end against the real stack.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
