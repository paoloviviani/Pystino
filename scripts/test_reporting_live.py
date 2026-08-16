#!/usr/bin/env python3
"""Exercise the reporting API and resets against the running compose stack.

Not a substitute for the unit tests — it is the half of them that cannot run on
SQLite. ``group_by=day`` converts each timestamp into the billing timezone with
PostgreSQL's ``timezone()``/``to_char()``, and the unique expression index on
``limit_rules`` is real DDL. Both are dialect-specific and therefore only ever
proven here.

Usage: python3 scripts/test_reporting_live.py   (stack must be up, with Keycloak)
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from test_oidc_flow import GATEWAY, check, login, request

FAILURES: list[str] = []


def api(opener: Any, path: str, **kwargs: Any) -> tuple[int, Any]:
    status, _, body = request(opener, f"{GATEWAY}{path}", **kwargs)
    try:
        return status, json.loads(body)
    except (ValueError, TypeError):
        return status, body.decode("utf-8", "replace")


def expect(label: str, condition: bool, detail: str = "") -> None:
    if not check(label, condition, detail):
        FAILURES.append(label)


def main() -> int:
    print("=== logging in as dave (platform-admins -> is_admin) ===")
    dave = login("dave")
    if dave is None:
        return 1
    status, profile = api(dave, "/api/me")
    expect("dave is an admin", bool(profile.get("is_admin")), str(profile.get("is_admin")))

    this_month = datetime.now(UTC).strftime("%Y-%m")

    print()
    print("=== the report resolves periods in the billing timezone ===")
    status, report = api(dave, "/api/admin/reports/usage")
    expect(
        "default report is the current month",
        status == 200 and report["period"]["label"] == this_month,
        f"HTTP {status}",
    )
    expect(
        "timezone is Europe/Rome",
        report["period"]["timezone"] == "Europe/Rome",
        report["period"]["timezone"],
    )

    status, march = api(dave, "/api/admin/reports/usage?period=2026-03")
    expect(
        "March starts at 23:00 UTC on 28 February (Rome is UTC+1 before the shift)",
        march["period"]["start"].startswith("2026-02-28T23:00"),
        march["period"]["start"],
    )
    expect(
        "and ends at 22:00 UTC on 31 March (UTC+2 after it)",
        march["period"]["end"].startswith("2026-03-31T22:00"),
        march["period"]["end"],
    )

    status, _bad = api(dave, "/api/admin/reports/usage?period=last-month")
    expect("an unparsable period is refused", status == 400, f"HTTP {status}")

    print()
    print("=== group_by, including the PostgreSQL-only day conversion ===")
    for dimension in ("group", "user", "model", "api_key", "day", "total"):
        status, body = api(dave, f"/api/admin/reports/usage?group_by={dimension}")
        expect(f"group_by={dimension}", status == 200, f"HTTP {status}: {str(body)[:120]}")
        if status == 200 and dimension == "day":
            labels = [row["label"] for row in body["rows"]]
            expect(
                "day labels are dates and in order",
                labels == sorted(labels)
                and all(len(label) == 10 and label[4] == "-" for label in labels),
                str(labels[:5]),
            )

    status, by_group = api(dave, "/api/admin/reports/usage?group_by=group")
    status, by_total = api(dave, "/api/admin/reports/usage?group_by=total")
    expect(
        "the totals agree however the report is sliced",
        Decimal(by_group["totals"]["cost"]) == Decimal(by_total["totals"]["cost"]),
        f"{by_group['totals']['cost']} vs {by_total['totals']['cost']}",
    )
    print(
        f"  this month: {by_total['totals']['cost']} {by_total['currency']} "
        f"over {by_total['totals']['requests']} request(s)"
    )
    for note in by_total["disclosures"]:
        print(f"  disclosure: {note}")

    print()
    print("=== CSV ===")
    csv_status, _, csv_body = request(dave, f"{GATEWAY}/api/admin/reports/usage.csv")
    text = csv_body.decode()
    expect("CSV is served", csv_status == 200, f"HTTP {csv_status}")
    expect("CSV has the header", text.startswith("period,period_start"), text[:60])
    expect("CSV ends with a total row", ",total," in text or text.rstrip().endswith("0"), "")

    print()
    print("=== a calendar quota, its counter, and a reset ===")
    groups = api(dave, "/api/admin/groups")[1]["items"]
    research = next((g for g in groups if g["name"] == "research"), None)
    if research is None:
        expect("the research group exists", False, str([g["name"] for g in groups]))
        return 1

    status, created = api(
        dave,
        "/api/admin/limits",
        method="POST",
        json_body={
            "name": "live-check monthly",
            "scope": "group",
            "scope_id": research["id"],
            "metric": "cost",
            "period": "month",
            "limit_value": "1000",
        },
    )
    if status == 409:
        # Left behind by an earlier run; find it and carry on.
        rules = api(dave, "/api/admin/limits")[1]["items"]
        created = next(
            r
            for r in rules
            if r["scope"] == "group"
            and r["scope_id"] == research["id"]
            and r["period"] == "month"
            and r["metric"] == "cost"
        )
        print("  reusing the rule from a previous run")
    else:
        expect("a calendar rule can be created", status == 201, f"HTTP {status}: {created}")
        expect(
            "it reports its window as a period",
            created.get("window_label") == "month",
            str(created.get("window_label")),
        )

    status, _duplicate = api(
        dave,
        "/api/admin/limits",
        method="POST",
        json_body={
            "name": "duplicate",
            "scope": "group",
            "scope_id": research["id"],
            "metric": "cost",
            "period": "month",
            "limit_value": "5",
        },
    )
    expect("a duplicate rule is refused by the expression index", status == 409, f"HTTP {status}")

    rules = api(dave, "/api/admin/limits")[1]["items"]
    rule = next(r for r in rules if r["id"] == created["id"])
    expect(
        "current_value is reported from the live counters",
        rule["current_value"] is not None,
        str(rule["current_value"]),
    )
    report_total = Decimal(
        api(dave, f"/api/admin/reports/usage?group_id={research['id']}&group_by=total")[1][
            "totals"
        ]["cost"]
    )
    expect(
        "the monthly quota counter and the monthly report agree",
        Decimal(rule["current_value"]) == report_total,
        f"quota {rule['current_value']} vs report {report_total}",
    )

    status, reset = api(
        dave,
        f"/api/admin/limits/{rule['id']}/reset",
        method="POST",
        json_body={"reason": "live check"},
    )
    expect("the rule can be reset", status == 200, f"HTTP {status}: {reset}")
    expect(
        "the reset records who did it",
        reset.get("created_by_email") == "dave@example.org",
        str(reset.get("created_by_email")),
    )

    after = next(r for r in api(dave, "/api/admin/limits")[1]["items"] if r["id"] == rule["id"])
    expect(
        "consumption is back to zero",
        Decimal(after["current_value"] or 0) == 0,
        str(after["current_value"]),
    )
    expect("the reset is timestamped on the rule", after["last_reset_at"] is not None, "")

    after_report = Decimal(
        api(dave, f"/api/admin/reports/usage?group_id={research['id']}&group_by=total")[1][
            "totals"
        ]["cost"]
    )
    expect(
        "and the billing report is untouched",
        after_report == report_total,
        f"{after_report} vs {report_total}",
    )

    trail = api(dave, f"/api/admin/limits/{rule['id']}/resets")[1]["items"]
    expect(
        "the reset is in the audit trail",
        any(e["reason"] == "live check" for e in trail),
        str(trail)[:120],
    )

    status, _ = api(
        dave, f"/api/admin/limits/{rule['id']}/reset", method="POST", json_body={"reason": ""}
    )
    expect("a reset without a reason is refused", status == 400, f"HTTP {status}")

    print()
    print("=== a non-admin sees only their own spend ===")
    alice = login("alice")
    if alice is not None:
        status, _mine = api(alice, "/api/me/reports/usage")
        expect("alice can read her own report", status == 200, f"HTTP {status}")
        status, _ = api(alice, "/api/admin/reports/usage")
        expect("alice cannot read everyone's", status == 403, f"HTTP {status}")

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
