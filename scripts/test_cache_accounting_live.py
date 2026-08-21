#!/usr/bin/env python3
"""Prompt-cache accounting against a real provider that really caches.

What only this can prove (docs/cache-accounting-findings.md):

The unit suite pins the *spellings* we know about, from payloads captured on one
day. It cannot notice a provider renaming the field, adding a fifth spelling, or
moving it out of `prompt_tokens_details` — and the failure mode is silent, because
an unread cache-write count does not error, it just gets billed at the input rate.

So this drives a real cache hit through the gateway and asserts the ledger saw
it. It needs:

* the stack up, with a provider whose upstream really caches, and a model from it
  catalogued and granted to the caller;
* that provider's `upstream_cost_unit` set, for the reconciliation check.

It spends a small amount of real money — two short completions over a ~1500 token
prompt. Skips rather than fails when the preconditions are absent, so it is safe
to run in a deployment that has only the fake upstream.

Usage:
    set -a; . deploy/.env; set +a
    uv run python scripts/test_cache_accounting_live.py [model-name]
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_oidc_flow import GATEWAY, check, login, request

FAILURES: list[str] = []
COMPOSE = [
    "docker", "compose", "--env-file", "deploy/.env",
    "-f", "deploy/compose/docker-compose.yml",
]
# Long enough to cross the usual automatic-cache threshold, and identical between
# the two calls so the second is a hit if the provider caches at all.
FILLER = (
    "A fixed reference document about European research infrastructure funding "
    "and its administrative overheads. "
) * 60


def expect(label: str, condition: bool, detail: str = "") -> None:
    if not check(label, condition, detail):
        FAILURES.append(label)


def sql(query: str) -> str:
    result = subprocess.run(  # noqa: S603
        [
            *COMPOSE, "exec", "-T", "postgres", "psql",
            "-U", "gateway", "-d", "gateway", "-tAc", query,
        ],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        check=False,
    )
    return result.stdout.strip()


def complete(secret: str, model: str) -> dict | None:
    body = json.dumps({
        "model": model, "max_tokens": 5, "temperature": 0,
        "messages": [
            {"role": "system", "content": FILLER},
            {"role": "user", "content": "Reply OK."},
        ],
    }).encode()
    req = urllib.request.Request(
        f"{GATEWAY}/v1/chat/completions", data=body, method="POST",
        headers={"content-type": "application/json", "authorization": f"Bearer {secret}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return dict(json.loads(response.read()))
    except urllib.error.HTTPError as error:
        if error.code == 429:
            print("  skipped: a quota is exhausted in this deployment")
        else:
            expect("the completion succeeds", False, f"HTTP {error.code}")
        return None


def main() -> int:
    dave = login("dave")
    if dave is None:
        return 1

    print()
    print("=== finding a model on a provider that reports cache pricing ===")
    _, _, raw = request(dave, f"{GATEWAY}/api/admin/providers?limit=200")
    providers = {p["id"]: p for p in json.loads(raw)["items"]}
    models = json.loads(request(dave, f"{GATEWAY}/api/admin/models?limit=200")[2])["items"]
    wanted = sys.argv[1] if len(sys.argv) > 1 else None

    candidates = [
        m for m in models
        if m["is_active"] and m["kind"] == "chat"
        and (wanted is None or m["name"] == wanted)
        # A cache-read price is the catalogue telling us this model caches.
        and (m.get("current_price") or {}).get("cache_read_per_mtok")
    ]
    if not candidates:
        print("  skipped: no active chat model with a cache-read price is catalogued here.")
        print("  Import one from a real provider first — the fake upstream does not cache.")
        return 0

    model = candidates[0]
    provider = providers.get(model["provider_id"], {})
    print(f"  using {model['name']} on {provider.get('name')} "
          f"(cache read {model['current_price']['cache_read_per_mtok']}/Mtok)")
    if not provider.get("upstream_cost_unit"):
        print(f"  note: {provider.get('name')} has no upstream_cost_unit set, so the "
              "reconciliation check is skipped")

    status, _, body = request(dave, f"{GATEWAY}/api/me/keys", method="POST",
                              json_body={"name": "cache-accounting-live"})
    if status != 201:
        expect("minted an API key", False, f"HTTP {status}")
        return 1
    secret = json.loads(body)["secret"]

    print()
    print("=== two identical prompts: a miss, then a hit ===")
    first = complete(secret, model["name"])
    if first is None:
        return 1 if FAILURES else 0
    time.sleep(3)
    second = complete(secret, model["name"])
    if second is None:
        return 1 if FAILURES else 0

    details = (second.get("usage") or {}).get("prompt_tokens_details") or {}
    cached = int(details.get("cached_tokens") or 0)
    expect(
        "the provider reports a cache hit on the second call",
        cached > 0,
        f"cached_tokens={cached}; without a hit the rest proves nothing",
    )
    if cached == 0:
        return 1

    print()
    print("=== the ledger recorded the slices ===")
    # The model name comes from our own API, not from user input, and this is a
    # development script handing psql a literal either way.
    columns = (
        "prompt_tokens, cached_prompt_tokens, cache_write_tokens, cost, "
        "coalesce(upstream_cost::text, ''), coalesce(upstream_cost_currency, '')"
    )
    name = model["name"]
    row = sql(
        f"select {columns} from usage_records where model_name = '{name}' "  # noqa: S608
        "order by created_at desc limit 1;"
    )
    if not row:
        expect("a usage row exists", False, "no rows")
        return 1
    prompt, stored_cached, written, cost, upstream, currency = row.split("|")
    print(f"  prompt={prompt} cached={stored_cached} written={written} cost={cost} "
          f"upstream={upstream or 'null'} {currency}")

    expect(
        "the cache read reached the ledger",
        int(stored_cached) == cached,
        f"ledger {stored_cached} vs response {cached}",
    )
    # The written slice is the one that was silently dropped before: no column,
    # and no reader looking for any of its four spellings.
    reported_write = any(
        int(details.get(key) or 0) > 0
        for key in ("cache_write_tokens", "cache_creation_tokens", "created_cache_tokens")
    )
    if reported_write:
        expect(
            "and so did the cache write",
            int(written) > 0,
            f"ledger stored {written}; zero here means a fifth spelling or a moved field",
        )
    else:
        print("  note: this provider reported no cache write on the hit; nothing to check")

    expect(
        "the three slices are disjoint and inside the prompt",
        int(stored_cached) + int(written) <= int(prompt),
        f"{stored_cached} read + {written} written of {prompt} prompt",
    )

    if provider.get("upstream_cost_unit") and upstream:
        ours, theirs = Decimal(cost), Decimal(upstream)
        # Their figure is rounded to whole micro-units per component, so exact
        # equality is not expected; an order-of-magnitude gap is the signal.
        close = theirs > 0 and Decimal("0.5") < ours / theirs < Decimal("2")
        expect(
            "our cost and the provider's agree to within rounding",
            close,
            f"ours {ours} vs theirs {theirs}",
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
