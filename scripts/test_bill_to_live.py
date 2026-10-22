#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27"]
# ///
"""`x-bill-to` against the running stack (ADR 0061).

The unit suite covers this on SQLite with a stub token. Two things it cannot
reach, and both are the point of this file:

* **A real access token.** The unit tests mint their own with a local key. Here
  the token comes from whatever identity provider the deployment is configured
  against, through the same code path a browser uses.
* **A membership no token describes.** The header must honour a group an
  administrator granted by hand, and refuse one the caller does not hold. That
  distinction lives in `memberships.source`, and getting it wrong is the
  mistake ADR 0057 records three times over — an implementation reading the
  token's `groups` claim passes every other check and fails this one.

Needs the bundled Authelia's device flow (see test_models_billing_live.py).
Source deploy/.env first, so PUBLIC_HOST and the credentials are set:

    set -a; . deploy/.env; set +a
    ./scripts/test_bill_to_live.py
"""

from __future__ import annotations

import os
import sys

import httpx

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILURES.append(label)
    return condition


def ok(label: str, detail: str = "") -> None:
    check(label, True, detail)


def skip(label: str, reason: str) -> None:
    print(f"  [skip] {label} — {reason}")


def origin() -> str:
    host = os.environ.get("PUBLIC_HOST")
    if host:
        port = os.environ.get("HTTPS_PORT", "443")
        return f"https://{host}:{port}"
    return "http://127.0.0.1:8000"


def ca_bundle() -> str | None:
    path = "deploy/tls/caddy-root.crt"
    return path if os.path.exists(path) else None


def device_flow_token() -> str | None:
    """An access token from the bundled Authelia's device flow, or None."""
    sys.path.insert(0, os.path.dirname(__file__))
    from test_models_billing_live import device_token, opener_and_verify

    op, _verify = opener_and_verify()
    return device_token(op)


def main() -> int:
    base = origin()
    bundle = ca_bundle()
    verify: str | bool = bundle if bundle else True
    print(f"origin: {base}\n")

    print("=== a token from the configured identity provider ===")
    token = device_flow_token()
    if token is None:
        skip(
            "the whole file",
            "no Authelia device flow; run against the bundled stack and source deploy/.env",
        )
        return 0
    ok("obtained an access token", f"{len(token)} chars")
    auth = {"Authorization": f"Bearer {token}"}

    with httpx.Client(base_url=base, verify=verify, timeout=60) as http:
        print("\n=== GET /v1/billing/groups ===")
        response = http.get("/v1/billing/groups", headers=auth)
        check(
            "a bearer caller may list its billable groups",
            response.status_code == 200,
            f"HTTP {response.status_code}",
        )
        if response.status_code != 200:
            return 1
        body = response.json()
        names = [g["name"] for g in body["data"]]
        defaults = [g["name"] for g in body["data"] if g["is_default"]]
        ok("groups", ", ".join(names) or "(none)")
        check("exactly one is the default", len(defaults) == 1, ", ".join(defaults))
        check(
            "it reports what this request bills",
            "billing_group" in body,
            str(body.get("billing_group")),
        )
        check("the list is sorted by name", names == sorted(names))

        # The manual-membership case. Skipped rather than provisioned: this
        # script checks a deployment and must not grant itself a group.
        print("\n=== a group the token does not name ===")
        manual = [n for n in names if n != body["billing_group"]]
        if not manual:
            skip(
                "billing to a second group",
                "this user has one group; grant a second with source=manual to "
                "exercise the case ADR 0057 records",
            )
        else:
            target = manual[0]
            response = http.get("/v1/billing/groups", headers={**auth, "x-bill-to": target})
            check(
                f"x-bill-to {target!r} changes what the request bills",
                response.status_code == 200 and response.json().get("billing_group") == target,
                f"HTTP {response.status_code}, billing_group="
                f"{response.json().get('billing_group') if response.status_code == 200 else '?'}",
            )
            check(
                "and the default is still reported as the default",
                response.status_code == 200
                and [g["name"] for g in response.json()["data"] if g["is_default"]] == defaults,
            )

        print("\n=== refusals ===")
        response = http.get(
            "/v1/billing/groups", headers={**auth, "x-bill-to": "no-such-group-anywhere"}
        )
        check(
            "a group that does not exist is refused",
            response.status_code == 403,
            f"HTTP {response.status_code}",
        )

        # Enumeration: a real group the caller does not hold must look identical
        # to one that does not exist, or the header lists the deployment.
        response2 = http.get("/v1/billing/groups", headers={**auth, "x-bill-to": "platform-admins"})
        if response2.status_code == 200:
            skip(
                "a group held by someone else is indistinguishable",
                "this caller is a member of 'platform-admins', so there is nothing to hide",
            )
        else:
            check(
                "and a real group the caller does not hold looks the same",
                response2.status_code == response.status_code
                and response2.json()["error"]["code"] == response.json()["error"]["code"],
                f"HTTP {response2.status_code}",
            )

        print("\n=== an API key is refused rather than quietly billed elsewhere ===")
        key = os.environ.get("GATEWAY_TEST_API_KEY", "")
        if not key:
            skip("the API key refusal", "set GATEWAY_TEST_API_KEY to check it")
        else:
            response = http.get(
                "/v1/billing/groups",
                headers={"Authorization": f"Bearer {key}", "x-bill-to": "research"},
            )
            check(
                "a key caller sending x-bill-to is refused",
                response.status_code == 403,
                f"HTTP {response.status_code}",
            )

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s) — " + "; ".join(FAILURES))
        return 1
    print("OK — a request may choose which group pays, and may not choose one it does not hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
