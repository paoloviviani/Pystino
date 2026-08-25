#!/usr/bin/env python3
"""Check the claims docker-compose.proxy.yml makes about a public deployment.

The other live scripts ask whether the platform works. This one asks whether it
is *only* what it is supposed to be, which is the question that matters once the
stack has an address the world can reach — and the one no unit test can answer,
because every assertion here is about sockets, certificates and the credentials
a real Keycloak will accept.

    docker compose --env-file deploy/.env \\
      -f deploy/compose/docker-compose.yml \\
      -f deploy/compose/docker-compose.smoke.yml \\
      -f deploy/compose/docker-compose.keycloak.yml \\
      -f deploy/compose/docker-compose.redaction.yml \\
      -f deploy/compose/docker-compose.proxy.yml up -d --build

    docker compose ... exec proxy cat \\
      /data/caddy/pki/authorities/local/root.crt > deploy/tls/caddy-root.crt

    set -a; . deploy/.env; set +a
    ./scripts/test_public_tls_live.py

What it asserts:

* the public origin serves TLS, and the certificate verifies — against the CA
  file, never against an unverified context, because a check that skips
  verification passes against anything that answers on the address;
* the four holes CLAUDE.md's fourth ground rule is about are closed: no
  plaintext, no admin/admin, no published seed password, a Secure cookie;
* nothing else of this stack is on a routable interface — PostgreSQL, Valkey,
  the gateway's own port, Keycloak's own port and the fake upstream are all
  reachable on loopback and refused everywhere else;
* a streamed completion still streams through the proxy.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_oidc_flow import (
    CA_BUNDLE,
    GATEWAY,
    KEYCLOAK_BASE,
    PASSWORDS,
    login,
    new_session,
    request,
)

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILURES.append(label)
    return condition


def skip(label: str, reason: str) -> None:
    print(f"  [skip] {label} — {reason}")


def report() -> int:
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("OK — the public deployment is TLS-only, and is only what it claims to be.")
    return 0


def routable_address() -> str | None:
    """This host's own non-loopback address.

    Found by asking the routing table which source address a packet to a public
    address would leave with — a UDP connect, so nothing is sent. Reading it from
    an interface list instead would have to guess which of several is the one
    that matters, and on this host there are five.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 53))  # TEST-NET-1: routed nowhere, never answers
        address: str = probe.getsockname()[0]
        return None if address.startswith("127.") else address
    except OSError:
        return None
    finally:
        probe.close()


def port_open(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def main() -> int:
    origin = GATEWAY
    if not origin.startswith("https://"):
        print(f"GATEWAY is {origin}, not https — source deploy/.env first.")
        return 2
    parsed = urllib.parse.urlsplit(origin)
    host, port = parsed.hostname or "", parsed.port or 443

    print(f"=== the certificate on {host}:{port} ===")
    if not Path(CA_BUNDLE).exists():
        print(f"  no CA file at {CA_BUNDLE}. Export it first:")
        print("    docker compose ... exec proxy cat \\")
        print("      /data/caddy/pki/authorities/local/root.crt > deploy/tls/caddy-root.crt")
        return 2

    context = ssl.create_default_context(cafile=CA_BUNDLE)
    try:
        # server_hostname is what verification is done against. For an IP it is
        # not sent as SNI — that is forbidden — but it is still the name the
        # certificate must cover, which is why the Caddyfile has to name a
        # default_sni for the server to have anything to offer at all.
        with (
            socket.create_connection((host, port), timeout=10) as raw,
            context.wrap_socket(raw, server_hostname=host) as tls,
        ):
            certificate = tls.getpeercert()
            version = tls.version()
    except (OSError, ssl.SSLError) as exc:
        check("the certificate verifies against the CA file", False, str(exc))
        return 1

    names = [value for kind, value in certificate.get("subjectAltName", ())]
    check("the certificate verifies against the CA file", True, f"names: {', '.join(names)}")
    check("it covers the address people type", host in names, host)
    check(
        "the negotiated protocol is TLS 1.2 or better",
        version in {"TLSv1.2", "TLSv1.3"},
        str(version),
    )
    print(f"  issuer:  {dict(x[0] for x in certificate.get('issuer', ()))}")
    print(f"  expires: {certificate.get('notAfter')}")

    print()
    print("=== nothing else of this stack is on a routable interface ===")
    # The point of the exercise. Each of these is a service with no
    # authentication worth the name — or, in the gateway's case, the same API
    # served without TLS — and each is published by a compose file in this
    # repository. Both halves matter: reachable on loopback proves the service is
    # actually running, so a refusal on the routable address is a binding and not
    # an empty port.
    address = routable_address()
    if address is None:
        skip("private ports are loopback-only", "this host has no non-loopback address")
    else:
        private = {
            5432: "PostgreSQL",
            6379: "Valkey",
            8000: "the gateway, without TLS",
            8081: "the fake upstream",
            int(os.environ.get("KEYCLOAK_PORT", "8080")): "Keycloak, without TLS",
        }
        for number, what in sorted(private.items()):
            if not port_open("127.0.0.1", number):
                skip(f"{what} on {number}", "not published at all, so nothing to reach")
                continue
            # The detail is printed on success too, so it says what was tried
            # rather than what went wrong — a line reading "reachable" next to an
            # [ok] is worse than no line at all.
            check(
                f"{what} answers on loopback and refuses {address}:{number}",
                not port_open(address, number),
                f"loopback {number} open, {address}:{number} tried",
            )
        # Not vanity: without it the three checks above would also pass on a host
        # where nothing at all was listening on a routable address.
        check(f"the proxy does answer on {address}:{port}", port_open(address, port))

    print()
    print("=== the credentials in the repository do not work ===")

    # Keycloak's master realm, which is the admin console's own login. The
    # fixture and every README say admin/admin; a deployment on a public address
    # that still accepts it has one hole, not four.
    def token(realm: str, data: dict[str, str]) -> tuple[int, dict[str, object]]:
        url = f"{KEYCLOAK_BASE}/realms/{realm}/protocol/openid-connect/token"
        opener = new_session()
        status, _, body = request(opener, url, data=data)
        try:
            return status, json.loads(body)
        except ValueError:
            return status, {}

    status, _ = token(
        "master",
        {
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": "admin",
            "password": "admin",
        },
    )
    # Not `== 401`: Keycloak answers a bad password with 400 invalid_grant and a
    # disabled or missing user with 401, and both are refusals. Asserting on the
    # exact code would make this fail for the right reason and read like a hole.
    check("admin/admin is refused on the admin console", status != 200, f"HTTP {status}")

    admin_password = os.environ.get("KEYCLOAK_ADMIN_PASSWORD", "")
    if admin_password:
        status, _ = token(
            "master",
            {
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": os.environ.get("KEYCLOAK_ADMIN_USER", "admin"),
                "password": admin_password,
            },
        )
        check("and the configured admin password is", status == 200, f"HTTP {status}")
    else:
        skip("the configured admin password works", "KEYCLOAK_ADMIN_PASSWORD is not set")

    secret = os.environ.get("KEYCLOAK_CLIENT_SECRET", "")
    if secret:
        grant = {
            "grant_type": "password",
            "client_id": "llm-gateway",
            "client_secret": secret,
            "username": "dave",
            "password": "dave-password",
        }
        status, payload = token("llm-platform", grant)
        check(
            "the published seed password no longer signs anyone in",
            status != 200,
            f"HTTP {status}: {payload.get('error_description', payload.get('error', ''))}",
        )
        grant["password"] = PASSWORDS["dave"]
        status, _ = token("llm-platform", grant)
        check("and the rotated one does", status == 200, f"HTTP {status}")

        # The value in docker-compose.keycloak.yml. Hardcoded on purpose: what
        # is being asserted is that it no longer works.
        grant["client_secret"] = "gateway-dev-secret-not-for-production"  # noqa: S105
        status, _ = token("llm-platform", grant)
        check("the published client secret is refused", status != 200, f"HTTP {status}")
    else:
        skip("the seed and client secrets were rotated", "KEYCLOAK_CLIENT_SECRET is not set")

    print()
    print("=== what a browser is given ===")
    opener = new_session()
    status, headers, _ = request(opener, f"{GATEWAY}/auth/login")
    cookie = headers.get("set-cookie", "")
    check("signing in sets a cookie", bool(cookie), cookie[:60])
    # Without Secure the login-state cookie travels on any downgrade to http,
    # which is the whole reason GATEWAY_SESSION_COOKIE_SECURE exists — and it is
    # only correct to set because something is terminating TLS in front.
    check("and marks it Secure", "secure" in cookie.lower(), cookie[:80])
    check("and HttpOnly", "httponly" in cookie.lower(), cookie[:80])
    check(
        "the browser is sent to the provider over https",
        headers.get("location", "").startswith("https://"),
        headers.get("location", "")[:70],
    )

    status, _, body = request(
        opener, f"{KEYCLOAK_BASE}/realms/llm-platform/.well-known/openid-configuration"
    )
    discovery = json.loads(body) if status == 200 else {}
    check(
        "the provider advertises itself over https",
        str(discovery.get("issuer", "")).startswith("https://"),
        str(discovery.get("issuer", "")),
    )

    print()
    print("=== a billed request, over TLS ===")
    session = login("alice")
    if session is None:
        check("alice can sign in", False, "the login flow failed; run test_oidc_flow.py")
        return 1
    status, _, body = request(
        session, f"{GATEWAY}/api/me/keys", json_body={"name": "public-tls-check"}, method="POST"
    )
    if not check("a key can be minted over the public origin", status == 201, f"HTTP {status}"):
        return 1
    key = json.loads(body)["secret"]

    models_request = urllib.request.Request(
        f"{GATEWAY}/v1/models", headers={"authorization": f"Bearer {key}"}
    )
    with urllib.request.urlopen(models_request, timeout=30) as response:
        models = json.load(response)["data"]
    chat = next((m["id"] for m in models if m.get("kind", "chat") == "chat"), None)
    if chat is None:
        skip("a streamed completion is not buffered", "no chat model is granted to alice's group")
    else:
        stream_request = urllib.request.Request(
            f"{GATEWAY}/v1/chat/completions",
            headers={"authorization": f"Bearer {key}", "content-type": "application/json"},
            data=json.dumps(
                {
                    "model": chat,
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": True,
                }
            ).encode(),
        )
        try:
            with urllib.request.urlopen(stream_request, timeout=60) as response:
                content_length = response.headers.get("content-length")
                first = response.readline()
                rest = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code != 429:
                raise
            # The demo user's cap is EUR 1 an hour and the fake upstream reports
            # a million tokens per request, so running the live scripts back to
            # back exhausts it legitimately. Skipped rather than failed, like the
            # others: a quota that refuses is the quota working.
            skip("a streamed completion is not buffered", "the hourly cap is exhausted")
            return report()
        check(
            "a completion streams over TLS",
            first.startswith(b"data: ") and b"[DONE]" in rest,
            first[:40].decode("utf-8", "replace"),
        )
        # A proxy that buffered the response would have the whole body in hand
        # and would say how long it is. Nothing errors when it does that; the
        # bytes are all correct and all late, which is why this is asserted
        # rather than eyeballed.
        check(
            "and the proxy did not buffer it into one body",
            content_length is None,
            f"content-length: {content_length}",
        )

    return report()


if __name__ == "__main__":
    sys.exit(main())
