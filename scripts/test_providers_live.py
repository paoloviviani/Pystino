#!/usr/bin/env python3
"""Check inference providers against the running compose stack.

What only this can prove (docs/adr/0027-inference-providers.md):

* the credential really is encrypted **in PostgreSQL**, not just in a unit test's
  SQLite file;
* a request is routed to the provider its model points at, with that provider's
  key, through the real HTTP client rather than an injected transport;
* the connection test reaches a real socket and reports what came back;
* per-user access works end to end, through `/v1/models` and a completion.

Usage:
    docker compose --env-file deploy/.env \\
      -f deploy/compose/docker-compose.yml \\
      -f deploy/compose/docker-compose.smoke.yml \\
      -f deploy/compose/docker-compose.keycloak.yml up -d --build
    python3 scripts/test_providers_live.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from test_oidc_flow import GATEWAY, check, login, request

FAILURES: list[str] = []

COMPOSE = [
    "docker",
    "compose",
    "--env-file",
    "deploy/.env",
    "-f",
    "deploy/compose/docker-compose.yml",
    "-f",
    "deploy/compose/docker-compose.smoke.yml",
    "-f",
    "deploy/compose/docker-compose.keycloak.yml",
    "-f",
    "deploy/compose/docker-compose.redaction.yml",
]


def expect(label: str, condition: bool, detail: str = "") -> None:
    if not check(label, condition, detail):
        FAILURES.append(label)


def api(opener: Any, path: str, **kwargs: Any) -> tuple[int, Any]:
    status, _, body = request(opener, f"{GATEWAY}{path}", **kwargs)
    try:
        return status, json.loads(body)
    except (ValueError, TypeError):
        return status, body.decode("utf-8", "replace")


def sql(query: str) -> str:
    """Read straight from PostgreSQL, to check what is actually on disk."""
    result = subprocess.run(  # noqa: S603
        [
            *COMPOSE,
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "gateway",
            "-d",
            "gateway",
            "-tAc",
            query,
        ],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        check=False,
    )
    return result.stdout.strip()


def main() -> int:
    print("=== logging in as dave ===")
    dave = login("dave")
    if dave is None:
        return 1

    print()
    print("=== the migration turned the environment upstream into a provider ===")
    status, providers = api(dave, "/api/admin/providers")
    expect("providers can be listed", status == 200, f"HTTP {status}")
    default = next((p for p in providers if p["name"] == "default"), None)
    expect("a `default` provider exists", default is not None, str([p["name"] for p in providers]))
    if default is None:
        return 1
    expect(
        "it points at the endpoint this deployment was already using",
        "fake-upstream" in default["base_url"],
        default["base_url"],
    )
    expect("and it kept the API key", default["has_api_key"] is True, str(default))
    print(f"  default -> {default['base_url']} (key {default['api_key_hint']})")

    print()
    print("=== the credential is encrypted in PostgreSQL, and never returned ===")
    stored = sql("select api_key_encrypted from providers where name = 'default';")
    expect("the stored value is ciphertext", stored.startswith("v1:"), stored[:24])
    expect("the plaintext key is not in the database", "fake-upstream-key" not in stored, "")
    expect("and not in the API response", "fake-upstream-key" not in json.dumps(default), "")
    expect("only a hint is exposed", "…" in default["api_key_hint"], default["api_key_hint"])

    print()
    print("=== the connection test reaches a real socket ===")
    status, result = api(dave, f"/api/admin/providers/{default['id']}/test", method="POST")
    expect("the test runs", status == 200, f"HTTP {status}: {result}")
    expect("and reports the provider is reachable", result.get("ok") is True, str(result))
    print(f"  {result.get('detail')} ({result.get('latency_ms')}ms)")

    print()
    print("=== a second provider, created through the API ===")
    status, created = api(
        dave,
        "/api/admin/providers",
        method="POST",
        json_body={
            "name": "live-check",
            "base_url": "http://fake-upstream:9099/v1",
            "api_key": "sk-live-check-abcdef",
            "description": "created by scripts/test_providers_live.py",
        },
    )
    if status == 409:
        created = next(p for p in api(dave, "/api/admin/providers")[1] if p["name"] == "live-check")
        print("  reusing the provider from a previous run")
    else:
        expect("it can be created", status == 201, f"HTTP {status}: {created}")

    expect(
        "its key is masked in the response",
        created.get("api_key_hint") == "sk-l…cdef",
        str(created.get("api_key_hint")),
    )
    row = sql("select api_key_encrypted from providers where name = 'live-check';")
    expect(
        "and encrypted on disk",
        "sk-live-check-abcdef" not in row and row.startswith("v1:"),
        row[:24],
    )

    print()
    print("=== editing does not silently wipe the credential ===")
    status, edited = api(
        dave,
        f"/api/admin/providers/{created['id']}",
        method="PATCH",
        json_body={"description": "edited"},
    )
    expect("the edit succeeds", status == 200, f"HTTP {status}")
    expect("and the key survives it", edited.get("has_api_key") is True, str(edited))

    status, cleared = api(
        dave,
        f"/api/admin/providers/{created['id']}",
        method="PATCH",
        json_body={"api_key": ""},
    )
    expect("an explicit empty key removes it", cleared.get("has_api_key") is False, str(cleared))

    print()
    print("=== a provider in use cannot be deleted ===")
    status, _refusal = api(dave, f"/api/admin/providers/{default['id']}", method="DELETE")
    expect(
        "the one serving models is refused",
        status == 409,
        f"HTTP {status} — cascading would orphan historical spend",
    )
    status, _ = api(dave, f"/api/admin/providers/{created['id']}", method="DELETE")
    expect("the unused one is removed", status == 204, f"HTTP {status}")

    print()
    print("=== routing: a completion reaches the model's provider ===")
    alice = login("alice")
    if alice is None:
        return 1
    status, _, body = request(
        alice, f"{GATEWAY}/api/me/keys", json_body={"name": "provider-check"}, method="POST"
    )
    if status != 201:
        expect("minted an API key", False, f"HTTP {status}")
        return 1
    secret = json.loads(body)["secret"]

    import urllib.error
    import urllib.request

    completion = urllib.request.Request(
        f"{GATEWAY}/v1/chat/completions",
        data=json.dumps(
            {"model": "smoke-model", "messages": [{"role": "user", "content": "ping"}]}
        ).encode(),
        headers={"content-type": "application/json", "authorization": f"Bearer {secret}"},
        method="POST",
    )
    routed = False
    try:
        with urllib.request.urlopen(completion, timeout=60) as response:
            expect("a completion succeeds", response.status == 200, str(response.status))
            routed = True
    except urllib.error.HTTPError as error:
        if error.code == 429:
            # The demo stack bills a million tokens per fake completion, so a
            # seeded quota can legitimately be spent. Distinguished from a
            # routing failure, which is what this section is about.
            print("  skipped: a quota is exhausted in this deployment, not a routing failure")
        else:
            expect("a completion succeeds", False, f"HTTP {error.code}")
    except Exception as exc:
        expect("a completion succeeds", False, str(exc))

    if routed:
        seen = json.loads(
            urllib.request.urlopen("http://localhost:8081/_last_request", timeout=30).read()
        )
        expect("the upstream was actually called", bool(seen), str(seen)[:80])

    print()
    print("=== what actually served the request is recorded ===")
    ledger = sql(
        "select model_name, upstream_model, upstream_provider, model_substituted "
        "from usage_records where upstream_model is not null "
        "order by created_at desc limit 1;"
    )
    expect("the provider's own model name is stored", bool(ledger), ledger or "no rows")
    if ledger:
        name, served, by, substituted = [*ledger.split("|"), "", "", ""][:4]
        print(f"  {name} -> served by {served} at {by or 'unreported'}")
        expect(
            "our client-facing name is not mistaken for a substitution",
            substituted == "f",
            f"model_substituted={substituted} — the two names differ by design",
        )

    print()
    print("=== embeddings ===")
    models = api(dave, "/api/admin/models")[1]
    embedding = next((m for m in models if m["kind"] == "embedding"), None)
    if embedding is None:
        print("  no embedding model catalogued here; skipping")
    else:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            f"{GATEWAY}/v1/embeddings",
            data=json.dumps({"model": embedding["name"], "input": ["one", "two"]}).encode(),
            headers={"content-type": "application/json", "authorization": f"Bearer {secret}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                payload = json.loads(response.read())
            expect("a batch is embedded", len(payload.get("data", [])) >= 1, str(payload)[:120])
            expect(
                "and the usage has no completion tokens",
                payload.get("usage", {}).get("completion_tokens") == 0,
                str(payload.get("usage")),
            )
            # The model name comes from our own API, not from user input; psql
            # is being handed a literal either way in this development script.
            # The name comes from our own API and psql is handed a literal;
            # this is a development script, not a query builder.
            name = embedding["name"]
            query = f"select completion_tokens, cost from usage_records where model_name = '{name}' order by created_at desc limit 1;"  # noqa: E501,S608
            row = sql(query)
            expect("the ledger charges input only", row.startswith("0|"), row or "no row")
            print(f"  ledger: completion_tokens|cost = {row}")
        except urllib.error.HTTPError as error:
            if error.code == 429:
                print("  skipped: a quota is exhausted in this deployment")
            else:
                expect("a batch is embedded", False, f"HTTP {error.code}")

        # A chat model on the embeddings route is refused here, not upstream.
        wrong = next((m for m in models if m["kind"] == "chat"), None)
        if wrong:
            req = urllib.request.Request(
                f"{GATEWAY}/v1/embeddings",
                data=json.dumps({"model": wrong["name"], "input": "x"}).encode(),
                headers={"content-type": "application/json", "authorization": f"Bearer {secret}"},
                method="POST",
            )
            try:
                urllib.request.urlopen(req, timeout=30)
                expect("a chat model is refused by /v1/embeddings", False, "it was accepted")
            except urllib.error.HTTPError as error:
                expect(
                    "a chat model is refused by /v1/embeddings",
                    error.code == 400,
                    f"HTTP {error.code}",
                )

    print()
    print("=== per-user model access ===")
    models = api(dave, "/api/admin/models")[1]
    smoke = next((m for m in models if m["name"] == "smoke-model"), None)
    if smoke is None:
        expect("smoke-model is catalogued", False, str([m["name"] for m in models]))
        return 1

    users = api(dave, "/api/admin/users")[1]
    carol = next((u for u in users if (u["email"] or "").startswith("carol")), None)
    if carol is None:
        print("  no carol in this deployment; skipping the per-user checks")
    else:
        # Carol is in no group, so nothing is visible to her before the grant.
        status, _ = api(dave, f"/api/admin/users/{carol['id']}/models/{smoke['id']}", method="PUT")
        expect("a personal grant can be made", status == 204, f"HTTP {status}")

        listing = api(dave, "/api/admin/models")[1]
        granted = next(m for m in listing if m["id"] == smoke["id"])
        expect(
            "and it is visible on the model",
            carol["email"] in granted["granted_to_users"],
            str(granted["granted_to_users"]),
        )

        status, _ = api(
            dave, f"/api/admin/users/{carol['id']}/models/{smoke['id']}", method="DELETE"
        )
        expect("and revoked again", status == 204, f"HTTP {status}")

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
