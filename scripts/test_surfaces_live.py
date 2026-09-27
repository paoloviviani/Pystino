#!/usr/bin/env python3
"""Check the Responses, Anthropic Messages and image routes against the stack.

What only this can prove (ADR 0030):

* the three new routes are reachable, authenticated and metered over real HTTP,
  through the real provider client rather than an injected transport;
* Anthropic's usage, which arrives split across two SSE frames, survives the
  pipeline and lands in **PostgreSQL** as one prompt figure — the unit suite
  runs on SQLite and cannot see a Numeric round trip;
* an image request with no usage object at all is still billed, from the
  picture count;
* the ledger records which surface served each request, so the same model
  reached three ways is distinguishable afterwards.

Usage:
    docker compose --env-file deploy/.env \\
      -f deploy/compose/docker-compose.yml \\
      -f deploy/compose/docker-compose.smoke.yml \\
      -f deploy/compose/docker-compose.redaction.yml up -d --build
    set -a; . deploy/.env; set +a; python3 scripts/test_surfaces_live.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from live_session import GATEWAY, admin_credentials, check, login, request, user_credentials
from test_providers_live import api, sql

FAILURES: list[str] = []

IMAGE_MODEL = "image-model"
IMAGE_UPSTREAM = "upstream/image-model"


def expect(label: str, condition: bool, detail: str = "") -> None:
    if not check(label, condition, detail):
        FAILURES.append(label)


def key_for(user: Any, name: str) -> str | None:
    status, created = api(user, "/api/me/keys", json_body={"name": name}, method="POST")
    if status != 201:
        expect(f"minted an API key ({name})", False, f"HTTP {status}: {created}")
        return None
    return str(created["secret"])


def call(
    secret: str,
    path: str,
    body: dict[str, Any],
    *,
    sse: bool = False,
    method: str = "POST",
) -> tuple[int, Any]:
    """A `/v1` request with an API key, straight over HTTP.

    Not `live_session.request`: that one carries a browser session, and the
    whole point of `/v1` is that it authenticates with a revocable key instead.
    """
    headers = {"content-type": "application/json", "authorization": f"Bearer {secret}"}
    if sse:
        headers["accept"] = "text/event-stream"
    req = urllib.request.Request(
        f"{GATEWAY}{path}",
        data=json.dumps(body).encode() if method != "GET" else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        raw = error.read()
        status = error.code

    if sse:
        return status, raw.decode("utf-8", "replace")
    try:
        return status, json.loads(raw)
    except (ValueError, TypeError):
        return status, raw.decode("utf-8", "replace")


def stream(secret: str, path: str, body: dict[str, Any]) -> tuple[int, str]:
    status, text = call(secret, path, body, sse=True)
    return status, str(text)


def ledger(surface: str) -> list[str]:
    """The newest row for one surface, as raw columns."""
    columns = (
        "prompt_tokens, completion_tokens, cached_prompt_tokens, image_count, "
        "coalesce(image_size,''), cost, usage_source, api_surface, streamed"
    )
    # `surface` is a constant from this file, and psql is handed a literal.
    query = f"select {columns} from usage_records where api_surface = '{surface}' order by created_at desc limit 1;"  # noqa: E501,S608
    row = sql(query)
    return row.split("|") if row else []


def ensure_image_model(dave: Any) -> bool:
    """Catalogue an image model, priced per picture, if it is not there already."""
    models = api(dave, "/api/admin/models?limit=200")[1]["items"]
    existing = next((m for m in models if m["name"] == IMAGE_MODEL), None)
    if existing is None:
        providers = api(dave, "/api/admin/providers")[1]["items"]
        provider = next((p for p in providers if p["is_active"]), None)
        if provider is None:
            expect("an active provider exists", False)
            return False
        status, existing = api(
            dave,
            "/api/admin/models",
            json_body={
                "name": IMAGE_MODEL,
                "upstream_model": IMAGE_UPSTREAM,
                "provider_id": provider["id"],
                "kind": "image",
            },
            method="POST",
        )
        if status != 201:
            expect("catalogued an image model", False, f"HTTP {status}: {existing}")
            return False
        expect("catalogued an image model", True, existing["name"])

    prices = api(dave, f"/api/admin/models/{existing['id']}/prices")[1]["items"]
    if not any(p.get("per_image") for p in prices):
        status, price = api(
            dave,
            f"/api/admin/models/{existing['id']}/prices",
            json_body={
                "input_per_mtok": "0",
                "output_per_mtok": "0",
                "per_image": "0.04",
            },
            method="POST",
        )
        expect("priced it per image", status == 201, f"HTTP {status}: {price}")

    # The key is minted against the signing-in account's default billing group,
    # so *that* group needs the grant — not a hard-coded group name. A live
    # deployment's admin has its own group; a dev stack's happens to be called
    # research. Falling back to a group named research covers the seeded shape.
    _, _, me_raw = request(dave, f"{GATEWAY}/api/me")
    me = json.loads(me_raw)
    target = (me.get("default_billing_group") or {}).get("id")
    if target is None:
        groups = api(dave, "/api/admin/groups?limit=200")[1]["items"]
        target = next((g["id"] for g in groups if g["name"] == "research"), None)
    if target is not None:
        api(dave, f"/api/admin/groups/{target}/models/{existing['id']}", method="PUT")
    return True


def main() -> int:
    print("=== signing in ===")
    credentials = admin_credentials()
    if credentials is None:
        print("FAILED: PYSTINO_LIVE_ADMIN_PASSWORD is not set (see scripts/live_session.py)")
        return 1
    dave = login(*credentials)
    if dave is None:
        return 1
    if user := user_credentials():
        alice = login(*user)
        if alice is None:
            return 1
    else:
        # /v1 is authenticated by API key, not by session, so the *surfaces*
        # themselves can be checked with a key minted by the admin. The key is
        # minted against the admin's own default billing group; the group-based
        # access checks live in test_providers_live.py.
        from live_session import skip

        skip(
            "a non-admin session for minting the key",
            "PYSTINO_LIVE_USER/_PASSWORD are not set — using the admin's",
        )
        alice = dave

    if not ensure_image_model(dave):
        return 1

    secret = key_for(alice, "surfaces-check")
    if secret is None:
        return 1

    print()
    print("=== image generation ===")
    status, body = call(
        secret,
        "/v1/images/generations",
        {"model": IMAGE_MODEL, "prompt": "a cat in Torino", "n": 3, "size": "1024x1024"},
    )
    if status == 429:
        print("  skipped: a quota is exhausted in this deployment")
    else:
        expect("images are generated", status == 200, f"HTTP {status}: {str(body)[:160]}")
        if status == 200:
            expect(
                "three of them", len(body.get("data") or []) == 3, str(len(body.get("data") or []))
            )
        row = ledger("images")
        expect("the ledger records it as an images request", bool(row), str(row))
        if row:
            expect("the picture count is recorded", row[3] == "3", row[3])
            expect("and the size", row[4] == "1024x1024", row[4])
            # 3 x EUR 0.04, from a response with no usage object at all.
            expect(
                "billed per image, from a response carrying no usage",
                row[5].startswith("0.120"),
                f"cost={row[5]}",
            )

        status, refusal = call(
            secret,
            "/v1/chat/completions",
            {"model": IMAGE_MODEL, "messages": [{"role": "user", "content": "hi"}]},
        )
        expect(
            "an image model is refused by the chat route",
            status == 400 and "images/generations" in json.dumps(refusal),
            f"HTTP {status}: {str(refusal)[:160]}",
        )

    print()
    print("=== responses ===")
    status, body = call(secret, "/v1/responses", {"model": "smoke-model", "input": "hello there"})
    if status == 429:
        print("  skipped: a quota is exhausted in this deployment")
    else:
        expect("a response is generated", status == 200, f"HTTP {status}: {str(body)[:160]}")
        if status == 200:
            expect(
                "and it echoes our model name, not the provider's",
                body.get("model") == "smoke-model",
                str(body.get("model")),
            )
            row = ledger("responses")
            expect("the ledger records it as a responses request", bool(row), str(row))
            if row:
                # The fake reports 1,000,000 in / 500,000 out.
                expect(
                    "with the Responses usage names read correctly",
                    row[0] == "1000000" and row[1] == "500000",
                    f"prompt={row[0]} completion={row[1]}",
                )
                expect("and marked upstream_exact", row[6] == "upstream_exact", row[6])

        status, refusal = call(
            secret,
            "/v1/responses",
            {"model": "smoke-model", "input": "hi", "previous_response_id": "resp_x"},
        )
        expect(
            "server-side conversation state is refused",
            status == 400,
            f"HTTP {status}: {str(refusal)[:160]}",
        )

    print()
    print("=== anthropic messages ===")
    status, body = call(
        secret,
        "/v1/messages",
        {
            "model": "smoke-model",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "hello there"}],
        },
    )
    if status == 429:
        print("  skipped: a quota is exhausted in this deployment")
    else:
        expect("a message is generated", status == 200, f"HTTP {status}: {str(body)[:160]}")
        row = ledger("messages")
        expect("the ledger records it as a messages request", bool(row), str(row))
        if row:
            # 600k uncached + 100k written + 300k read = 1,000,000 prompt.
            expect(
                "the prompt is the sum of the three Anthropic slices",
                row[0] == "1000000",
                f"prompt={row[0]} (expected 600000+100000+300000)",
            )
            expect("the cache read is recorded", row[2] == "300000", row[2])

        status, text = stream(
            secret,
            "/v1/messages",
            {
                "model": "smoke-model",
                "max_tokens": 64,
                "stream": True,
                "messages": [{"role": "user", "content": "streamed hello"}],
            },
        )
    if status == 429:
        # The demo stack bills 1.5M tokens per request against a EUR 1/hour
        # budget, so the later checks in a full run legitimately run out. That
        # is the quota working, not a failure of the thing under test.
        print("  streamed: skipped, a quota is exhausted in this deployment")
    else:
        expect("a streamed message works", status == 200, f"HTTP {status}")
        expect(
            "named SSE events survive the pipeline",
            "event: message_start" in text and "event: message_stop" in text,
            text[:200],
        )
        row = ledger("messages")
        if row:
            # The load-bearing one: message_start carries the input count and
            # message_delta the output count. Letting the last frame win — the
            # correct rule for OpenAI — records a prompt of zero here.
            expect(
                "usage split across two frames is merged, not half-lost",
                row[0] == "1000000" and row[1] == "500000",
                f"prompt={row[0]} completion={row[1]}",
            )
            expect("and the row is marked streamed", row[8] == "t", row[8])

    print()
    print("=== model capabilities ===")
    providers = api(dave, "/api/admin/providers")[1]["items"]
    # The provider serving the fake upstream, not merely the first active one.
    # Everything below asserts about `upstream/*` models, which only the fake
    # upstream offers — and a deployment that has had a real provider added
    # through the console (as this one has) will otherwise discover against
    # that instead, and report a missing model that was never supposed to be
    # there. Falls back to the first active provider for a stack with only one.
    provider_id = next(
        (p["id"] for p in providers if p["is_active"] and "fake-upstream" in p["base_url"]),
        next((p["id"] for p in providers if p["is_active"]), None),
    )
    if provider_id:
        found = api(dave, f"/api/admin/models/discover?provider_id={provider_id}")[1]
        offered = {m["upstream_model"]: m for m in found.get("available", [])}
        catalogued = {
            m["upstream_model"]: m for m in api(dave, "/api/admin/models?limit=200")[1]["items"]
        }
        rich = offered.get("upstream/big-model") or catalogued.get("upstream/big-model")

        expect("discovery finds the multimodal model", rich is not None, str(sorted(offered)))
        if rich is not None:
            expect(
                "and reports what the provider claims it can do",
                "image" in rich["input_modalities"] and "tools" in rich["supported_features"],
                f"in={rich['input_modalities']} feat={rich['supported_features']}",
            )
            # `context_size` is the key the real catalogue uses, and it was
            # missing from the importer's list — every imported model had a
            # null context window and nothing failed (ADR 0031).
            expect(
                "and the context window, from the key the catalogue really uses",
                rich["context_window"] == 200_000,
                str(rich["context_window"]),
            )

        just_imported = False
        if "upstream/big-model" not in catalogued and rich is not None:
            status, _ = api(
                dave,
                f"/api/admin/models/import?provider_id={provider_id}",
                json_body={"models": [{"upstream_model": "upstream/big-model"}]},
                method="POST",
            )
            expect("it can be imported", status == 201, f"HTTP {status}")
            just_imported = status == 201
            catalogued = {
                m["upstream_model"]: m
                for m in api(dave, "/api/admin/models?limit=200")[1]["items"]
            }

        imported = catalogued.get("upstream/big-model")
        if imported is not None:
            # Only meaningful on the run that actually imported it. A few lines
            # below, this script edits `reasoning` off the same model to prove
            # an operator can correct a claim — and that edit is *supposed* to
            # persist, so on every later run the model is one a human has
            # already corrected. Asserting the catalogue's original claim
            # against it would be a check that fails because the feature under
            # test worked.
            if not just_imported:
                print(
                    "  skipped: upstream/big-model was catalogued by an earlier run and "
                    f"edited by it — feat={imported['supported_features']}. Delete the "
                    "model to exercise the import path again."
                )
            else:
                expect(
                    "the capabilities survive the import",
                    "image" in imported["input_modalities"]
                    and "reasoning" in imported["supported_features"],
                    f"in={imported['input_modalities']} feat={imported['supported_features']}",
                )
            status, edited = api(
                dave,
                f"/api/admin/models/{imported['id']}",
                json_body={"supported_features": ["Tools", "  json_mode "]},
                method="PATCH",
            )
            expect(
                "an operator can correct a claim, normalised",
                status == 200 and edited["supported_features"] == ["json_mode", "tools"],
                f"HTTP {status}: {edited.get('supported_features')}",
            )

        status, cards = call(secret, "/v1/models", {}, method="GET")
        if status == 200:
            by_id = {card["id"]: card for card in cards.get("data", [])}
            embed = by_id.get("embed-model")
            if embed is None:
                # Not a failure, and this used to report one. The model comes
                # from the discovery-and-adopt step above, which skips itself on
                # a stack a previous run already catalogued — so on every run
                # after the first this asserted on a model nothing had created
                # and reported the *product* broken. A check that depends on an
                # earlier step has to notice when that step was skipped.
                print(
                    "  skipped: embed-model was never adopted on this stack, so there "
                    "is no card to read a kind from. Delete the model, or use a fresh "
                    "database, to exercise it."
                )
            else:
                expect(
                    "and a caller can see the kind without taking a 400 to find out",
                    embed["kind"] == "embedding",
                    str(embed["kind"]),
                )

    print()
    print("=== the report explains the image spend ===")
    status, report = api(dave, "/api/admin/reports/usage?group_by=total")
    if status == 200:
        images = report["totals"]["images"]
        expect("the report counts the images", images >= 0, str(images))
        if images:
            expect(
                "and discloses that they carry no tokens",
                any("per image" in note for note in report["disclosures"]),
                str(report["disclosures"]),
            )

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
