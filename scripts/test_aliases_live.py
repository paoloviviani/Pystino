#!/usr/bin/env python3
"""Check model aliases against the running compose stack (ADR 0101).

What only this can prove — the unit suite drives the same code against a fake
upstream, but through an injected transport and a session that never crossed
TLS, an IdP or a real proxy:

* an alias is created through the admin API and served through the real proxy,
  with the caller authenticated by an API key minted over TLS;
* **access to the alias is independent of access to its target**: a caller whose
  billing group holds the alias and not the target chats through the alias and
  is refused the target by name. The unit suite asserts both halves; here they
  are asserted against the access predicate that production actually runs;
* the ledger row names the **target** (`model_id`, `model_name`) and keeps the
  alias in `requested_model` — read straight out of PostgreSQL, the way an
  operator reconciling a bill would read it;
* the card in `/v1/models` carries no trace of the target: its name appears
  nowhere in the response body, not even as a provider name;
* deactivating the target makes the alias unavailable: 503 `alias_unavailable`
  on the request, and the card gone from the listing — the unavailability rule
  is a request-time decision, not a visibility filter.

Usage — the same setup as `test_providers_live.py`:

    docker compose --env-file deploy/.env \\
      -f deploy/compose.yaml -f deploy/dev/smoke.yml up -d --wait
    docker compose --env-file deploy/.env -f deploy/compose.yaml -f deploy/dev/smoke.yml \\
      exec -T gateway gateway seed --model smoke-model --upstream-model smoke-model
    set -a; . deploy/.env; set +a; python3 scripts/test_aliases_live.py

The caller is the administrator, whose default billing group must have **no**
grant on `smoke-model`. The seed grants its own group, so on a stack seeded the
documented way this holds; the script checks it and refuses to run otherwise,
rather than granting the target and then wondering why the 404 check passed.

It cleans up after itself: the alias is deleted, which takes its grants and its
route row with it, and the key it minted is revoked.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from live_session import GATEWAY, admin_credentials, check, login
from test_providers_live import api, sql

FAILURES: list[str] = []

ALIAS = "alias-live-check"
TARGET = "smoke-model"


def expect(label: str, condition: bool, detail: str = "") -> None:
    if not check(label, condition, detail):
        FAILURES.append(label)


def key_for(user: Any, name: str) -> str | None:
    status, created = api(user, "/api/me/keys", json_body={"name": name}, method="POST")
    if status != 201:
        expect(f"minted an API key ({name})", False, f"HTTP {status}: {created}")
        return None
    return str(created["secret"])


def call(secret: str, path: str, body: dict[str, Any]) -> tuple[int, Any]:
    """A `/v1` request with an API key, straight over HTTP."""
    req = urllib.request.Request(
        f"{GATEWAY}{path}",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "authorization": f"Bearer {secret}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, raw.decode("utf-8", "replace")


def latest_usage(alias: str) -> list[str]:
    """The newest ledger row for this alias, as PostgreSQL holds it."""
    # Interpolated, as every live script's `sql` call is (see
    # benchmark_live.py): the value is this script's own constant, and the
    # check runs against a throwaway stack.
    query = (
        "SELECT model_name, requested_model, model_id FROM usage_records "  # noqa: S608
        f"WHERE requested_model = '{alias}' ORDER BY created_at DESC LIMIT 1"
    )
    return sql(query).strip().split("|")


def main() -> int:
    print("=== signing in ===")
    credentials = admin_credentials()
    if credentials is None:
        print("FAILED: PYSTINO_LIVE_ADMIN_PASSWORD is not set (see scripts/live_session.py)")
        return 1
    dave = login(*credentials)
    if dave is None:
        return 1

    print()
    print("=== setup: a group with no grant on the target ===")
    models = api(dave, "/api/admin/models?limit=200")[1]["items"]
    target = next((m for m in models if m["name"] == TARGET), None)
    if target is None:
        print(f"  skipped: {TARGET} is not catalogued. Seed it first — see the docstring.")
        return 1

    # The caller's own billing group, from the listing: there is no
    # single-group GET, and the grant list is what this check turns on.
    # (`/api/me` summarises the group without its grants.)
    me = api(dave, "/api/me")[1]
    group = next(
        (
            g
            for g in api(dave, "/api/admin/groups?limit=200")[1]["items"]
            if g["id"] == me["default_billing_group"]["id"]
        ),
        None,
    )
    if group is None:
        print("  skipped: the caller's billing group is not in the admin listing.")
        return 1
    if TARGET in group["models"]:
        print(
            f"  skipped: the caller's group {group['name']!r} already holds {TARGET!r}, so the "
            "404 check could not be honest. Grant the alias to a group without the target."
        )
        return 1

    # The key is minted before the alias exists, so "the alias works" can only
    # be about the alias and not about a key minted after the grant.
    secret = key_for(dave, "aliases-check")
    if secret is None:
        return 1

    print()
    print("=== the alias, through the admin API ===")
    # A previous run that failed between create and cleanup leaves the name
    # behind, and a live check that then 409s forever is no use to anyone. Its
    # name is this script's own constant, so removing it takes nothing an
    # operator made.
    listed = api(dave, "/api/admin/aliases")[1]
    leftover = next((m for m in listed if m.get("name") == ALIAS), None)
    if leftover is not None:
        status, _ = api(dave, f"/api/admin/aliases/{leftover['id']}", method="DELETE")
        expect("removed the previous run's alias", status == 204, f"HTTP {status}")

    status, alias = api(
        dave,
        "/api/admin/aliases",
        json_body={
            "name": ALIAS,
            "display_name": "Alias live check",
            "target_model_id": target["id"],
        },
        method="POST",
    )
    if status != 201:
        expect("created the alias", False, f"HTTP {status}: {alias}")
        return 1
    expect("created the alias", True, f"status {alias['status']}, kind {alias['kind']}")
    expect(
        "its target is the model we pointed it at",
        (alias.get("target") or {}).get("id") == target["id"],
        str(alias.get("target")),
    )

    status, _ = api(dave, f"/api/admin/groups/{group['id']}/models/{alias['id']}", method="PUT")
    expect("granted the alias to the caller's group", status == 204, f"HTTP {status}")
    # From the alias's own response: the model detail endpoint refuses an alias
    # id, which is the check that kept this from working for a while.
    granted = next((a for a in api(dave, "/api/admin/aliases")[1] if a["id"] == alias["id"]), {})
    expect(
        "and the group is on the grant list",
        group["name"] in granted.get("granted_to", []),
        str(granted.get("granted_to")),
    )

    print()
    print("=== through the alias ===")
    status, body = call(
        secret,
        "/v1/chat/completions",
        {"model": ALIAS, "messages": [{"role": "user", "content": "hello there"}]},
    )
    if status == 429:
        print("  skipped: a quota is exhausted in this deployment")
    else:
        expect("a chat through the alias works", status == 200, f"HTTP {status}: {str(body)[:160]}")
        if status == 200:
            expect(
                "and it echoes the name the caller sent",
                body.get("model") == ALIAS,
                str(body.get("model")),
            )
            row = latest_usage(ALIAS)
            expect(
                "the ledger names the target",
                row and row[0] == TARGET,
                f"model_name={row[0] if row else None}",
            )
            expect(
                "and keeps the alias beside it",
                row and row[1] == ALIAS,
                f"requested_model={row[1] if row else None}",
            )
            expect(
                "and the row's model_id is the target's id",
                row and row[2] == target["id"],
                f"model_id={row[2] if row else None}",
            )

    print()
    print("=== the target, by name ===")
    status, refusal = call(
        secret,
        "/v1/chat/completions",
        {"model": TARGET, "messages": [{"role": "user", "content": "hi"}]},
    )
    expect(
        "the target itself is refused — the alias is the grant",
        status == 404 and refusal.get("error", {}).get("code") == "model_not_found",
        f"HTTP {status}: {str(refusal)[:140]}",
    )

    print()
    print("=== the catalogue ===")
    req = urllib.request.Request(
        f"{GATEWAY}/v1/models", headers={"authorization": f"Bearer {secret}"}
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        raw = response.read().decode()
    listing = json.loads(raw)
    cards = {card["id"]: card for card in listing.get("data", [])}
    expect("the alias is listed", ALIAS in cards, str(sorted(cards)))
    if ALIAS in cards:
        card = cards[ALIAS]
        expect(
            "its capabilities are the target's",
            card.get("context_window") == target["context_window"],
            f"context_window={card.get('context_window')} vs {target['context_window']}",
        )
        expect(
            "its own text is the alias's",
            card.get("display_name") == "Alias live check",
            str(card.get("display_name")),
        )
    # The whole body, not just the card: a leak through `owned_by` or an
    # upstream name would be exactly the kind this check exists to catch.
    expect("the target's name appears nowhere in the listing", TARGET not in raw, "")

    print()
    print("=== the target goes away ===")
    status, _ = api(
        dave, f"/api/admin/models/{target['id']}", json_body={"is_active": False}, method="PATCH"
    )
    expect("deactivated the target", status == 200, f"HTTP {status}")

    status, refusal = call(
        secret,
        "/v1/chat/completions",
        {"model": ALIAS, "messages": [{"role": "user", "content": "hello there"}]},
    )
    expect(
        "the alias answers 503 alias_unavailable",
        status == 503 and refusal.get("error", {}).get("code") == "alias_unavailable",
        f"HTTP {status}: {str(refusal)[:140]}",
    )
    expect(
        "with the ADR's message",
        refusal.get("error", {}).get("message")
        == f"The model alias '{ALIAS}' has no available target.",
        str(refusal.get("error", {}).get("message")),
    )

    req = urllib.request.Request(
        f"{GATEWAY}/v1/models", headers={"authorization": f"Bearer {secret}"}
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        cards_after = {card["id"] for card in json.loads(response.read()).get("data", [])}
    expect("and the card is gone from /v1/models", ALIAS not in cards_after, "")

    status, _ = api(
        dave, f"/api/admin/models/{target['id']}", json_body={"is_active": True}, method="PATCH"
    )
    expect("reactivated the target", status == 200, f"HTTP {status}")
    status, body = call(
        secret,
        "/v1/chat/completions",
        {"model": ALIAS, "messages": [{"role": "user", "content": "hello there"}]},
    )
    expect("and the alias serves again", status == 200, f"HTTP {status}")

    print()
    print("=== cleanup ===")
    status, _ = api(dave, f"/api/admin/aliases/{alias['id']}", method="DELETE")
    expect("deleted the alias", status == 204, f"HTTP {status}")
    keys = api(dave, "/api/me/keys")[1]["items"]
    mine = next((k for k in keys if k["name"] == "aliases-check"), None)
    if mine is not None:
        status, _ = api(dave, f"/api/me/keys/{mine['id']}", method="DELETE")
        expect("revoked the key it minted", status in (200, 204), f"HTTP {status}")
    leftover = next(
        (m for m in api(dave, "/api/admin/models?limit=200")[1]["items"] if m["name"] == ALIAS),
        None,
    )
    expect("nothing of the alias is left", leftover is None, "")

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
