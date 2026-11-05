#!/usr/bin/env python3
"""Decision models against a real stack and the real Cortecs Decisions API (ADR 0100).

The unit suite proves the routes and the metering against a fake upstream. What
only a real provider can show, and what this checks:

* a predicate answers with a `probability` (and, measured 2026-11-04, no
  `confidence`), through `/v1/decisions` and `/v1/systemone`;
* `usage.cost` is read as micro-EUR and is the charge in `provider_reported`
  mode: the usage row carries it as `cost`, `cost_source=provider_reported`,
  with the price table's figure beside it in `computed_cost`;
* one usage row per request, surface `decisions`;
* a quota is reserved before the call and settled after it: a rule smaller than
  the reservation refuses the call with a 429 before anything is spent, and a
  rule with room ends at the recorded tokens, not at the reservation estimate;
* a routing field is refused with a 400 naming it, and nothing is recorded as
  spent; `preference` passes;
* an unknown model, at the gateway and at the provider, is a clean error and is
  never charged;
* Cortecs's `/models?tag=Decision` models are importable through the discovery dialog
  and the entries come back as kind `decision`;
* `/v1/models` lists the model with `kind: "decision"` and the chat route
  refuses it.

Spends about EUR 0.00001 per call, a few dozen calls at most. The Cortecs key is
read inside this script, from `CORTECS_API_KEY_FILE` (a path) or
`CORTECS_API_KEY`, sent only to the gateway's admin API, never printed, and
scrubbed from anything echoed. Never put it on a command line.

    docker compose --env-file deploy/.env -f deploy/compose.yaml \\
      -f deploy/dev/smoke.yml --profile authelia up -d --wait
    set -a; . deploy/.env; set +a
    export PYSTINO_LIVE_ADMIN_PASSWORD=...
    export CORTECS_API_KEY_FILE=/path/to/key
    uv run python scripts/test_decisions_live.py

Run it after `scripts/test_providers_live.py`'s requirements are met (the stack
up, psql reachable through compose). Never run by CI: it needs a real key.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from live_session import GATEWAY, admin_credentials, check, login, request, user_credentials
from test_providers_live import api, sql

FAILURES: list[str] = []

PROVIDER = "cortecs-decisions-live"
MODEL = "live-clef-flash"
UPSTREAM = "clef-flash"
GHOST = "live-ghost-decision"
GHOST_UPSTREAM = "no-such-decision-model"
#: What Cortecs listed on 2026-11-04 (reports/2026-11-04-decision-reviewer-trials.md).
INPUT_PER_MTOK = "0.042"

QUESTION = {
    "type": "predicate",
    "name": "safe",
    "instructions": "Is the action described in the input routine and safe for a coding agent?",
}
INPUT = "The agent runs the project's unit tests with `npm test` in its own workspace."


def expect(label: str, condition: bool, detail: str = "") -> bool:
    ok = check(label, condition, scrub(detail))
    if not ok:
        FAILURES.append(label)
    return ok


_SECRET = ""


def scrub(text: str) -> str:
    """Whatever is printed has the provider key removed first."""
    return text.replace(_SECRET, "<redacted>") if _SECRET else text


def read_key() -> str:
    path = os.environ.get("CORTECS_API_KEY_FILE", "")
    if path:
        return Path(path).read_text().strip()
    return os.environ.get("CORTECS_API_KEY", "").strip()


def call(
    secret: str, path: str, body: dict[str, Any], method: str = "POST"
) -> tuple[int, dict[str, str], Any]:
    """A `/v1` request by API key, with the response headers."""
    req = urllib.request.Request(
        f"{GATEWAY}{path}",
        data=json.dumps(body).encode() if method != "GET" else None,
        headers={"content-type": "application/json", "authorization": f"Bearer {secret}"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            raw, status, headers = response.read(), response.status, dict(response.headers)
    except urllib.error.HTTPError as error:
        raw, status, headers = error.read(), error.code, dict(error.headers)
    try:
        return status, headers, json.loads(raw)
    except (ValueError, TypeError):
        return status, headers, raw.decode("utf-8", "replace")


def ledger(request_count_before: int) -> list[dict[str, str]]:
    """The decisions rows after the first `request_count_before`, oldest first."""
    columns = [
        "status",
        "model_name",
        "prompt_tokens",
        "completion_tokens",
        "cost",
        "computed_cost",
        "cost_source",
        "usage_source",
        "upstream_cost",
        "upstream_provider",
        "upstream_status",
        "error_code",
    ]
    query = (
        f"select {', '.join(f'coalesce({c}::text, chr(126))' for c in columns)} "  # noqa: S608
        "from usage_records where api_surface = 'decisions' order by created_at;"
    )
    rows = [line.split("|") for line in sql(query).splitlines() if line]
    return [dict(zip(columns, row, strict=False)) for row in rows[request_count_before:]]


def row_count() -> int:
    return int(sql("select count(*) from usage_records where api_surface = 'decisions';") or 0)


def limit_current(secret: str, rule_id: str) -> Decimal | None:
    status, _, body = call(secret, "/v1/pystino/usage", {}, method="GET")
    if status != 200 or not isinstance(body, dict):
        return None
    for rule in body.get("limits", []):
        if rule["id"] == rule_id and rule.get("current_value") is not None:
            return Decimal(str(rule["current_value"]))
    return None


def main() -> int:
    global _SECRET
    _SECRET = read_key()
    if not _SECRET:
        print("FAILED: set CORTECS_API_KEY_FILE (a path) or CORTECS_API_KEY")
        return 2
    credentials = admin_credentials()
    if credentials is None:
        print("FAILED: PYSTINO_LIVE_ADMIN_PASSWORD is not set (see scripts/live_session.py)")
        return 2
    admin = login(*credentials)
    if admin is None:
        return 1
    member = admin
    if user := user_credentials():
        member = login(*user) or admin

    print(f"gateway: {GATEWAY}\n")
    print("=== 1. a Cortecs provider and a decision model ===")
    status, provider = api(
        admin,
        "/api/admin/providers",
        method="POST",
        json_body={
            "name": PROVIDER,
            "plugin": "cortecs",
            "kind": "router",
            "billing_mode": "provider_reported",
            "api_key": _SECRET,
        },
    )
    if status != 201:
        expect("the Cortecs provider is created", False, f"HTTP {status}: {provider}")
        return 1
    expect("the Cortecs provider is created", True, provider["name"])
    expect("the key is not echoed back", _SECRET not in json.dumps(provider))

    # The kind needs a plugin that serves decisions: a generic provider is refused.
    status, generic = api(
        admin,
        "/api/admin/providers",
        method="POST",
        json_body={"name": "generic-no-decisions", "base_url": "http://127.0.0.1:9/v1"},
    )
    if status == 201:
        status, refusal = api(
            admin,
            "/api/admin/models",
            method="POST",
            json_body={
                "name": "generic-decision",
                "upstream_model": "x",
                "provider_id": generic["id"],
                "kind": "decision",
            },
        )
        expect(
            "a decision model on a provider without decision support is refused",
            status == 400,
            f"HTTP {status}: {str(refusal)[:200]}",
        )
        api(admin, f"/api/admin/providers/{generic['id']}", method="DELETE")

    status, discovery = api(
        admin,
        f"/api/admin/models/discover?provider_id={provider['id']}",
    )
    found = (
        {m["upstream_model"]: m for m in (discovery.get("available") or [])}
        if status == 200
        else {}
    )
    expect(
        "discovery finds clef-flash and laya, as kind decision",
        status == 200
        and {"clef-flash", "laya"} <= set(found)
        and all(found[k]["kind"] == "decision" for k in ("clef-flash", "laya")),
        f"HTTP {status}: {sorted(found)}"
        + (f" kinds={[found[k]['kind'] for k in sorted(found)]}" if found else ""),
    )
    if found.get(UPSTREAM):
        expect(
            "and its input price is the catalogue's",
            Decimal(str(found[UPSTREAM]["input_per_mtok"])) == Decimal(INPUT_PER_MTOK),
            str(found[UPSTREAM]["input_per_mtok"]),
        )

    models: dict[str, dict[str, Any]] = {}
    for name, upstream in ((MODEL, UPSTREAM), (GHOST, GHOST_UPSTREAM)):
        status, created = api(
            admin,
            "/api/admin/models",
            method="POST",
            json_body={
                "name": name,
                "upstream_model": upstream,
                "provider_id": provider["id"],
                "kind": "decision",
            },
        )
        if not expect(f"{name} is catalogued as kind decision", status == 201, str(created)[:200]):
            return 1
        models[name] = created
        status, price = api(
            admin,
            f"/api/admin/models/{created['id']}/prices",
            method="POST",
            json_body={"input_per_mtok": INPUT_PER_MTOK, "output_per_mtok": "0"},
        )
        expect(f"{name} is priced (input only)", status == 201, str(price)[:200])

    _, _, me_raw = request(member, f"{GATEWAY}/api/me")
    group = ((json.loads(me_raw)).get("default_billing_group") or {}).get("id")
    if group is None:
        expect("the caller has a default billing group", False)
        return 1
    for created in models.values():
        api(admin, f"/api/admin/groups/{group}/models/{created['id']}", method="PUT")
    status, key = api(member, "/api/me/keys", json_body={"name": "decisions-live"}, method="POST")
    if not expect("an API key is minted", status == 201, str(key)[:200]):
        return 1
    secret = str(key["secret"])

    print("\n=== 2. the catalogue ===")
    status, _, listing = call(secret, "/v1/models", {}, method="GET")
    entries = {m["id"]: m for m in listing["data"]} if status == 200 else {}
    expect(
        "/v1/models lists it with kind decision",
        entries.get(MODEL, {}).get("kind") == "decision",
        str(entries.get(MODEL)),
    )
    status, _, chat = call(
        secret,
        "/v1/chat/completions",
        {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
    )
    expect(
        "the chat route refuses a decision model and names the route",
        status == 400 and "decisions" in json.dumps(chat),
        f"HTTP {status}: {str(chat)[:200]}",
    )

    print("\n=== 3. a predicate ===")
    before = row_count()
    started = time.monotonic()
    status, _, body = call(
        secret,
        "/v1/decisions",
        {"model": MODEL, "input": INPUT, "questions": [QUESTION], "preference": "cost"},
    )
    elapsed = time.monotonic() - started
    if status == 429:
        print("  skipped the rest: a quota is exhausted in this deployment")
        return 1
    if not expect(
        "a predicate is answered", status == 200, scrub(f"HTTP {status}: {str(body)[:300]}")
    ):
        return 1
    answer = (body.get("answers") or [{}])[0]
    probability = answer.get("probability")
    expect(
        "the answer carries a probability between 0 and 1",
        isinstance(probability, float) and 0 <= probability <= 1,
        str(answer),
    )
    expect("a predicate answer has no confidence", "confidence" not in answer, str(answer))
    print(f"  probability {probability} in {elapsed:.2f}s; usage {body.get('usage')}")
    reported = (body.get("usage") or {}).get("cost")
    expect(
        "usage.cost is an integer (micro-EUR)",
        isinstance(reported, int) and reported > 0,
        str(reported),
    )
    rows = ledger(before)
    expect("exactly one usage row for the request", len(rows) == 1, str(len(rows)))
    if rows:
        row = rows[0]
        print(f"  ledger: {row}")
        expect("it is completed", row["status"] == "completed", row["status"])
        expect(
            "the charge is the provider's reported figure in EUR",
            isinstance(reported, int) and Decimal(row["cost"]) == Decimal(reported) / 1_000_000,
            f"cost={row['cost']} reported={reported}",
        )
        expect("cost_source is provider_reported", row["cost_source"] == "provider_reported")
        expect(
            "the price table's figure is kept beside it",
            Decimal(row["computed_cost"])
            == Decimal(row["prompt_tokens"]) * Decimal(INPUT_PER_MTOK) / 1_000_000,
            f"computed={row['computed_cost']} tokens={row['prompt_tokens']}",
        )
        expect(
            "the reported figure is at most a micro-EUR above the table's",
            0 <= Decimal(row["cost"]) - Decimal(row["computed_cost"]) <= Decimal("0.000001"),
            f"{row['cost']} vs {row['computed_cost']}",
        )
        expect(
            "the provider's own figure is recorded",
            row["upstream_cost"] != "~",
            row["upstream_cost"],
        )
        expect("the serving sub-provider is recorded", row["upstream_provider"] != "~")

    print("\n=== 4. quota: reserved before the call, settled after ===")
    status, rule = api(
        admin,
        "/api/admin/limits",
        method="POST",
        json_body={
            "name": "decisions-live ceiling",
            "scope": "global",
            "metric": "tokens",
            "window_seconds": 3600,
            "limit_value": "100000000",
        },
    )
    rule_id = rule.get("id") if status == 201 else None
    expect("a generous global token rule is created", status == 201, str(rule)[:200])
    if rule_id:
        # Counters rebuild from the ledger, so the rule already counts the call above.
        start = limit_current(secret, rule_id)
        before = row_count()
        status, _, body = call(
            secret, "/v1/decisions", {"model": MODEL, "input": INPUT, "questions": [QUESTION]}
        )
        end = limit_current(secret, rule_id)
        rows = ledger(before)
        expect("a second call succeeds", status == 200, str(body)[:200])
        if rows and start is not None and end is not None:
            spent = int(rows[0]["prompt_tokens"]) + int(rows[0]["completion_tokens"])
            expect(
                "the counter moved by the recorded tokens, not by the reservation estimate",
                end - start == spent,
                f"counter {start} -> {end}, recorded {spent}",
            )
        else:
            expect("the counter and the row can be read", False, f"{start} {end} {rows}")
        api(admin, f"/api/admin/limits/{rule_id}", method="DELETE")

    status, tight = api(
        admin,
        "/api/admin/limits",
        method="POST",
        json_body={
            "name": "decisions-live tight",
            "scope": "global",
            "metric": "requests",
            "window_seconds": 3600,
            "limit_value": "0",
        },
    )
    if status == 201:
        before = row_count()
        status, _, body = call(
            secret, "/v1/decisions", {"model": MODEL, "input": INPUT, "questions": [QUESTION]}
        )
        expect(
            "a rule with no room refuses the call with 429 before it reaches the provider",
            status == 429,
            f"HTTP {status}: {str(body)[:200]}",
        )
        expect(
            "and no completed row is recorded",
            not any(r["status"] == "completed" for r in ledger(before)),
            str(ledger(before)),
        )
        api(admin, f"/api/admin/limits/{tight['id']}", method="DELETE")
    else:
        expect("a zero-room rule is created", False, str(tight)[:200])

    print("\n=== 5. refusals ===")
    for field, value in (
        ("allowed_providers", ["nowhere"]),
        ("eu_native", False),
        ("fallbacks", ["x"]),
    ):
        before = row_count()
        status, _, body = call(
            secret,
            "/v1/decisions",
            {"model": MODEL, "input": INPUT, "questions": [QUESTION], field: value},
        )
        expect(
            f"{field} is refused with a 400 naming it",
            status == 400 and field in json.dumps(body),
            f"HTTP {status}: {str(body)[:200]}",
        )
        expect(
            f"{field}: nothing was spent",
            not any(r["status"] == "completed" for r in ledger(before)),
        )

    before = row_count()
    status, _, body = call(
        secret, "/v1/decisions", {"model": "no-such-model", "input": INPUT, "questions": [QUESTION]}
    )
    expect(
        "a model the gateway does not know is a clean 4xx error",
        status in (400, 404) and isinstance(body, dict) and "error" in body,
        f"HTTP {status}: {str(body)[:200]}",
    )
    status, _, body = call(
        secret, "/v1/decisions", {"model": GHOST, "input": INPUT, "questions": [QUESTION]}
    )
    expect(
        "a model the provider does not know passes its 404 through with the error body",
        status == 404 and isinstance(body, dict) and "error" in body,
        f"HTTP {status}: {str(body)[:300]}",
    )
    rows = [r for r in ledger(before) if r["model_name"] == GHOST]
    expect(
        "and it is recorded as an upstream error with nothing charged",
        len(rows) == 1 and rows[0]["status"] != "completed" and Decimal(rows[0]["cost"]) == 0,
        str(rows),
    )

    print("\n=== 6. the System One form ===")
    before = row_count()
    status, _, body = call(
        secret,
        "/v1/systemone",
        {
            "model": MODEL,
            "state": {"action": INPUT},
            "questions": {"q1": {"type": "noul", "instructions": QUESTION["instructions"]}},
        },
    )
    expect("/v1/systemone answers", status == 200, f"HTTP {status}: {str(body)[:300]}")
    if status == 200:
        print(f"  answers: {body.get('answers')}")
        expect(
            "the answers are keyed by our id",
            isinstance(body.get("answers"), dict) and "q1" in body["answers"],
            str(body.get("answers")),
        )
        rows = ledger(before)
        expect(
            "one row, charged at the reported cost",
            len(rows) == 1 and rows[0]["cost_source"] == "provider_reported",
            str(rows),
        )

    print("\n=== cleanup ===")
    for created in models.values():
        api(admin, f"/api/admin/models/{created['id']}", method="DELETE")
    api(admin, f"/api/admin/providers/{provider['id']}", method="DELETE")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for label in FAILURES:
            print(f"  - {label}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
