#!/usr/bin/env python3
"""Create the Notion MCP connector in ToolHive and print its authorize URL.

    set -a; . deploy/.env; set +a
    ./scripts/setup_notion_connector.py

Notion's MCP advertises `registration_endpoint` *and*
`client_id_metadata_document_supported`, and ToolHive prefers the latter — so
the `client_id` it presents is `https://toolhive.dev/oauth/client-metadata.json`
and **no application has to be registered anywhere**, by us or by Notion. That
also means our own issuer is not involved in this flow at all, which sidesteps
the path-prefix question ADR 0063 raises about the embedded authorization
server.

Two things about authorising it, both measured rather than assumed:

* **the redirect URI is `http://localhost:<port>/callback`.** There is a
  `--remote-auth-callback-port` flag and no callback-*host* flag, so the URI
  names localhost relative to the container. The compose overlay publishes that
  port on the host's loopback; a person authorising from another machine needs
  an SSH tunnel to it, exactly as the README describes for the console:

      ssh -L 8666:127.0.0.1:8666 <user>@130.192.84.103

* **the token is not persisted.** `encrypted` refuses in a container with
  "OS keyring is not available", `environment` is read-only and so cannot hold
  a token ToolHive obtains, and `1password` needs an account. The workload log
  says so itself. An authorisation therefore lasts until ToolHive restarts.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

import httpx

NAME = "notion"
URL = "https://mcp.notion.com/mcp"
ISSUER = "https://mcp.notion.com"


def ca_bundle() -> str | bool:
    path = "deploy/tls/caddy-root.crt"
    return path if os.path.exists(path) else True


def base() -> str:
    host = os.environ.get("PUBLIC_HOST")
    if host:
        return f"https://{host}:{os.environ.get('HTTPS_PORT', '443')}"
    return "http://127.0.0.1:8080"


def token(client: httpx.Client) -> str:
    """An access token for ToolHive's API, from this deployment's IdP.

    `GATEWAY_OIDC__*` rather than `CHAT_OPENID_CONFIG`: the latter holds JSON
    and sourcing `deploy/.env` strips its quotes.
    """
    needed = [
        "GATEWAY_OIDC__ISSUER",
        "GATEWAY_OIDC__CLIENT_ID",
        "GATEWAY_OIDC__CLIENT_SECRET",
        "KEYCLOAK_TEST_USER",
        "KEYCLOAK_TEST_PASSWORD",
    ]
    missing = [name for name in needed if not os.environ.get(name)]
    if missing:
        sys.exit(f"source deploy/.env first — missing {', '.join(missing)}")
    r = client.post(
        f"{os.environ['GATEWAY_OIDC__ISSUER']}/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": os.environ["GATEWAY_OIDC__CLIENT_ID"],
            "client_secret": os.environ["GATEWAY_OIDC__CLIENT_SECRET"],
            "username": os.environ["KEYCLOAK_TEST_USER"],
            "password": os.environ["KEYCLOAK_TEST_PASSWORD"],
            "scope": "openid",
        },
    )
    if r.status_code != 200:
        sys.exit(f"could not get a token: {r.status_code} {r.text[:200]}")
    return r.json()["access_token"]


CONTAINER = os.environ.get("TOOLHIVE_CONTAINER", "llm-platform-toolhive-1")
DOCKER = shutil.which("docker") or "/usr/bin/docker"


def authorize_url() -> str | None:
    """The URL ToolHive is waiting on, from the workload's own log.

    `docker cp` rather than `docker exec`: the image is distroless and has no
    shell to run `cat` with. The last match wins — re-creating the workload
    starts a new flow with a new PKCE challenge, and the stale one would fail.
    """
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "workload.log")
        result = subprocess.run(  # noqa: S603 - argv is constants plus two
        # names from this script's own environment, which is the same trust
        # level as the script. No shell, and nothing here comes from a request.
            # Full path: ruff's S607 objects to resolving it from PATH,
            # and it is right that a script running docker should say
            # which docker.
            [DOCKER, "cp", f"{CONTAINER}:/data/toolhive/logs/{NAME}.log", target],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            print(f"could not read the log: {result.stderr.strip()[:200]}")
            return None
        with open(target, errors="replace") as handle:
            found = re.findall(r"https://mcp\.notion\.com/authorize\?\S+", handle.read())
    return found[-1] if found else None


def notion_advertises(client: httpx.Client) -> dict[str, object]:
    """What Notion says about its own OAuth, read rather than hardcoded.

    Through httpx rather than `urllib`: that endpoint answers **403** to
    urllib's default user agent and 200 to anything browser-ish, and a 403
    there reads like an authorisation problem when it is a user-agent filter.
    """
    r = client.get(f"{ISSUER}/.well-known/oauth-authorization-server")
    r.raise_for_status()
    return r.json()


def main() -> int:
    callback_port = int(os.environ.get("TOOLHIVE_CALLBACK_PORT", "8666"))

    # `--url` only reads the log, so it is safe to run repeatedly while
    # somebody is in the middle of authorising.
    if "--url" in sys.argv:
        url = authorize_url()
        if not url:
            print("no authorize URL in the log — create the connector first")
            return 1
        print(url)
        return 0

    mcp = f"{base()}/mcp"

    with httpx.Client(verify=ca_bundle(), follow_redirects=True, timeout=180) as client:
        meta = notion_advertises(client)
        print("Notion advertises:")
        for key in ("issuer", "registration_endpoint", "code_challenge_methods_supported"):
            print(f"  {key}: {meta.get(key)}")
        cimd = meta.get("client_id_metadata_document_supported")
        print(f"  client_id_metadata_document_supported: {cimd}")

        auth = {"Authorization": f"Bearer {token(client)}"}

        existing = client.get(f"{mcp}/api/v1beta/workloads/{NAME}", headers=auth)
        if existing.status_code == 200:
            print(f"\n`{NAME}` already exists — removing it so this is repeatable")
            client.delete(f"{mcp}/api/v1beta/workloads/{NAME}", headers=auth)

        body = {
            "name": NAME,
            "url": URL,
            "transport": "streamable-http",
            "oauth_config": {
                "issuer": ISSUER,
                "scopes": ["default"],
                "use_pkce": True,
                # RFC 8707. Notion's protected-resource document names itself
                # as the resource, and the MCP spec requires the indicator.
                "resource": ISSUER,
                # Fixed, so the redirect URI is a port somebody can tunnel.
                # Left unset it picks a random one each time.
                "callback_port": callback_port,
                # There is no browser in the container; it logs the URL
                # instead, which is what this script goes and reads.
                "skip_browser": True,
            },
        }
        r = client.post(f"{mcp}/api/v1beta/workloads", headers=auth, json=body)
        print(f"\ncreate `{NAME}` -> {r.status_code}")
        if r.status_code >= 400:
            print(r.text[:400])
            return 1

    host = os.environ.get("PUBLIC_HOST", "the host")
    print(
        f"""
Created. To authorise it:

  1. From your own machine, tunnel the callback port:

         ssh -L {callback_port}:127.0.0.1:{callback_port} <user>@{host}

  2. Read the authorize URL out of the workload's log:

         ./scripts/setup_notion_connector.py --url

  3. Open it, approve, and the redirect lands on the tunnelled port.

The token lives until ToolHive restarts — see this file's header for why.
"""
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
