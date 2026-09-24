"""End-to-end sign-in through a Pystino stack (used by stack.yml and by hand): proxy → gateway → Authelia → back.

Browser-shaped: every request goes to the public origin (resolved to the local
proxy), cookies are kept per host, redirects are followed by hand so each hop
is visible. Proves: the proxy routes, Authelia serves its portal under
/authelia, the gateway's OIDC client exchanges the code over the internal
back-channel (no CA bundle anywhere), and the bootstrap address becomes admin.
"""
import re
import sys
import urllib.parse

import httpx

if len(sys.argv) != 3:
    sys.exit("usage: e2e_login.py <deploy dir> <first user password>")
deploy = sys.argv[1]
env = {}
for line in open(f"{deploy}/.env"):
    if "=" in line and not line.startswith("#"):
        k, v = line.rstrip("\n").split("=", 1)
        env[k] = v.strip("'")
password = sys.argv[2]
origin = env["PUBLIC_ORIGIN"]
public = urllib.parse.urlsplit(origin)
port = public.port or 443


def rewrite(request: httpx.Request) -> None:
    if request.url.host == public.hostname:
        request.url = request.url.copy_with(host="127.0.0.1", port=port)
        request.headers["host"] = public.netloc
        request.extensions["sni_hostname"] = public.hostname


client = httpx.Client(verify=False, follow_redirects=False, timeout=30,
                      event_hooks={"request": [rewrite]})


def step(method, url, **kw):
    r = client.request(method, url, **kw)
    print(f"{method} {urllib.parse.urlsplit(url).path[:60]} -> {r.status_code} {r.headers.get('location', '')[:100]}")
    return r


r = step("GET", f"{origin}/auth/login?provider=default")
assert r.status_code in (302, 303, 307), r.text[:300]
auth_url = r.headers["location"]
assert auth_url.startswith(f"{origin}/authelia/"), auth_url
r = step("GET", auth_url)
loc = r.headers["location"]
flow_id = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(loc).query)).get("flow_id")
r = step("POST", f"{origin}/authelia/api/firstfactor",
         json={"username": env["AUTHELIA_ADMIN_USER"], "password": password,
               "keepMeLoggedIn": False, "flow": "openid_connect", "flowID": flow_id},
         headers={"origin": origin})
assert r.status_code == 200, r.text[:300]
for _ in range(5):
    r = step("GET", auth_url)
    loc = r.headers.get("location", "")
    if "/auth/callback/" in loc:
        break
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(loc).query))
    if "consent" in loc and q.get("flow_id"):
        g = step("GET", f"{origin}/authelia/api/oidc/consent?flow_id={q['flow_id']}", headers={"origin": origin})
        claims = g.json().get("data", {}).get("claims") or []
        c = step("POST", f"{origin}/authelia/api/oidc/consent",
                 json={"flow_id": q["flow_id"], "client_id": "pystino-console", "consent": True,
                       "pre_configure": True, "claims": claims}, headers={"origin": origin})
        auth_url = c.json()["data"]["redirect_uri"]
assert "/auth/callback/" in loc and "code=" in loc, loc
r = step("GET", loc)  # the gateway exchanges the code over the back-channel here
assert r.status_code in (302, 303), r.text[:500]
me = step("GET", f"{origin}/api/me")
assert me.status_code == 200, me.text[:300]
body = me.json()
print("ME:", {k: body.get(k) for k in ("email", "is_admin", "groups")})
assert body.get("is_admin") is True, "bootstrap admin was not promoted"
print("E2E_OK")
