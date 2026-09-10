#!/usr/bin/env python3
"""ToolHive in the stack, against the running thing (ADR 0063).

What this can prove without a registered upstream OAuth application: that it
is reachable only through Caddy, that it refuses an unauthenticated caller,
that it accepts the *same* Keycloak access token the gateway accepts — one
identity for models and connectors — and that its remote-OAuth surface is
present in the running API rather than only in the documentation.

What it deliberately does not claim: that dynamic client registration works
under the `/mcp` path prefix. That needs a real upstream provider and a
registered OAuth app, and Stacklok's own troubleshooting names a path segment
in `issuer` as a common cause of failure — so it is an open question recorded
in ADR 0063, not something to assert from a green check here.
"""

import os
import sys

import httpx


def _environment() -> dict[str, str]:
    """The deployment's variables.

    Sourced, not read: `set -a; . deploy/.env; set +a` is how CLAUDE.md says to
    run these, and it is what points them at the https origin rather than at
    localhost. `PYSTINO_ENV` names a file to read instead, for running without
    sourcing — useful from a worktree, where `deploy/.env` does not exist.
    """
    path = os.environ.get("PYSTINO_ENV")
    if not path:
        return dict(os.environ)
    values = dict(os.environ)
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key] = value.strip().strip('"').strip("'")
    return values


env = _environment()


def ca_bundle() -> str | bool:
    """Caddy's root, so this verifies rather than skips.

    The same helper `test_knowledge_live.py` and `test_bill_to_live.py` use.
    `True` when the file is absent, which is the plain-http case where there is
    nothing to verify against.
    """
    for path in ("deploy/tls/caddy-root.crt", env.get("GATEWAY_CA_BUNDLE", "")):
        if path and os.path.exists(path):
            return path
    # Nothing to verify against — the plain-http case.
    return True

BASE = f"https://{env['PUBLIC_HOST']}:{env['HTTPS_PORT']}"
MCP = f"{BASE}/mcp"

ok = 0
fail = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global ok, fail
    if condition:
        ok += 1
        print(f"  ok   {name}")
    else:
        fail += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def access_token(client: httpx.Client) -> str | None:
    """A real access token, by password grant against this deployment's IdP.

    The same token the gateway validates: same issuer, same `pystino-api`
    audience. That it works on both is the point of pointing ToolHive at them
    rather than giving connectors an identity of their own.

    Built from `GATEWAY_OIDC__*` rather than `CHAT_OPENID_CONFIG`, because the
    latter holds JSON and `set -a; . deploy/.env` strips its quotes — so the
    documented way to run this script is exactly the way that breaks it.
    """
    issuer = env.get("GATEWAY_OIDC__ISSUER")
    client_id = env.get("GATEWAY_OIDC__CLIENT_ID")
    client_secret = env.get("GATEWAY_OIDC__CLIENT_SECRET")
    user = env.get("KEYCLOAK_TEST_USER")
    password = env.get("KEYCLOAK_TEST_PASSWORD")
    if not all([issuer, client_id, client_secret, user, password]):
        print("  (skipped: source deploy/.env for GATEWAY_OIDC__* and KEYCLOAK_TEST_*)")
        return None

    r = client.post(
        f"{issuer}/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": client_id,
            "client_secret": client_secret,
            "username": user,
            "password": password,
            "scope": "openid",
        },
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    if r.status_code != 200:
        print(f"  (no token: {r.status_code} {r.text[:160]})")
        return None
    return r.json().get("access_token")


with httpx.Client(verify=ca_bundle(), follow_redirects=True, timeout=60) as c:
    print("reachable only through the proxy:")
    r = c.get(f"{MCP}/api/v1beta/version")
    check("unauthenticated is refused", r.status_code == 401, str(r.status_code))
    r = c.get(f"{MCP}/api/v1beta/version", headers={"Authorization": "Bearer nonsense"})
    check("a junk token is refused", r.status_code == 401, str(r.status_code))

    # Ground rule 4: the service's own port must not be on the routable
    # address. Its absence is the assertion, so a connection error is a pass.
    host, port = env["PUBLIC_HOST"], 8080
    try:
        httpx.get(f"http://{host}:{port}/api/v1beta/version", timeout=5)
        check("port 8080 is not published", False, "it answered on the routable address")
    except Exception:
        check("port 8080 is not published", True)

    print("\none identity for models and connectors:")
    token = access_token(c)
    check("got an access token from the deployment's IdP", token is not None)
    if token:
        auth = {"Authorization": f"Bearer {token}"}
        r = c.get(f"{MCP}/api/v1beta/version", headers=auth)
        check(
            "ToolHive accepts it",
            r.status_code == 200,
            f"{r.status_code}: {r.text[:160]}",
        )
        if r.status_code == 200:
            print(f"       version: {r.text.strip()[:120]}")
        # The same token, on the gateway. If both answer, the claim holds.
        g = c.get(f"{BASE}/v1/models", headers=auth)
        check("the gateway accepts the same token", g.status_code == 200, str(g.status_code))

        print("\nthe remote-OAuth surface is in the running API:")
        r = c.get(f"{MCP}/api/openapi.json", headers=auth)
        check("OpenAPI is served", r.status_code == 200, str(r.status_code))
        if r.status_code == 200:
            spec = r.json()
            schemas = spec.get("components", {}).get("schemas", {})
            create = schemas.get("pkg_api_v1.createRequest", {}).get("properties", {})
            check(
                "a workload can be a remote URL",
                "url" in create,
                "no `url` field — remote servers would need a local container",
            )
            check(
                "and can carry an OAuth config",
                "oauth_config" in create,
                "no `oauth_config` — this is the assurance the decision rested on",
            )
            oauth = schemas.get("pkg_api_v1.remoteOAuthConfig", {}).get("properties", {})
            for field, why in [
                ("issuer", "OIDC discovery against the connector's own provider"),
                ("use_pkce", "PKCE"),
                ("resource", "RFC 8707 resource indicator, which MCP requires"),
                ("client_secret", "a secret reference rather than a literal"),
            ]:
                check(f"remoteOAuthConfig carries `{field}` — {why}", field in oauth)

    print("\nworkloads list cleanly on a fresh instance:")
    if token:
        r = c.get(f"{MCP}/api/v1beta/workloads", headers={"Authorization": f"Bearer {token}"})
        check("the workloads endpoint answers", r.status_code == 200, str(r.status_code))

print(f"\n{ok} ok, {fail} failed")
print(
    "\nNot proven here: dynamic client registration under the /mcp prefix. "
    "See ADR 0063 — it needs a registered upstream OAuth app, and a path "
    "segment in `issuer` is a documented cause of failure."
)
sys.exit(1 if fail else 0)
