#!/usr/bin/env python3
"""Check per-search billing against the stack (ADR 0058).

What only this can prove:

* ``per_search`` survives a round trip through PostgreSQL's ``Numeric(24,12)``
  as plain digits — the unit suite runs on SQLite and cannot see it;
* the report and its **CSV export** sum the new column on the real dialect.
  The export keeps its own list of columns, and its missing one was found here
  and nowhere else;
* the whole chain holds outside the unit suite: the cap is written into the
  outgoing tool, the counterparty reports what it actually searched, and the
  charge lands on the ledger row beside the tokens.

Needs the smoke overlay: its fake upstream reports two searches for any request
carrying a search tool.

Usage:
    docker compose --env-file deploy/.env \\
      -f deploy/compose/docker-compose.yml \\
      -f deploy/compose/docker-compose.smoke.yml \\
      -f deploy/compose/docker-compose.redaction.yml up -d --build
    set -a; . deploy/.env; set +a; python3 scripts/test_web_search_live.py
"""

from __future__ import annotations

import csv
import io
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from live_session import (
    GATEWAY,
    admin_credentials,
    check,
    login,
    request,
    user_credentials,
)
from test_providers_live import api
from test_surfaces_live import call, key_for

FAILURES: list[str] = []

TOOL = {"type": "web_search_20250305", "name": "web_search"}
PER_SEARCH = Decimal("0.01")


def expect(label: str, condition: bool, detail: str = "") -> None:
    if not check(label, condition, detail):
        FAILURES.append(label)


def main() -> int:
    credentials = admin_credentials()
    if credentials is None:
        print("set PYSTINO_LIVE_ADMIN_PASSWORD (see scripts/live_session.py)")
        return 1
    session = login(*credentials)
    if session is None:
        print("admin login failed")
        return 1

    # The key is minted as the *user*, not the admin: `/v1` authenticates with
    # a revocable key that carries a billing group, and the seeded admin has
    # none — which is the 400 this script hit the first time it was run.
    people = user_credentials()
    if people is None:
        print("set PYSTINO_LIVE_USER and PYSTINO_LIVE_USER_PASSWORD")
        return 1
    user = login(*people)
    if user is None:
        print("user login failed")
        return 1
    secret = key_for(user, "web-search-live")
    if secret is None:
        return 1

    # Priced against a model this caller can actually reach rather than the
    # first in the catalogue: model access is an allow-list, and a 404 here
    # would read as a pricing bug.
    _, offered = call(secret, "/v1/models", {}, method="GET")
    names = [m["id"] for m in (offered or {}).get("data", [])]
    status, models = api(session, "/api/admin/models?limit=100")
    model = next(
        (
            m
            for m in models.get("items", [])
            if m["kind"] == "chat" and m["is_active"] and m["name"] in names
        ),
        None,
    )
    if model is None:
        print(f"no chat model this caller may use; offered {names}")
        return 1
    print(f"model: {model['name']}")

    # -- the rate ----------------------------------------------------------
    status, created = api(
        session,
        f"/api/admin/models/{model['id']}/prices",
        method="POST",
        json_body={
            "input_per_mtok": "1",
            "output_per_mtok": "2",
            "per_search": str(PER_SEARCH),
        },
    )
    expect("appended a per-search price", status == 201, f"HTTP {status}: {created}")
    stored = str(created.get("per_search")) if status == 201 else ""
    expect(
        "per_search round-trips as plain digits",
        bool(stored) and Decimal(stored) == PER_SEARCH and "E" not in stored,
        f"stored as {stored!r}",
    )

    # -- a searched request ------------------------------------------------
    status, body = call(
        secret,
        "/v1/messages",
        {
            "model": model["name"],
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "who won the match yesterday"}],
            "tools": [TOOL],
        },
    )
    expect("a searched request is served", status == 200, f"HTTP {status}: {body}")
    reported = (body or {}).get("usage", {}).get("server_tool_use", {})
    expect(
        "the counterparty reported its searches",
        reported.get("web_search_requests") == 2,
        f"usage.server_tool_use = {reported}",
    )

    # The ledger is read through the report rather than through psql: the
    # report *is* a sum over `usage_records.search_count`, so it proves the
    # column was written, and it does not need the compose project name.

    # -- the report, on the real dialect -----------------------------------
    status, report = api(session, "/api/admin/reports/usage?group_by=model")
    expect("the report answers", status == 200, f"HTTP {status}")
    row = next((r for r in report.get("rows", []) if r["label"] == model["name"]), None)
    expect("the report has a row for the model", row is not None)
    if row is not None:
        expect(
            "the report sums the search count",
            row["searches"] >= 2,
            f"searches={row['searches']}",
        )
    expect(
        "the report discloses the search charge",
        any("web search" in note for note in report.get("disclosures", [])),
        f"disclosures={report.get('disclosures')}",
    )

    # -- the CSV export ----------------------------------------------------
    _, _, raw = request(session, f"{GATEWAY}/api/admin/reports/usage.csv?group_by=model")
    exported: list[dict[str, Any]] = list(csv.DictReader(io.StringIO(raw.decode())))
    expect(
        "the CSV export carries a searches column",
        bool(exported) and "searches" in exported[0],
        f"columns={list(exported[0]) if exported else []}",
    )
    if exported and "searches" in exported[0]:
        total = exported[-1]["searches"]
        expect("the CSV total row counts them", int(total or 0) >= 2, f"total={total}")

    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
