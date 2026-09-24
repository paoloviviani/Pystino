#!/usr/bin/env python3
"""Check the console against the running stack.

What only this can prove: that the assets landed in the image, that the entry
document and its scripts are served with the right cache and security headers,
and — the part that matters — that the API calls the page makes actually work
for a real session. A build that succeeds and a page that renders nothing is the
failure mode this catches.

It does not drive a browser. Rendering is covered by the component tests; what
is checked here is the contract between the built page and the gateway serving
it, which is exactly the seam those tests cannot see.

Usage:
    docker compose --env-file deploy/.env \\
      -f deploy/compose/docker-compose.yml \\
      -f deploy/compose/docker-compose.smoke.yml up -d --build
    set -a; . deploy/.env; set +a; python3 scripts/test_console_live.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from live_session import (
    GATEWAY,
    admin_credentials,
    check,
    login,
    new_session,
    request,
    skip,
    user_credentials,
)

FAILURES: list[str] = []


def expect(label: str, condition: bool, detail: str = "") -> None:
    if not check(label, condition, detail):
        FAILURES.append(label)


def fetch(opener: Any, path: str) -> tuple[int, dict[str, str], bytes]:
    return request(opener, f"{GATEWAY}{path}")


def main() -> int:
    anonymous = new_session()

    print("=== the console is served ===")
    status, headers, body = fetch(anonymous, "/console")
    expect("the entry document is served", status == 200, f"HTTP {status}")
    text = body.decode("utf-8", "replace")
    expect("it contains the mount point", 'id="root"' in text, text[:120])

    scripts = re.findall(r'src="(/console/assets/[^"]+\.js)"', text)
    expect("it references a hashed script", bool(scripts), text[:200])

    print()
    print("=== headers a page serving spend data needs ===")
    csp = headers.get("content-security-policy", "")
    expect("a content security policy is set", bool(csp), "" if csp else "no header")
    expect(
        "scripts may not be inline or evaluated",
        "'unsafe-inline'" not in _directive(csp, "script-src")
        and "'unsafe-eval'" not in _directive(csp, "script-src"),
        _directive(csp, "script-src"),
    )
    expect("the console cannot be framed", "frame-ancestors 'none'" in csp)
    expect(
        "the entry document is revalidated, never cached hard",
        "no-cache" in headers.get("cache-control", ""),
        headers.get("cache-control", "missing"),
    )

    if scripts:
        status, asset_headers, _ = fetch(anonymous, scripts[0])
        expect("the script is served", status == 200, f"HTTP {status}")
        expect(
            "hashed assets are cached immutably",
            "immutable" in asset_headers.get("cache-control", ""),
            asset_headers.get("cache-control", "missing"),
        )
        expect(
            "assets carry the security headers too",
            "content-security-policy" in asset_headers,
            "a policy on the page but not its scripts is a policy with a hole in it",
        )

    print()
    print("=== client-side routing ===")
    status, _, deep = fetch(anonymous, "/console/admin/reports")
    expect("a deep link returns the page, not a 404", status == 200, f"HTTP {status}")
    expect("and it is the same page", 'id="root"' in deep.decode("utf-8", "replace"))

    status, _, _ = fetch(anonymous, "/console/assets/index-deadbeef.js")
    expect("a missing asset is a 404, not HTML", status == 404, f"HTTP {status}")

    print()
    print("=== the API calls the page makes, with a real session ===")
    credentials = admin_credentials()
    if credentials is None:
        print("FAILED: PYSTINO_LIVE_ADMIN_PASSWORD is not set (see scripts/live_session.py)")
        return 1
    admin_session = login(*credentials)
    if admin_session is None:
        return 1
    user_opener: Any = None
    if user := user_credentials():
        user_opener = login(*user)
        if user_opener is None:
            return 1
    else:
        skip(
            "a non-admin is refused the admin report",
            "PYSTINO_LIVE_USER/_PASSWORD are not set — add a person in the console "
            "(Settings → Identity providers → People)",
        )

    status, _, body = fetch(admin_session, "/api/me")
    expect("/api/me answers", status == 200, f"HTTP {status}")
    profile = json.loads(body) if status == 200 else {}
    expect("it names the signed-in user", bool(profile.get("email")), str(profile)[:120])

    status, _, body = fetch(admin_session, "/api/me/reports/usage?group_by=model")
    expect("/api/me/reports/usage answers", status == 200, f"HTTP {status}")
    if status == 200:
        report = json.loads(body)
        expect("the report names its period", bool(report["period"]["label"]))
        expect(
            "and the timezone it was computed in",
            report["period"]["timezone"] == "Europe/Rome",
            report["period"]["timezone"],
        )
        # The console formats this string; it must never parse it into a float.
        expect(
            "the total is a decimal string, not a number",
            isinstance(report["totals"]["cost"], str),
            type(report["totals"]["cost"]).__name__,
        )
        print(f"  this month: {report['totals']['cost']} {report['currency']}")

    status, _, _ = fetch(admin_session, "/api/me/keys")
    expect("/api/me/keys answers", status == 200, f"HTTP {status}")

    status, _, _ = fetch(admin_session, "/api/me/reports/usage.csv")
    expect("the CSV export answers", status == 200, f"HTTP {status}")

    if user_opener is not None:
        print()
        print("=== a non-admin cannot reach the admin data behind /admin/* ===")
        status, _, _ = fetch(user_opener, "/api/admin/reports/usage")
        expect(
            "the non-admin is refused the admin report",
            status == 403,
            f"HTTP {status} — the console hides admin routes, but the API is what enforces it",
        )
        # The console renders the /admin/* shell for anyone, then shows "administrators
        # only" and calls nothing. The HTML being public is fine — it is an empty div
        # and a script tag; the data behind it is what needs a session.
        status, _, _ = fetch(user_opener, "/console/admin/quotas")
        expect("but the page itself still loads", status == 200, f"HTTP {status}")

    print()
    print("=== every endpoint the admin screens call ===")
    for label, path in [
        ("models", "/api/admin/models"),
        ("groups", "/api/admin/groups"),
        ("limits", "/api/admin/limits"),
        ("users", "/api/admin/users"),
        ("reports", "/api/admin/reports/usage?group_by=group"),
        ("reports CSV", "/api/admin/reports/usage.csv"),
    ]:
        status, _, _ = fetch(admin_session, path)
        expect(f"{label} answers for an admin", status == 200, f"HTTP {status}")

    # Price history and reset history are per-id, so they need something to point
    # at. Skipped rather than faked when the deployment has none.
    # Every listing answers with a pagination envelope now (ADR 0029), so the
    # rows are under `items`.
    models = json.loads(fetch(admin_session, "/api/admin/models")[2])["items"]
    if models:
        status, _, _ = fetch(admin_session, f"/api/admin/models/{models[0]['id']}/prices")
        expect("price history answers", status == 200, f"HTTP {status}")

    limits = json.loads(fetch(admin_session, "/api/admin/limits")[2])["items"]
    if limits:
        status, _, _ = fetch(admin_session, f"/api/admin/limits/{limits[0]['id']}/resets")
        expect("reset history answers", status == 200, f"HTTP {status}")
        expect(
            "a rule reports its consumption or says it cannot",
            "current_value" in limits[0],
            "an absent field is not the same as a null one",
        )

    print()
    print("=== pagination ===")
    _, _, body = fetch(admin_session, "/api/admin/models?limit=1")
    page = json.loads(body)
    expect(
        "a listing answers with an envelope",
        set(page) == {"items", "total", "limit", "offset"},
        str(sorted(page)),
    )
    expect(
        "which reports the match, not the page",
        page["total"] >= len(page["items"]),
        f"total {page['total']}, returned {len(page['items'])}",
    )

    # One row at a time must reach every row, exactly once. An off-by-one in
    # the offset loses a row in the middle of a catalogue, which cannot be seen
    # from the first page.
    walked: list[str] = []
    offset = 0
    while True:
        step = json.loads(fetch(admin_session, f"/api/admin/models?limit=1&offset={offset}")[2])
        walked += [entry["name"] for entry in step["items"]]
        offset += 1
        if offset >= step["total"]:
            break
    whole = json.loads(fetch(admin_session, "/api/admin/models?limit=200")[2])
    expect(
        "paging one row at a time yields the whole catalogue",
        sorted(walked) == sorted(entry["name"] for entry in whole["items"]),
        f"{sorted(walked)} vs {sorted(entry['name'] for entry in whole['items'])}",
    )
    expect("and no row twice", len(walked) == len(set(walked)), str(walked))

    hit = json.loads(fetch(admin_session, "/api/admin/models?q=model")[2])
    expect(
        "a search narrows the total, not only the page",
        hit["total"] <= whole["total"],
        f"{hit['total']} of {whole['total']}",
    )
    literal = json.loads(fetch(admin_session, "/api/admin/models?q=%25")[2])
    expect("a percent sign is searched for literally", literal["total"] == 0, str(literal["total"]))

    for bad in ("limit=0", "limit=201", "offset=-1"):
        status, _, _ = fetch(admin_session, f"/api/admin/models?{bad}")
        # Refused rather than clamped: silently returning a different window is
        # how a client treats a truncated list as complete.
        expect(f"{bad} is refused", status == 400, f"HTTP {status}")

    status, _, body = fetch(admin_session, "/api/admin/reports/usage?limit=1")
    expect(
        "a report is an aggregation and does not truncate",
        status == 200 and "items" not in json.loads(body),
        f"HTTP {status}",
    )

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("all checks passed")
    return 0


def _directive(csp: str, name: str) -> str:
    for part in csp.split("; "):
        if part.startswith(name):
            return part
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
