#!/usr/bin/env python3
"""Bring your own key (docs/byok.md) against the running compose stack.

What the unit suite cannot reach, and why this is a live script:

* **The network guard on real sockets.** The unit suite injects a resolver. Here
  the gateway resolves real names inside a real compose network: its own
  service names, its own subnets, and a real endpoint outside them.
* **The ledger on PostgreSQL**: `usage_records.byok` and the absence of a
  billing group, read straight from the database, and the quota counters (real
  Valkey) not moving.
* **Capability on a real sign-in**: a member who holds `byok_allowed` through a
  group, and one who does not, each signed in through the bundled Authelia.

An honest note on the upstream. A private provider may not be reached on the
compose network (its service names and subnets stay refused even with
`byok_private_networks` on), so the compose `fake-upstream` cannot stand in for
a member's provider. This script starts a second copy of the same fake
(`scripts/_fake_upstream.py`) on the *host's* LAN address, which is exactly the
shape of a member's lab endpoint: private, outside the compose subnets. That
needs `byok_private_networks` on, and the script sets it through the policy API
and puts it back. Set `PYSTINO_LIVE_BYOK_UPSTREAM` (for example
`http://10.0.0.5:8082/v1`) to use an endpoint you started yourself.

Usage:
    docker compose --env-file deploy/.env \\
      -f deploy/compose.yaml -f deploy/dev/smoke.yml up -d --wait
    set -a; . deploy/.env; set +a; python3 scripts/test_byok_live.py

Needs the bundled Authelia (it provisions two throwaway non-admin logins, and
the `live-check-user` the other scripts use) and `PYSTINO_LIVE_ADMIN_PASSWORD`.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

import live_session
from live_session import GATEWAY, admin_credentials, check, login, request, skip

ROOT = Path(__file__).resolve().parent.parent

FAILURES: list[str] = []

GROUP = "byok-live"
OTHER_USER = "live-nobyok-user"
PROVIDER = "byok-live-lab"
MODEL = "byok-live-model"
UPSTREAM_PORT = int(os.environ.get("PYSTINO_LIVE_BYOK_PORT", "8082"))

STREAMED_BYOK = (
    f"select count(*) from usage_records where model_name = 'my/{MODEL}' "  # noqa: S608
    "and byok and group_id is null and streamed;"
)

COMPOSE = [
    "docker",
    "compose",
    "--env-file",
    "deploy/.env",
    "-f",
    "deploy/compose.yaml",
    "-f",
    "deploy/dev/smoke.yml",
]


def expect(label: str, condition: bool, detail: str = "") -> bool:
    ok = check(label, condition, detail)
    if not ok:
        FAILURES.append(label)
    return ok


def api(opener: Any, path: str, **kwargs: Any) -> tuple[int, Any]:
    status, _, body = request(opener, f"{GATEWAY}{path}", **kwargs)
    try:
        return status, json.loads(body)
    except (ValueError, TypeError):
        return status, body.decode("utf-8", "replace")


def code_of(body: Any) -> str:
    """The machine code of an error envelope (`{"error": {"code": …}}`)."""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("code", ""))
    return ""


def message_of(body: Any) -> str:
    """The human message of an error envelope, shortened for a log line."""
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return str(body["error"].get("message", ""))[:140]
    return str(body)[:140]


def compose(*args: str, check_rc: bool = True) -> str:
    result = subprocess.run(  # noqa: S603
        [*COMPOSE, *args],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        check=False,
        env={**os.environ, "PYSTINO_SRC": os.environ.get("PYSTINO_SRC", str(ROOT))},
    )
    if check_rc and result.returncode != 0:
        raise SystemExit(
            f"docker compose {' '.join(args)} failed:\n"
            f"{(result.stderr or result.stdout).strip()[:400]}"
        )
    return result.stdout.strip()


def sql(query: str) -> str:
    """Read straight from PostgreSQL (see test_providers_live.sql for why it is loud)."""
    return compose(
        "exec", "-T", "postgres", "psql", "-U", "gateway", "-d", "gateway", "-tAc", query
    )


def v1(secret: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    """A `/v1` request over bare HTTP: it authenticates from the bearer alone."""
    headers = {"authorization": f"Bearer {secret}", "accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["content-type"] = "application/json"
    req = urllib.request.Request(f"{GATEWAY}{path}", data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw.decode("utf-8", "replace")


def chat(secret: str, model: str, text: str = "ping") -> tuple[int, Any]:
    return v1(
        secret,
        "/v1/chat/completions",
        {"model": model, "messages": [{"role": "user", "content": text}]},
    )


def lan_address() -> str | None:
    """The address this host reaches the outside through, or None (no route)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1: no packet is sent
        return str(probe.getsockname()[0])
    except OSError:
        return None
    finally:
        probe.close()


@contextlib.contextmanager
def host_upstream() -> Iterator[str | None]:
    """A fake upstream on the host's LAN address; yields its `/v1` URL, or None."""
    given = os.environ.get("PYSTINO_LIVE_BYOK_UPSTREAM", "")
    if given:
        yield given.rstrip("/")
        return
    address = lan_address()
    if address is None or address.startswith("127."):
        yield None
        return
    proc = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-m",
            "uvicorn",
            "_fake_upstream:app",
            "--host",
            address,
            "--port",
            str(UPSTREAM_PORT),
            "--log-level",
            "warning",
        ],
        cwd=str(ROOT / "scripts"),
        stdout=subprocess.DEVNULL,  # the fake prints every request it receives
    )
    try:
        for _ in range(50):
            try:
                with socket.create_connection((address, UPSTREAM_PORT), timeout=1):
                    break
            except OSError:
                time.sleep(0.2)
        else:
            yield None
            return
        yield f"http://{address}:{UPSTREAM_PORT}/v1"
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)


def provision(admin: Any, login_name: str) -> tuple[str, str] | None:
    """Create (or re-key) a throwaway non-admin login, as live_session does for its own."""
    email = f"{login_name}@example.org"
    status, found = api(admin, f"/api/admin/users?q={email}")
    existing = next(
        (row for row in found.get("items", []) if row.get("email") == email)
        if status == 200 and isinstance(found, dict)
        else iter(()),
        None,
    )
    if existing is not None:
        status, body = api(
            admin, f"/api/admin/users/{existing['id']}/reset-password", method="POST"
        )
    else:
        status, body = api(
            admin,
            "/api/admin/users",
            method="POST",
            json_body={"login": login_name, "display_name": login_name, "email": email},
        )
    if status not in (200, 201) or not body.get("password"):
        return None
    return login_name, str(body["password"])


def patient_login(username: str, password: str, *, wait: float = 8) -> Any:
    """Sign in after Authelia has picked a freshly written login up.

    Its file backend reloads a few seconds after the console API writes the
    user, and a refused password counts toward its brute-force ban (three in
    two minutes, then five minutes), so this waits once for the reload rather
    than retrying in a loop.
    """
    time.sleep(wait)
    return login(username, password)


def set_policy(admin: Any, **fields: Any) -> dict[str, Any]:
    status, body = api(admin, "/api/admin/oidc/policy", method="PUT", json_body=fields)
    if status != 200:
        raise SystemExit(f"PUT /api/admin/oidc/policy {fields} answered HTTP {status}: {body}")
    # The answering worker refreshes at once; the others within the poll interval.
    time.sleep(float(body.get("propagation_seconds", 0)) + 0.5)
    return dict(body)


def mint_key(opener: Any, name: str) -> str | None:
    status, created = api(opener, "/api/me/keys", method="POST", json_body={"name": name})
    if status != 201:
        expect(f"minted an API key ({name})", False, f"HTTP {status}: {created}")
        return None
    return str(created["secret"])


def usage_doc(secret: str) -> dict[str, Any]:
    status, body = v1(secret, "/v1/pystino/usage")
    if status != 200 or not isinstance(body, dict):
        raise SystemExit(f"GET /v1/pystino/usage answered HTTP {status}: {body}")
    return body


def container_addresses(service: str) -> list[str]:
    """The IPv4 addresses a compose service's container has, from the engine."""
    cid = compose("ps", "-q", service)
    if not cid:
        return []
    out = subprocess.run(  # noqa: S603
        [
            shutil.which("docker") or "docker",
            "inspect",
            "-f",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{.Gateway}} {{end}}",
            cid.splitlines()[0],
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return [a for a in out.stdout.split() if a]


def refused(
    user: Any, base_url: str, label: str, *, expect_code: str = "provider_url_blocked"
) -> None:
    """A create the guard must refuse with a 400 naming the rule."""
    status, body = api(
        user,
        "/api/me/providers",
        method="POST",
        json_body={"name": "netguard-probe", "base_url": base_url, "api_key": "sk-probe"},
    )
    if status == 201:  # never leave a provider behind that the guard let through
        api(user, f"/api/me/providers/{body['id']}", method="DELETE")
    expect(
        f"refused: {label}",
        status == 400 and code_of(body) == expect_code,
        f"HTTP {status} {code_of(body)}: {str(body)[:160]}" if status != 400 else "",
    )


def main() -> int:
    print(f"gateway: {GATEWAY}\n")

    credentials = admin_credentials()
    if credentials is None:
        print("FAILED: PYSTINO_LIVE_ADMIN_PASSWORD is not set (see scripts/live_session.py)")
        return 1
    admin = login(*credentials)
    if admin is None:
        return 1

    member_credentials = live_session.user_credentials()
    # Authelia's file watcher can miss a second write that follows the first
    # within a few seconds; the second login would then be refused as unknown.
    time.sleep(6)
    other_credentials = (
        provision(admin, OTHER_USER)
        if os.environ.get("PYSTINO_LIVE_AUTO_USER", "1") != "0"
        else None
    )
    if member_credentials is None or other_credentials is None:
        skip(
            "everything",
            "this needs two non-admin members: the bundled Authelia (auto-provisioned), "
            "or PYSTINO_LIVE_USER/_PASSWORD and a second login",
        )
        return 0
    member = patient_login(*member_credentials)
    other = patient_login(*other_credentials, wait=0)
    if member is None or other is None:
        return 1

    _, member_me = api(member, "/api/me")
    _, other_me = api(other, "/api/me")
    _, policy_before = api(admin, "/api/admin/oidc/policy")
    state: dict[str, Any] = {"group": None, "rule": None, "provider": None}

    try:
        with host_upstream() as upstream:
            if upstream is None:
                skip("everything", "no non-loopback host address to run a fake upstream on")
                return 0
            return run(admin, member, other, member_me, other_me, upstream, state)
    finally:
        cleanup(admin, member, state, policy_before, member_me, other_me)


def run(
    admin: Any,
    member: Any,
    other: Any,
    member_me: dict[str, Any],
    other_me: dict[str, Any],
    upstream: str,
    state: dict[str, Any],
) -> int:
    member_key = mint_key(member, "byok-live-member")
    other_key = mint_key(other, "byok-live-other")
    if member_key is None or other_key is None:
        return 1

    print("=== without the capability: refused, on every surface ===")
    set_policy(admin, byok_private_networks=False)
    expect(
        "/api/me reports byok false for the member",
        member_me.get("capabilities", {}).get("byok") is False,
        str(member_me.get("capabilities")),
    )
    for who, opener in (("member (not granted yet)", member), ("other member", other)):
        for method, path in (("GET", "/api/me/providers"), ("GET", "/api/me/models")):
            status, body = api(opener, path, method=method)
            expect(
                f"{who}: {method} {path} is 403 byok_not_allowed",
                status == 403 and code_of(body) == "byok_not_allowed",
                f"HTTP {status} {code_of(body)}",
            )
        status, body = api(
            opener,
            "/api/me/providers",
            method="POST",
            json_body={"name": "nope", "base_url": "https://api.example.com/v1", "api_key": "k"},
        )
        expect(
            f"{who}: creating a provider is 403 byok_not_allowed",
            status == 403 and code_of(body) == "byok_not_allowed",
            f"HTTP {status} {code_of(body)}",
        )
    for who, secret in (("member", member_key), ("other member", other_key)):
        status, body = chat(secret, f"my/{MODEL}")
        expect(
            f"{who}: my/{MODEL} on /v1 is 403 byok_not_allowed",
            status == 403 and code_of(body) == "byok_not_allowed",
            f"HTTP {status} {code_of(body)}",
        )

    print("\n=== 1. an administrator grants byok_allowed to a group the member is in ===")
    status, groups = api(admin, "/api/admin/groups?limit=200")
    group = next((g for g in groups["items"] if g["name"] == GROUP), None)
    if group is None:
        status, group = api(admin, "/api/admin/groups", method="POST", json_body={"name": GROUP})
        expect("the group is created", status == 201, f"HTTP {status}: {group}")
    state["group"] = group["id"]
    expect("a new group does not carry byok (default off)", group.get("byok_allowed") is False)
    status, body = api(
        admin,
        f"/api/admin/groups/{group['id']}/members",
        method="POST",
        json_body={"user_id": member_me["id"]},
    )
    expect("the member joins it", status in (204, 400), f"HTTP {status}: {body}")
    status, body = api(
        admin,
        f"/api/admin/groups/{group['id']}",
        method="PATCH",
        json_body={"byok_allowed": True},
    )
    expect("byok_allowed is set on the group", status == 200 and body["byok_allowed"] is True)
    _, now = api(member, "/api/me")
    expect(
        "the member's /api/me now reports byok",
        now.get("capabilities", {}).get("byok") is True,
        str(now.get("capabilities")),
    )
    _, now_other = api(other, "/api/me")
    expect(
        "the other member's does not",
        now_other.get("capabilities", {}).get("byok") is False,
        str(now_other.get("capabilities")),
    )

    print("\n=== 2. the member adds a private provider (the guard is on) ===")
    base = {"name": PROVIDER, "base_url": upstream, "api_key": "sk-byok-live-secret-0123"}
    status, body = api(member, "/api/me/providers", method="POST", json_body=base)
    expect(
        "a host on the LAN is refused while private networks are off",
        status == 400 and code_of(body) == "provider_url_blocked",
        f"HTTP {status} {code_of(body)}: {str(body)[:200]}",
    )
    print(f"  {body['error']['message'] if isinstance(body, dict) and 'error' in body else body}")

    print("\n  -> an administrator allows private networks (a lab's own endpoint)")
    policy = set_policy(admin, byok_private_networks=True)
    expect("the policy says so", policy.get("byok_private_networks") is True)
    status, provider = api(member, "/api/me/providers", method="POST", json_body=base)
    if not expect("the provider is created", status == 201, f"HTTP {status}: {provider}"):
        return 1
    state["provider"] = provider["id"]
    expect(
        "its key is masked and never returned",
        provider.get("has_api_key") is True
        and "sk-byok-live-secret-0123" not in json.dumps(provider),
        str(provider.get("api_key_hint")),
    )
    stored = sql(
        f"select api_key_encrypted from providers where id = '{provider['id']}';"  # noqa: S608
    )
    expect(
        "the key is ciphertext in PostgreSQL",
        stored.startswith("v1:") and "sk-byok-live-secret" not in stored,
        stored[:12],
    )
    status, probe = api(member, f"/api/me/providers/{provider['id']}/test", method="POST")
    expect(
        "the connection test reaches the endpoint",
        status == 200 and probe.get("ok") is True,
        str(probe),
    )

    status, model = api(
        member,
        "/api/me/models",
        method="POST",
        json_body={"name": MODEL, "upstream_model": "smoke-model", "provider_id": provider["id"]},
    )
    if not expect("a model is created on it", status == 201, f"HTTP {status}: {model}"):
        return 1
    status, _ = api(
        member,
        f"/api/me/models/{model['id']}/prices",
        method="POST",
        json_body={"input_per_mtok": "1", "output_per_mtok": "2", "currency": "EUR"},
    )
    expect("priced in EUR at the member's own rows", status == 201, f"HTTP {status}")

    status, _bad = api(
        member,
        "/api/me/models",
        method="POST",
        json_body={"name": "my/x", "upstream_model": "m", "provider_id": provider["id"]},
    )
    expect("a stored name may not begin with my/", status == 400, f"HTTP {status}")

    print("\n=== 3. my/<name> through /v1: usage is recorded, nothing is charged ===")
    # A tiny cap on the member's group: a deployment model would be refused the
    # moment it was spent, and a BYOK request must pass regardless.
    status, rule = api(
        admin,
        "/api/admin/limits",
        method="POST",
        json_body={
            "name": "byok-live cap",
            "scope": "group",
            "scope_id": group["id"],
            "metric": "cost",
            "period": "month",
            "limit_value": "0.01",
            "applies_to": "aggregate",
        },
    )
    expect("a EUR 0.01 cap on the member's group", status in (201, 409), f"HTTP {status}: {rule}")
    if status == 201:
        state["rule"] = rule["id"]

    before = usage_doc(member_key)
    before_rows = sql("select count(*) from usage_records where byok;")
    status, models = v1(member_key, "/v1/models")
    mine = [m for m in models.get("data", []) if m["id"] == f"my/{MODEL}"]
    expect(
        f"/v1/models lists my/{MODEL} with byok true",
        status == 200 and len(mine) == 1 and mine[0].get("byok") is True,
        str([m["id"] for m in models.get("data", [])]),
    )
    status, models_other = v1(other_key, "/v1/models")
    expect(
        "another member's /v1/models does not list it",
        all(not str(m["id"]).startswith("my/") for m in models_other.get("data", [])),
    )

    status, answer = chat(member_key, f"my/{MODEL}")
    expect("the completion succeeds", status == 200, f"HTTP {status}: {str(answer)[:200]}")
    expect(
        "the response names the model the caller sent",
        isinstance(answer, dict) and answer.get("model") == f"my/{MODEL}",
        str(answer.get("model") if isinstance(answer, dict) else answer),
    )
    time.sleep(1.5)  # the ledger row is written after the response

    row_json = sql(
        "select row_to_json(u) from usage_records u "  # noqa: S608
        f"where model_name = 'my/{MODEL}' order by created_at desc limit 1;"
    )
    row = json.loads(row_json) if row_json else {}
    expect("a usage row exists for my/<name>", bool(row), row_json[:100])
    expect("it is marked byok = true", row.get("byok") is True, str(row.get("byok")))
    expect(
        "it belongs to the member and to no billing group",
        row.get("user_id") == member_me["id"] and row.get("group_id") is None,
        f"user={row.get('user_id')} group={row.get('group_id')}",
    )
    expect(
        "the tokens are recorded",
        row.get("total_tokens") == 1_500_000,
        str(row.get("total_tokens")),
    )
    cost = [v for k, v in row.items() if k.startswith("cost") and v not in (None, 0, "0")]
    fields = {k: v for k, v in row.items() if "cost" in k or "curr" in k}
    print(f"  ledger row cost fields: {fields}")
    expect("the member's own price rows produced a cost", bool(cost), str(cost))

    after = usage_doc(member_key)
    expect(
        "quota-counted usage is unchanged (requests, tokens, cost)",
        all(before["usage"][k] == after["usage"][k] for k in ("requests", "total_tokens", "cost")),
        f"{before['usage']} -> {after['usage']}",
    )
    expect(
        "no limit rule's current value moved",
        [r["current_value"] for r in before["limits"]]
        == [r["current_value"] for r in after["limits"]],
        f"{[r['current_value'] for r in before['limits']]} -> "
        f"{[r['current_value'] for r in after['limits']]}",
    )
    own_before = before["usage"].get("own_keys") or {}
    own_after = after["usage"].get("own_keys") or {}
    expect(
        "it shows as usage.own_keys, in EUR",
        own_after.get("requests", 0) == own_before.get("requests", 0) + 1
        and own_after.get("total_tokens", 0) == own_before.get("total_tokens", 0) + 1_500_000
        and own_after.get("currency") == "EUR",
        f"{own_before} -> {own_after}",
    )
    expect(
        "exactly one byok row was added",
        int(sql("select count(*) from usage_records where byok;")) == int(before_rows) + 1,
    )
    status, mine_rows = api(member, "/api/me/requests?own_keys=true")
    expect(
        "the member's own request log has it (own_keys=true)",
        status == 200
        and any(str(r.get("model")) == f"my/{MODEL}" for r in mine_rows.get("items", [])),
        f"HTTP {status}: {str(mine_rows)[:160]}",
    )
    status, admin_rows = api(admin, "/api/admin/requests?limit=200")
    expect(
        "the administrators' request log leaves it out",
        status == 200
        and all(str(r.get("model")) != f"my/{MODEL}" for r in admin_rows.get("items", [])),
        f"HTTP {status}",
    )
    streamed_before = int(sql(STREAMED_BYOK))
    status, _streamed = v1(
        member_key,
        "/v1/chat/completions",
        {
            "model": f"my/{MODEL}",
            "stream": True,
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    expect("a streamed call works too", status == 200, f"HTTP {status}")
    time.sleep(1.5)
    expect(
        "and is also byok, with no group",
        int(sql(STREAMED_BYOK)) == streamed_before + 1,
    )

    print("\n=== 4. the guard refuses this deployment's own host, services and subnets ===")
    print("  -- private networks OFF")
    set_policy(admin, byok_private_networks=False)
    for url, label in (
        ("http://127.0.0.1:9/v1", "loopback 127.0.0.1"),
        ("http://[::1]:9/v1", "loopback ::1"),
        ("http://localhost:8000/v1", "localhost"),
        ("http://169.254.169.254/v1", "the cloud metadata address"),
        ("http://gateway:8000/api/admin", "the gateway service name"),
        ("http://postgres:5432/", "the postgres service name"),
        ("http://valkey:6379/", "the valkey service name"),
        ("http://authelia:9091/", "the authelia service name"),
        ("http://proxy/", "the proxy service name"),
        ("http://fake-upstream:9099/v1", "the fake upstream's service name (a bare host)"),
    ):
        refused(member, url, f"{label} (off)")
    addresses: list[str] = []
    for service in ("gateway", "postgres", "valkey"):
        addresses += container_addresses(service)
    addresses = sorted(set(addresses))
    if addresses:
        for address in addresses:
            refused(
                member, f"http://{address}:8000/v1", f"the gateway's own subnet, {address} (off)"
            )
    else:
        skip("the gateway's own subnets (off)", "could not read the containers' addresses")
    refused(member, "http://user:pass@api.example.com/v1", "credentials in the URL")
    refused(member, "ftp://api.example.com/v1", "a scheme other than http(s)")

    print("  -- private networks ON: the LAN opens, this deployment does not")
    set_policy(admin, byok_private_networks=True)
    for url, label in (
        ("http://127.0.0.1:9/v1", "loopback 127.0.0.1"),
        ("http://[::1]:9/v1", "loopback ::1"),
        ("http://localhost:8000/v1", "localhost"),
        ("http://169.254.169.254/v1", "the cloud metadata address"),
        ("http://gateway:8000/api/admin", "the gateway service name"),
        ("http://postgres:5432/", "the postgres service name"),
        ("http://valkey:6379/", "the valkey service name"),
        ("http://authelia:9091/", "the authelia service name"),
    ):
        refused(member, url, f"{label} (on)")
    for address in addresses:
        refused(member, f"http://{address}:8000/v1", f"the gateway's own subnet, {address} (on)")

    # A compose service the list does not name resolves, inside the gateway, to
    # an address on its own subnet; that must be refused when it connects.
    status, sneaky = api(
        member,
        "/api/me/providers",
        method="POST",
        json_body={
            "name": "netguard-sneaky",
            "base_url": "http://fake-upstream:9099/v1",
            "api_key": "sk-probe",
        },
    )
    if status == 201:
        status, probe = api(member, f"/api/me/providers/{sneaky['id']}/test", method="POST")
        expect(
            "a compose service the list lacks is refused at connect time (on)",
            status == 200 and probe.get("ok") is False,
            f"HTTP {status}: {probe}",
        )
        print(f"  {probe.get('detail') if isinstance(probe, dict) else probe}")
        api(member, f"/api/me/providers/{sneaky['id']}", method="DELETE")
    else:
        expect(
            "a compose service the list lacks is refused (on)",
            status == 400 and code_of(sneaky) == "provider_url_blocked",
            f"HTTP {status}: {sneaky}",
        )

    status, answer = chat(member_key, f"my/{MODEL}")
    expect("the LAN provider still works with private networks on", status == 200, f"HTTP {status}")
    set_policy(admin, byok_private_networks=False)
    status, answer = chat(member_key, f"my/{MODEL}")
    expect(
        "turning private networks off refuses the very next request, cleanly",
        status in (400, 403, 502) and isinstance(answer, dict) and bool(code_of(answer)),
        f"HTTP {status}: {str(answer)[:200]}",
    )
    print(f"  {status} {code_of(answer)}: {message_of(answer)}")
    set_policy(admin, byok_private_networks=True)
    status, _ = chat(member_key, f"my/{MODEL}")
    expect("and turning it back on restores it", status == 200, f"HTTP {status}")

    print("\n=== 5. the administrator sees the provider, disables it, enables it ===")
    status, listing = api(admin, "/api/admin/byok/providers?limit=200")
    mine_p = next((p for p in listing.get("items", []) if p["id"] == provider["id"]), None)
    expect("the provider is in the administrators' list", status == 200 and mine_p is not None)
    if mine_p is None:
        return 1
    expect(
        "with its owner, host and usage",
        mine_p["owner"]["email"] == member_me["email"]
        and mine_p["base_url_host"] in upstream
        and mine_p["usage"]["requests"] >= 2,
        str({k: mine_p[k] for k in ("owner", "base_url_host", "usage")}),
    )
    expect(
        "and never the credential",
        "sk-byok-live-secret" not in json.dumps(listing) and "api_key_hint" not in mine_p,
    )
    status, models_listing = api(admin, "/api/admin/byok/models?limit=200")
    mine_m = next((m for m in models_listing.get("items", []) if m["id"] == model["id"]), None)
    expect("the model is listed too", status == 200 and mine_m is not None)
    status, plain = api(admin, "/api/admin/providers?limit=200")
    expect(
        "the ordinary admin catalogue does not list it",
        status == 200 and all(p["id"] != provider["id"] for p in plain.get("items", [])),
    )
    status, _ = api(
        admin,
        f"/api/admin/providers/{provider['id']}",
        method="PATCH",
        json_body={"description": "x"},
    )
    expect("and a private id is a 404 there", status == 404, f"HTTP {status}")

    status, held = api(admin, f"/api/admin/byok/providers/{provider['id']}/disable", method="POST")
    expect("disabled by the administrator", status == 200 and held["admin_disabled"] is True)
    status, answer = chat(member_key, f"my/{MODEL}")
    expect(
        "the member's call now fails cleanly (a client error with a code, no 5xx)",
        400 <= status < 500 and bool(code_of(answer)),
        f"HTTP {status}: {str(answer)[:200]}",
    )
    print(f"  {status} {code_of(answer)}")
    _, models_now = v1(member_key, "/v1/models")
    expect(
        "and it drops out of /v1/models",
        all(m["id"] != f"my/{MODEL}" for m in models_now.get("data", [])),
    )
    status, _ = api(
        member, f"/api/me/providers/{provider['id']}", method="PATCH", json_body={"is_active": True}
    )
    status, answer = chat(member_key, f"my/{MODEL}")
    expect(
        "the owner cannot undo the hold",
        400 <= status < 500,
        f"HTTP {status}",
    )
    status, held = api(admin, f"/api/admin/byok/providers/{provider['id']}/enable", method="POST")
    expect("enabled again by the administrator", status == 200 and held["admin_disabled"] is False)
    status, answer = chat(member_key, f"my/{MODEL}")
    expect("the member's call works again", status == 200, f"HTTP {status}: {str(answer)[:200]}")

    status, held = api(admin, f"/api/admin/byok/models/{model['id']}/disable", method="POST")
    expect("a model can be held on its own", status == 200 and held["admin_disabled"] is True)
    status, answer = chat(member_key, f"my/{MODEL}")
    expect("and its calls fail cleanly", 400 <= status < 500, f"HTTP {status}")
    status, held = api(admin, f"/api/admin/byok/models/{model['id']}/enable", method="POST")
    status, answer = chat(member_key, f"my/{MODEL}")
    expect("released, it works", status == 200, f"HTTP {status}")

    status, audit = api(admin, "/api/admin/audit?action=byok.&limit=50")
    actions = {e["action"] for e in audit.get("items", [])} if status == 200 else set()
    expect(
        "the holds are in the audit log",
        {
            "byok.provider.disable",
            "byok.provider.enable",
            "byok.model.disable",
            "byok.model.enable",
        }
        <= actions,
        str(sorted(actions)),
    )

    print("\n=== 6. a member WITHOUT the capability is refused; rows are private ===")
    status, body = api(
        other,
        f"/api/me/providers/{provider['id']}",
        method="PATCH",
        json_body={"description": "x"},
    )
    expect(
        "the other member cannot touch the provider (403 without the capability)",
        status == 403 and code_of(body) == "byok_not_allowed",
        f"HTTP {status} {code_of(body)}",
    )
    status, body = chat(other_key, f"my/{MODEL}")
    expect(
        "nor call my/<name> (403)",
        status == 403 and code_of(body) == "byok_not_allowed",
        f"HTTP {status} {code_of(body)}",
    )
    # With the capability but not the owner: another member's rows are a 404.
    status, _ = api(
        admin,
        f"/api/admin/groups/{state['group']}/members",
        method="POST",
        json_body={"user_id": other_me["id"]},
    )
    status, body = api(
        other,
        f"/api/me/providers/{provider['id']}",
        method="PATCH",
        json_body={"description": "x"},
    )
    expect(
        "holding the capability, another member's provider id is a 404",
        status == 404,
        f"HTTP {status}",
    )
    status, body = chat(other_key, f"my/{MODEL}")
    expect(
        "and so is their model on /v1 (not found, never a 403)",
        status == 404,
        f"HTTP {status}: {str(body)[:160]}",
    )
    status, _ = api(
        admin, f"/api/admin/groups/{state['group']}/members/{other_me['id']}", method="DELETE"
    )

    print("\n=== 7. losing the capability stops my/<name> and keeps the rows ===")
    api(
        admin,
        f"/api/admin/groups/{state['group']}",
        method="PATCH",
        json_body={"byok_allowed": False},
    )
    status, body = chat(member_key, f"my/{MODEL}")
    expect(
        "the member's my/<name> answers 403 byok_not_allowed",
        status == 403 and code_of(body) == "byok_not_allowed",
        f"HTTP {status} {code_of(body)}",
    )
    status, body = api(member, "/api/me/providers")
    expect("and so does their provider list", status == 403, f"HTTP {status}")
    status, listing = api(admin, "/api/admin/byok/providers?limit=200")
    expect(
        "the rows are kept",
        any(p["id"] == provider["id"] for p in listing.get("items", [])),
    )
    api(
        admin,
        f"/api/admin/groups/{state['group']}",
        method="PATCH",
        json_body={"byok_allowed": True},
    )
    status, _ = chat(member_key, f"my/{MODEL}")
    expect("regained, it works as it was", status == 200, f"HTTP {status}")

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s) — " + "; ".join(FAILURES))
        return 1
    print(
        "OK — BYOK end to end: grant, private provider, my/<name>, ledger, guard, holds, refusal."
    )
    return 0


def cleanup(
    admin: Any,
    member: Any,
    state: dict[str, Any],
    policy_before: Any,
    member_me: dict[str, Any],
    other_me: dict[str, Any],
) -> None:
    """Leave the stack as it was found; every step tolerates the step before it failing."""
    with contextlib.suppress(Exception):
        api(
            admin,
            "/api/admin/oidc/policy",
            method="PUT",
            json_body={
                "byok_private_networks": bool(
                    isinstance(policy_before, dict) and policy_before.get("byok_private_networks")
                )
            },
        )
    with contextlib.suppress(Exception):
        if state.get("rule"):
            api(admin, f"/api/admin/limits/{state['rule']}", method="DELETE")
    with contextlib.suppress(Exception):
        if state.get("provider"):
            _, models = api(member, "/api/me/models?include_inactive=true")
            for m in models.get("items", []):
                api(member, f"/api/me/models/{m['id']}", method="DELETE")
            api(member, f"/api/me/providers/{state['provider']}", method="DELETE")
    with contextlib.suppress(Exception):
        if state.get("group"):
            api(admin, f"/api/admin/groups/{state['group']}", method="DELETE")


if __name__ == "__main__":
    sys.exit(main())
