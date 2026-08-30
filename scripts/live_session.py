"""Shared live-check plumbing: an authenticated browser session, by password.

Every live script signs a person in and then drives the same-origin API with
the session cookie. That used to mean driving the identity provider's login
form; now the gateway has its own first-class way in — local email + password
(ADR 0043) — and this module uses it. No identity provider is needed to run
the live checks, which is the point: the checks must work on a deployment with
no IdP configured at all.

Credentials come from the environment, i.e. deploy/.env:

    GATEWAY_LOCAL_ADMIN_EMAIL      (default admin@local)
    GATEWAY_LOCAL_ADMIN_PASSWORD   (required)
    GATEWAY_LOCAL_USER_EMAIL       (optional, a non-admin for the 403 checks)
    GATEWAY_LOCAL_USER_PASSWORD

The admin account exists on every deployment that turned local auth on;
`gateway passwd --no-admin user@local` creates the second. A script that asks
for the non-admin and finds none configured reports that as skipped, not
failed — the check is about authorisation, not about provisioning.

The session plumbing itself (cookie jar, no-redirect opener, TLS context from
Caddy's CA, the error envelope) is deliberately the same shape
test_oidc_flow.py used to have, so the scripts that survived Keycloak's
removal read unchanged.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# What a browser types. Behind the TLS proxy the scripts must follow
# PUBLIC_HOST/HTTPS_PORT (source deploy/.env first); on the dev shape they
# follow the published loopback port.
PUBLIC_ORIGIN = os.environ.get("PUBLIC_ORIGIN") or (
    f"https://{os.environ['PUBLIC_HOST']}:{os.environ.get('HTTPS_PORT', '443')}"
    if os.environ.get("PUBLIC_HOST")
    else ""
)
GATEWAY = os.environ.get("GATEWAY_URL") or PUBLIC_ORIGIN or (
    f"http://localhost:{os.environ.get('GATEWAY_PORT', '8000')}"
)

# Caddy's internal CA signs the certificate in the IP configuration, and nothing
# has heard of it — including these scripts, which verify properly rather than
# skipping verification. Export it once:
#
#   docker compose ... exec proxy cat \
#     /data/caddy/pki/authorities/local/root.crt > deploy/tls/caddy-root.crt
CA_BUNDLE = os.environ.get(
    "GATEWAY_CA_BUNDLE", str(Path(__file__).resolve().parent.parent / "deploy/tls/caddy-root.crt")
)
_SSL_CONTEXT: ssl.SSLContext | None = None
if GATEWAY.startswith("https://") and Path(CA_BUNDLE).exists():
    _SSL_CONTEXT = ssl.create_default_context(cafile=CA_BUNDLE)
    # Also as the process default, because not every call in the scripts goes
    # through `new_session()`: the `/v1` checks use a bare `urlopen`
    # deliberately, to prove a key works with no cookie jar in play. Without
    # this those calls verify against the system store, which has never heard
    # of Caddy's internal CA, and fail with an unhelpful
    # CERTIFICATE_VERIFY_FAILED long after the TLS checks passed.
    urllib.request.install_opener(
        urllib.request.build_opener(urllib.request.HTTPSHandler(context=_SSL_CONTEXT))
    )

failures: list[str] = []


def admin_credentials() -> tuple[str, str] | None:
    email = os.environ.get("GATEWAY_LOCAL_ADMIN_EMAIL", "admin@local")
    password = os.environ.get("GATEWAY_LOCAL_ADMIN_PASSWORD", "")
    return (email, password) if password else None


def user_credentials() -> tuple[str, str] | None:
    email = os.environ.get("GATEWAY_LOCAL_USER_EMAIL", "")
    password = os.environ.get("GATEWAY_LOCAL_USER_PASSWORD", "")
    return (email, password) if email and password else None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stop at redirects so each hop can be inspected."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class LocalhostSecureCookiePolicy(http.cookiejar.DefaultCookiePolicy):
    """Send ``Secure`` cookies to ``http://localhost``, as browsers do.

    Behind the TLS proxy every cookie is Secure, and browsers treat
    ``http://localhost`` as a *secure context* and send such cookies anyway;
    ``http.cookiejar`` implements no such exception, so it silently withholds
    them and a dev-shape login appears to never stick.

    That is a limitation of this test client, not of the gateway, so it is
    corrected here rather than by weakening anything in the stack.
    """

    def return_ok_secure(self, cookie: http.cookiejar.Cookie, request: Any) -> bool:
        host = urllib.parse.urlsplit(request.full_url).hostname or ""
        if host in {"localhost", "127.0.0.1", "::1"}:
            return True
        return bool(super().return_ok_secure(cookie, request))


def new_session() -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar(policy=LocalhostSecureCookiePolicy())
    handlers: list[Any] = [urllib.request.HTTPCookieProcessor(jar), NoRedirect]
    if _SSL_CONTEXT is not None:
        handlers.append(urllib.request.HTTPSHandler(context=_SSL_CONTEXT))
    return urllib.request.build_opener(*handlers)


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
        # HTTP header names are case-insensitive and servers disagree.
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


def skip(label: str, reason: str) -> None:
    print(f"  [skip] {label} — {reason}")


def login(email: str, password: str) -> urllib.request.OpenerDirector | None:
    """Sign in with email + password and return the authenticated session.

    POST /auth/login sets the same session cookie the OIDC callback sets, so
    what comes back is indistinguishable from a browser session — which is the
    feature working as designed, exercised here on every live run.
    """
    opener = new_session()
    status, _, body = request(
        opener,
        f"{GATEWAY}/auth/login",
        json_body={"email": email, "password": password},
    )
    if status != 200:
        check(f"{email}: password sign-in", False, f"HTTP {status}: {body[:120]!r}")
        return None
    status, _, _me_body = request(opener, f"{GATEWAY}/auth/session")
    if status != 200:
        check(f"{email}: session cookie was accepted", False, f"HTTP {status}")
        return None
    return opener
