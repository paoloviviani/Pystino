"""End-to-end sign-in through a Pystino stack (used by stack.yml and by hand).

    uv run python deploy/ci/e2e_login.py <deploy dir> <first user's password> [--chat]

Browser-shaped: every request goes to the public origin, whose name this
script resolves to the local proxy; cookies are kept per host and redirects
are followed by hand so each hop prints. It proves: the proxy routes, Authelia
serves its portal under /authelia, the gateway's OIDC client exchanges the
code over the internal back-channel (no CA bundle anywhere), and the bootstrap
address is the administrator. With --chat it then signs into the chat on the
same Authelia session — the chat's own back-channel discovery and token
exchange — and reads the signed-in user back.
"""

import socket
import sys
import urllib.parse

import httpx

args = [a for a in sys.argv[1:] if not a.startswith("--")]
if len(args) != 2:
    sys.exit("usage: e2e_login.py <deploy dir> <first user password> [--chat]")
deploy, password = args
chat = "--chat" in sys.argv

env = {}
for line in open(f"{deploy}/.env"):
    if "=" in line and not line.startswith("#"):
        key, value = line.rstrip("\n").split("=", 1)
        env[key] = value.strip("'")
origin = env["PUBLIC_ORIGIN"]
public = urllib.parse.urlsplit(origin)

# The public name resolves to this machine, as DNS would make it for a real
# deployment; URLs, Host headers, SNI and cookie domains stay exactly as a
# browser would have them.
_getaddrinfo = socket.getaddrinfo


def _resolve(host, *rest, **kw):
    return _getaddrinfo("127.0.0.1" if host == public.hostname else host, *rest, **kw)


socket.getaddrinfo = _resolve
client = httpx.Client(verify=False, follow_redirects=False, timeout=30)


def step(method, url, **kw):
    response = client.request(method, url, **kw)
    where = response.headers.get("location", "")[:100]
    print(f"{method} {urllib.parse.urlsplit(url).path[:60]} -> {response.status_code} {where}")
    return response


def authorize(auth_url: str, client_id: str, done_marker: str) -> str:
    """Walk Authelia from an authorization URL to the relying party's callback."""
    loc = ""
    for _ in range(6):
        r = step("GET", auth_url)
        loc = r.headers.get("location", "")
        if done_marker in loc:
            return loc
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(loc).query))
        if loc.startswith(f"{origin}/authelia/?") and query.get("flow_id"):
            first = step(
                "POST",
                f"{origin}/authelia/api/firstfactor",
                json={
                    "username": env["AUTHELIA_ADMIN_USER"],
                    "password": password,
                    "keepMeLoggedIn": False,
                    "flow": "openid_connect",
                    "flowID": query["flow_id"],
                },
                headers={"origin": origin},
            )
            assert first.status_code == 200, first.text[:300]
        elif "consent" in loc and query.get("flow_id"):
            info = step(
                "GET",
                f"{origin}/authelia/api/oidc/consent?flow_id={query['flow_id']}",
                headers={"origin": origin},
            )
            claims = info.json().get("data", {}).get("claims") or []
            decision = step(
                "POST",
                f"{origin}/authelia/api/oidc/consent",
                json={
                    "flow_id": query["flow_id"],
                    "client_id": client_id,
                    "consent": True,
                    "pre_configure": True,
                    "claims": claims,
                },
                headers={"origin": origin},
            )
            auth_url = decision.json()["data"]["redirect_uri"]
        else:
            raise SystemExit(f"unexpected hop: {r.status_code} {loc} {r.text[:200]}")
    raise SystemExit(f"never reached {done_marker}: {loc}")


# --- the console ------------------------------------------------------------
r = step("GET", f"{origin}/auth/login?provider=default")
assert r.status_code in (302, 303, 307), r.text[:300]
assert r.headers["location"].startswith(f"{origin}/authelia/"), r.headers["location"]
callback = authorize(r.headers["location"], "pystino-console", "/auth/callback/")
r = step("GET", callback)  # the gateway exchanges the code over the back-channel here
assert r.status_code in (302, 303), r.text[:500]
me = step("GET", f"{origin}/api/me")
assert me.status_code == 200, me.text[:300]
body = me.json()
print("ME:", {k: body.get(k) for k in ("email", "is_admin", "groups")})
assert body.get("is_admin") is True, "the bootstrap address was not made administrator"

# --- the chat ---------------------------------------------------------------
if chat:
    r = step("GET", f"{origin}/chat/login")
    assert r.status_code in (302, 303, 307), r.text[:300]
    callback = authorize(r.headers["location"], "cerea", "/chat/login/callback")
    r = step("GET", callback)  # the chat exchanges the code over its back-channel
    assert r.status_code in (302, 303), r.text[:500]
    user = step("GET", f"{origin}/chat/api/user")
    assert user.status_code == 200, user.text[:300]
    print("CHAT USER:", {k: user.json().get(k) for k in ("email", "username")})
    assert user.json().get("email") == env["AUTHELIA_ADMIN_EMAIL"]

print("E2E_OK")
