"""Shared live-check plumbing: an authenticated browser session, by OpenID Connect.

Every live script signs a person in and then drives the same-origin API with
the session cookie. People sign in through an identity provider only (ADR
0088), so this module walks the same authorization-code flow a browser does:
the gateway's `/auth/login?provider=…`, the bundled Authelia's first-factor
API, the gateway's callback, which sets the session cookie. What comes back is
indistinguishable from a browser session. `deploy/ci/e2e_login.py` is the same
walk, run by CI against a fresh install.

The IdP must be the bundled Authelia (its portal API is what is driven here),
and the gateway's client must not ask for consent — first-party clients are
`consent_mode: implicit` (D15).

Credentials come from the environment:

    PUBLIC_ORIGIN                    the deployment (source the deploy dir's .env)
    PYSTINO_LIVE_ADMIN_USER          Authelia username (default $AUTHELIA_ADMIN_USER, else admin)
    PYSTINO_LIVE_ADMIN_PASSWORD      required: the password behind AUTHELIA_ADMIN_PASSWORD_DIGEST
                                     (whatever you minted it from — deploy/.env.example)
    PYSTINO_LIVE_USER                optional, a non-admin for the 403 checks
    PYSTINO_LIVE_USER_PASSWORD
    PYSTINO_LIVE_PROVIDER            optional: the provider name (default: the first enabled)
    PYSTINO_LIVE_RESOLVE             optional: an IP the public name resolves to (a trial
                                     stack with no DNS), as a hosts-file entry would
    GATEWAY_CA_BUNDLE                optional: a CA to trust (TLS_MODE=internal)

A second person is added in the console (Settings → Identity providers →
People). A script that asks for the non-admin and finds none configured
reports that as skipped, not failed — the check is about authorisation, not
about provisioning.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

PUBLIC_ORIGIN = os.environ.get("PUBLIC_ORIGIN", "").rstrip("/")
GATEWAY = (os.environ.get("GATEWAY_URL") or PUBLIC_ORIGIN or "http://localhost:8000").rstrip("/")

# A trial stack's public name usually has no DNS; resolve it to the proxy the
# way a hosts-file entry would, so URLs, Host headers, SNI and cookie domains
# stay exactly what a browser would use.
_RESOLVE = os.environ.get("PYSTINO_LIVE_RESOLVE", "")
if _RESOLVE:
    _public_host = urllib.parse.urlsplit(GATEWAY).hostname
    _getaddrinfo = socket.getaddrinfo

    def _resolve(host: Any, *rest: Any, **kw: Any) -> Any:
        return _getaddrinfo(_RESOLVE if host == _public_host else host, *rest, **kw)

    socket.getaddrinfo = _resolve

# Verified TLS always; a stack on Caddy's internal CA names that CA here:
#   docker compose exec proxy cat /data/caddy/pki/authorities/local/root.crt > caddy-root.crt
CA_BUNDLE = os.environ.get("GATEWAY_CA_BUNDLE", "")
_SSL_CONTEXT: ssl.SSLContext | None = None
if GATEWAY.startswith("https://") and CA_BUNDLE:
    _SSL_CONTEXT = ssl.create_default_context(cafile=CA_BUNDLE)
    # Also as the process default, because not every call in the scripts goes
    # through `new_session()`: the `/v1` checks use a bare `urlopen`
    # deliberately, to prove a key works with no cookie jar in play.
    urllib.request.install_opener(
        urllib.request.build_opener(urllib.request.HTTPSHandler(context=_SSL_CONTEXT))
    )

failures: list[str] = []


def admin_credentials() -> tuple[str, str] | None:
    user = os.environ.get("PYSTINO_LIVE_ADMIN_USER") or os.environ.get(
        "AUTHELIA_ADMIN_USER", "admin"
    )
    password = os.environ.get("PYSTINO_LIVE_ADMIN_PASSWORD", "")
    return (user, password) if password else None


def user_credentials() -> tuple[str, str] | None:
    user = os.environ.get("PYSTINO_LIVE_USER", "")
    password = os.environ.get("PYSTINO_LIVE_USER_PASSWORD", "")
    return (user, password) if user and password else None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stop at redirects so each hop can be inspected."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class LocalhostSecureCookiePolicy(http.cookiejar.DefaultCookiePolicy):
    """Send ``Secure`` cookies to ``http://localhost``, as browsers do.

    Browsers treat ``http://localhost`` as a *secure context* and send Secure
    cookies to it; ``http.cookiejar`` implements no such exception. That is a
    limitation of this test client, not of the gateway, so it is corrected here
    rather than by weakening anything in the stack.
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
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    body: bytes | None = None
    sent = {"accept": "application/json, text/html", **(headers or {})}
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        sent["content-type"] = "application/x-www-form-urlencoded"
    elif json_body is not None:
        body = json.dumps(json_body).encode()
        sent["content-type"] = "application/json"

    req = urllib.request.Request(url, data=body, headers=sent, method=method)

    def lower(message: Any) -> dict[str, str]:
        # HTTP header names are case-insensitive and servers disagree. A
        # response may set several cookies; they are kept, one per line,
        # rather than the last one silently winning.
        out: dict[str, str] = {}
        for key, value in message.items():
            key = key.lower()
            out[key] = f"{out[key]}\n{value}" if key == "set-cookie" and key in out else value
        return out

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


def _provider(opener: urllib.request.OpenerDirector) -> str:
    named = os.environ.get("PYSTINO_LIVE_PROVIDER", "")
    if named:
        return named
    status, _, body = request(opener, f"{GATEWAY}/auth/methods")
    providers = json.loads(body).get("providers", []) if status == 200 else []
    if not providers:
        raise RuntimeError(f"GET /auth/methods named no provider (HTTP {status})")
    return str(providers[0]["name"])


@dataclass
class SignIn:
    """The outcome of one walk through the login flow."""

    opener: urllib.request.OpenerDirector | None
    #: Authelia's answer to the password (200 accepted, 401 refused).
    first_factor_status: int
    #: The cookies the gateway's callback set (deletions left out), one per line —
    #: its session cookie among them.
    session_cookie: str = ""
    error: str = ""


def sign_in(username: str, password: str) -> SignIn:
    """Walk the authorization-code flow as a browser would; never raises for a refusal."""
    opener = new_session()
    try:
        provider = _provider(opener)
    except (RuntimeError, OSError, ValueError) as exc:
        return SignIn(None, 0, error=str(exc))
    status, headers, _ = request(
        opener, f"{GATEWAY}/auth/login?" + urllib.parse.urlencode({"provider": provider})
    )
    authorize = headers.get("location", "")
    if status not in (302, 303, 307) or not authorize:
        return SignIn(None, 0, error=f"/auth/login answered HTTP {status}, no redirect")

    first = 0
    location = authorize
    for _ in range(8):
        split = urllib.parse.urlsplit(location)
        query = dict(urllib.parse.parse_qsl(split.query))
        if split.path.startswith("/auth/callback/"):
            status, headers, body = request(opener, location)
            if status not in (302, 303):
                return SignIn(None, first, error=f"callback HTTP {status}: {body[:160]!r}")
            cookies = [
                line
                for line in headers.get("set-cookie", "").split("\n")
                if line and "max-age=0" not in line.lower()
            ]
            return SignIn(opener, first, session_cookie="\n".join(cookies))
        if "consent" in split.path:
            return SignIn(
                None,
                first,
                error="the IdP asks for consent; first-party clients should be "
                "consent_mode: implicit (D15)",
            )
        if query.get("flow_id") and not first:
            # Authelia's portal: its API lives under the portal's own path.
            portal = f"{split.scheme}://{split.netloc}{split.path.rstrip('/')}"
            first, _, body = request(
                opener,
                f"{portal}/api/firstfactor",
                json_body={
                    "username": username,
                    "password": password,
                    "keepMeLoggedIn": False,
                    "flow": "openid_connect",
                    "flowID": query["flow_id"],
                },
                headers={"origin": f"{split.scheme}://{split.netloc}"},
            )
            if first != 200:
                return SignIn(None, first, error=f"first factor HTTP {first}: {body[:120]!r}")
            location = authorize  # the authorization now completes on the IdP session
            continue
        status, headers, body = request(opener, location)
        location = urllib.parse.urljoin(location, headers.get("location", ""))
        if status not in (302, 303, 307) or not headers.get("location"):
            return SignIn(None, first, error=f"unexpected hop {status} at {split.path}")
    return SignIn(None, first, error="the login flow never reached the gateway's callback")


def login(username: str, password: str) -> urllib.request.OpenerDirector | None:
    """Sign in through the IdP and return the authenticated session, or None (reported)."""
    result = sign_in(username, password)
    if result.opener is None:
        check(f"{username}: OIDC sign-in", False, result.error)
        return None
    status, _, _ = request(result.opener, f"{GATEWAY}/auth/session")
    if status != 200:
        check(f"{username}: session cookie was accepted", False, f"HTTP {status}")
        return None
    return result.opener
