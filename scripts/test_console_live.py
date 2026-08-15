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
      -f deploy/compose/docker-compose.smoke.yml \\
      -f deploy/compose/docker-compose.keycloak.yml up -d --build
    python3 scripts/test_console_live.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from test_oidc_flow import GATEWAY, check, login, new_session, request

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
    expect('it contains the mount point', 'id="root"' in text, text[:120])

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
    expect('and it is the same page', 'id="root"' in deep.decode("utf-8", "replace"))

    status, _, _ = fetch(anonymous, "/console/assets/index-deadbeef.js")
    expect("a missing asset is a 404, not HTML", status == 404, f"HTTP {status}")

    print()
    print("=== the API calls the page makes, with a real session ===")
    alice = login("alice")
    if alice is None:
        return 1

    status, _, body = fetch(alice, "/api/me")
    expect("/api/me answers", status == 200, f"HTTP {status}")
    profile = json.loads(body) if status == 200 else {}
    expect("it names the signed-in user", bool(profile.get("email")), str(profile)[:120])

    status, _, body = fetch(alice, "/api/me/reports/usage?group_by=model")
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
        print(f"  alice this month: {report['totals']['cost']} {report['currency']}")

    status, _, _ = fetch(alice, "/api/me/keys")
    expect("/api/me/keys answers", status == 200, f"HTTP {status}")

    status, _, _ = fetch(alice, "/api/me/reports/usage.csv")
    expect("the CSV export answers", status == 200, f"HTTP {status}")

    print()
    print("=== a non-admin cannot reach the admin data behind /admin/* ===")
    status, _, _ = fetch(alice, "/api/admin/reports/usage")
    expect(
        "alice is refused the admin report",
        status == 403,
        f"HTTP {status} — the console hides admin routes, but the API is what enforces it",
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
