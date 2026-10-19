#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""The models listing follows the billing group, against the running stack.

The unit suite pins the rule on SQLite with a minted token. What only a live
stack can say is that a **real** access token — one the identity provider
itself issued, through the same device flow an agent machine enrolls with —
sees exactly the models its billing group can use, and that the header moves
the list the way the send path is billed:

* a person in two groups, with a model granted to the second group only;
* ``GET /v1/models`` **without** ``x-bill-to`` does not list it (once, this
  listed it: the old code scoped the list to every group the caller belongs
  to, while a request was admitted against the billing group only);
* with ``x-bill-to: <second group>`` it is listed, its detail answers, and a
  chat completion on it succeeds.

The token comes from the bundled Authelia's RFC 8622 device flow for the
machine-enrollment client (``opencode-enrollment`` — ADR 0089's client, whose
tokens the gateway already accepts on ``/v1``), approved through the same
portal API a browser drives. A Keycloak deployment has no Authelia portal;
that is what ``test_bill_to_live.py`` is for.

The second group, the model granted to it alone, and the membership are set
up through the management API (an administrator's grant, source ``manual``),
so the run is repeatable on a stack that starts bare.

Source deploy/.env first, so PUBLIC_ORIGIN and the credentials are set. A
trial stack with no DNS needs the same two exports the other live checks
take (see docs/operations.md):

    set -a; . deploy/.env; set +a
    export PYSTINO_LIVE_ADMIN_PASSWORD=… PYSTINO_LIVE_RESOLVE=127.0.0.1 \
           GATEWAY_CA_BUNDLE=$PWD/deploy/tls/caddy-root.crt
    ./scripts/test_models_billing_live.py
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

sys.path.insert(0, os.path.dirname(__file__))

from live_session import (  # noqa: E402
    LocalhostSecureCookiePolicy,
    NoRedirect,
    admin_credentials,
)

FAILURES: list[str] = []

SECOND_GROUP = "models-live-second"
SECOND_MODEL = "second-group-model"


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
    public = os.environ.get("PUBLIC_ORIGIN")
    if public:
        return public.rstrip("/")
    host = os.environ.get("PUBLIC_HOST")
    if host:
        port = os.environ.get("HTTPS_PORT", "443")
        return f"https://{host}:{port}"
    return "http://127.0.0.1:8000"


def ca_bundle() -> str | None:
    path = os.environ.get("GATEWAY_CA_BUNDLE")
    if path and os.path.exists(path):
        return path
    path = "deploy/tls/caddy-root.crt"
    return path if os.path.exists(path) else None


def opener_and_verify() -> tuple[urllib.request.OpenerDirector, ssl.SSLContext | bool]:
    bundle = ca_bundle()
    context = ssl.create_default_context(cafile=bundle) if bundle else ssl.create_default_context()
    jar = CookieJar(policy=LocalhostSecureCookiePolicy())
    return (
        urllib.request.build_opener(
            NoRedirect, urllib.request.HTTPCookieProcessor(jar), urllib.request.HTTPSHandler(context=context)
        ),
        context if bundle else True,
    )


def request(
    op: urllib.request.OpenerDirector,
    url: str,
    method: str = "GET",
    data: dict | None = None,
    headers: dict | None = None,
    form: bool = False,
) -> tuple[int, str, str]:
    """One request through the opener. Returns (status, location, body)."""
    body = None
    headers = dict(headers or {})
    if data is not None:
        body = urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()
        headers["content-type"] = (
            "application/x-www-form-urlencoded" if form else "application/json"
        )
    req = urllib.request.Request(url, data=body, method=method)
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with op.open(req) as response:
            return response.status, response.headers.get("location", ""), response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get("location", ""), error.read().decode()
    except urllib.error.URLError as error:
        return 0, "", f"unreachable: {error.reason}"


def idp_base() -> str:
    """The bundled Authelia sits under /authelia on the public origin."""
    return f"{origin().rstrip('/')}/authelia"


def device_token(op: urllib.request.OpenerDirector) -> str | None:
    """A real access token for the signed-in person, by the device flow.

    The same walk an agent machine's enrollment drives, with the approval
    done through the portal API a browser would drive: first factor, then
    the device consent, then the user authorization that marks the session
    approved. Authelia's token endpoint rate-limits polling below its
    advertised interval, so this waits the full interval between polls.
    """
    base = idp_base()
    origin_ = origin().rstrip("/")

    # Discovery names the device endpoint only when the IdP speaks RFC 8628;
    # anything else is what test_bill_to_live.py's Keycloak path is for.
    status, _, body = request(op, f"{base}/.well-known/openid-configuration")
    if status != 200:
        skip("a token from the identity provider", f"discovery unreachable (HTTP {status})")
        return None
    doc = json.loads(body)
    if not doc.get("device_authorization_endpoint"):
        skip(
            "a token from the identity provider",
            "the IdP does not advertise the device flow; configure a Keycloak "
            "direct grant (deploy/dev/keycloak/compose.yml) and use "
            "test_bill_to_live.py",
        )
        return None

    status, _, body = request(
        op,
        f"{base}/api/oidc/device-authorization",
        method="POST",
        data={
            "client_id": "opencode-enrollment",
            "scope": "openid profile email groups offline_access",
        },
        form=True,
    )
    if status != 200:
        skip("a token from the identity provider", f"device authorization refused (HTTP {status})")
        return None
    grant = json.loads(body)
    ok("the identity provider started a device authorization", grant["user_code"])

    # The portal session. The walk is the browser's: /auth/login, the login
    # redirect, first factor, pre-configured consent, back to the callback.
    status, location, _ = request(op, f"{origin_}/auth/login?provider=default")
    if status != 302:
        skip("a signed-in portal session", f"/auth/login answered HTTP {status}")
        return None
    signed_in = False
    auth_url = location
    for _ in range(6):
        # Always re-request the authorize URL: after the first factor the
        # same URL now answers with the consent (pre-configured clients skip
        # it) and then the gateway callback, which is what a browser does.
        status, location, body = request(op, auth_url)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(location).query))
        if "consent" in location and query.get("flow_id"):
            _, _, info = request(
                op, f"{base}/api/oidc/consent?flow_id={query['flow_id']}", headers={"origin": origin_}
            )
            claims = (json.loads(info).get("data") or {}).get("claims") or []
            status, _, decision = request(
                op,
                f"{base}/api/oidc/consent",
                method="POST",
                data={
                    "flow_id": query["flow_id"],
                    "client_id": "pystino-console",
                    "consent": True,
                    "pre_configure": True,
                    "claims": claims,
                },
                headers={"origin": origin_},
            )
            if status != 200:
                skip("a signed-in portal session", f"consent refused (HTTP {status})")
                return None
            auth_url = json.loads(decision)["data"]["redirect_uri"]
            continue
        if query.get("flow_id") and location.startswith(f"{origin_}/authelia/?"):
            credentials = admin_credentials()
            if credentials is None:
                skip("a signed-in portal session", "PYSTINO_LIVE_ADMIN_PASSWORD is not set")
                return None
            status, _, _ = request(
                op,
                f"{base}/api/firstfactor",
                method="POST",
                data={
                    "username": credentials[0],
                    "password": credentials[1],
                    "keepMeLoggedIn": False,
                    "flow": "openid_connect",
                    "flowID": query["flow_id"],
                },
                headers={"origin": origin_},
            )
            continue
        if "/auth/callback" in location:
            # The gateway's callback is what sets the session cookie this
            # whole walk was for: follow it, then stop.
            request(op, location)
            signed_in = True
            break
        if status == 200:
            signed_in = True
            break
    if not signed_in:
        skip("a signed-in portal session", f"the sign-in walk stalled at {location[:80]}")
        return None
    ok("a signed-in portal session")

    # The person confirms the code: consent first (it mints the consent
    # session), then the user authorization that marks the device session
    # approved. Skipping the second step leaves the token polling forever.
    status, _, body = request(
        op,
        f"{base}/api/oidc/consent",
        method="POST",
        data={
            "client_id": "opencode-enrollment",
            "user_code": grant["user_code"],
            "subflow": "device_authorization",
            "consent": True,
        },
        headers={"origin": origin_},
    )
    if status != 200:
        skip("the device authorization approved", f"consent refused (HTTP {status})")
        return None
    flow_id = (json.loads(body).get("data") or {}).get("flow_id")
    status, _, body = request(
        op,
        f"{base}/api/oidc/device-authorization",
        method="PUT",
        data={
            "client_id": "opencode-enrollment",
            "user_code": grant["user_code"],
            "flow_id": flow_id,
        },
        form=True,
    )
    if status != 200:
        skip("the device authorization approved", f"user authorization answered HTTP {status}")
        return None
    ok("the device authorization approved")

    interval = grant.get("interval", 5)
    deadline = time.time() + 60
    while time.time() < deadline:
        status, _, body = request(
            op,
            f"{base}/api/oidc/token",
            method="POST",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": "opencode-enrollment",
                "device_code": grant["device_code"],
            },
            form=True,
        )
        payload = json.loads(body)
        if "access_token" in payload:
            return payload["access_token"]
        error = payload.get("error")
        if error not in ("authorization_pending", "slow_down"):
            skip("the token endpoint answered", f"{error}: {payload.get('error_description', '')[:120]}")
            return None
        time.sleep(interval)
    skip("the token endpoint answered", "the device code was never exchanged")
    return None


def admin_api(op: urllib.request.OpenerDirector, path: str, method: str = "GET", data: dict | None = None):
    status, _, body = request(op, f"{origin().rstrip('/')}{path}", method=method, data=data)
    try:
        parsed = json.loads(body) if body else None
    except json.JSONDecodeError:
        parsed = None
    return status, parsed


def setup_second_group_and_model(op: urllib.request.OpenerDirector) -> tuple[str, str] | None:
    """The two-group shape, through the management API.

    A manual group is the one kind an administrator's screen may edit (nobody's
    directory reconciles it away), the model rides the seeded provider priced
    like `gateway seed` prices one, and the grant is the second group's alone:
    the admin's own default group must NOT hold it, or the first assertion
    would be about a grant, not about the billing group.
    """
    status, groups = admin_api(op, "/api/admin/groups?limit=200")
    if status != 200:
        skip("the second group", f"the group listing answered HTTP {status}")
        return None
    second = next((g for g in groups["items"] if g["name"] == SECOND_GROUP), None)
    if second is None:
        status, second = admin_api(op, "/api/admin/groups", method="POST", data={"name": SECOND_GROUP})
        if status != 201:
            skip("the second group", f"creating it answered HTTP {status}")
            return None
    ok(f"a second group ({SECOND_GROUP})")

    status, models = admin_api(op, "/api/admin/models?limit=200")
    if status != 200:
        skip("the second group's model", f"the model listing answered HTTP {status}")
        return None
    model = next((m for m in models["items"] if m["name"] == SECOND_MODEL), None)
    if model is None:
        status, providers = admin_api(op, "/api/admin/providers")
        if status != 200:
            skip("the second group's model", f"the provider listing answered HTTP {status}")
            return None
        provider = next((p for p in providers["items"] if p.get("is_active")), None)
        if provider is None:
            skip("the second group's model", "no active provider (run `gateway seed` first)")
            return None
        status, model = admin_api(
            op,
            "/api/admin/models",
            method="POST",
            data={
                "name": SECOND_MODEL,
                "upstream_model": "smoke-model",
                "provider_id": provider["id"],
            },
        )
        if status != 201:
            skip("the second group's model", f"creating it answered HTTP {status}: {model}")
            return None
        status, _ = admin_api(
            op,
            f"/api/admin/models/{model['id']}/prices",
            method="POST",
            data={"input_per_mtok": "1", "output_per_mtok": "2"},
        )
        if status != 201:
            skip("the second group's model", f"pricing it answered HTTP {status}")
            return None
    ok(f"a model granted to the second group only ({SECOND_MODEL})")

    # The grant is the second group's alone. A previous run may have left the
    # model granted wider; the point of the check needs exactly one holder.
    status, groups = admin_api(op, "/api/admin/groups?limit=200")
    for group in groups["items"]:
        if group["name"] == SECOND_GROUP:
            continue
        status, _ = admin_api(
            op, f"/api/admin/groups/{group['id']}/models/{model['id']}", method="DELETE"
        )
    status, _ = admin_api(
        op, f"/api/admin/groups/{second['id']}/models/{model['id']}", method="PUT"
    )
    if status != 204:
        skip("the grant", f"granting the model to the second group answered HTTP {status}")
        return None

    # The person joins the second group as an administrator's manual grant:
    # no login will reconcile it away (ADR 0057), and — the point — no token
    # names it, so a listing that read the token's claims instead of the
    # billing group would fail here too.
    _, _, me_raw = request(op, f"{origin().rstrip('/')}/api/me")
    me = json.loads(me_raw)
    status, _ = admin_api(
        op,
        f"/api/admin/groups/{second['id']}/members",
        method="POST",
        data={"user_id": me["id"]},
    )
    if status not in (204, 400):  # 400 = already a member, from an earlier run
        skip("the membership", f"adding the person answered HTTP {status}")
        return None
    ok("the person is a member of the second group (a grant no token describes)")
    return second["id"], model["id"]


def main() -> int:
    base = origin()
    print(f"origin: {base}\n")

    op, verify = opener_and_verify()

    print("=== a real access token, by the device flow ===")
    token = device_token(op)
    if token is None:
        return 0

    auth = {"Authorization": f"Bearer {token}"}

    def v1(path: str, extra: dict | None = None, method: str = "GET", data: dict | None = None):
        headers = {**auth, **(extra or {})}
        status, _, body = request(
            op,
            f"{base}{path}",
            method=method,
            data=data,
            headers=headers,
        )
        try:
            parsed = json.loads(body) if body else None
        except json.JSONDecodeError:
            parsed = None
        return status, parsed

    status, me = v1("/v1/me")
    check("the token authenticates to /v1", status == 200, f"HTTP {status}")
    if status != 200:
        return 1
    ok(f"the person is {me.get('email')} (billing group {me.get('billing_group')})")

    print("\n=== the two-group shape, through the management API ===")
    ids = setup_second_group_and_model(op)
    if ids is None:
        return 1
    second_id, model_id = ids

    print("\n=== the listing follows the billing group ===")
    status, listing = v1("/v1/models")
    names = [entry["id"] for entry in (listing or {}).get("data", [])] if status == 200 else []
    check(
        f"{SECOND_MODEL} is off the list without x-bill-to",
        status == 200 and SECOND_MODEL not in names,
        f"HTTP {status}, listed: {names}",
    )
    status, _ = v1(f"/v1/models/{SECOND_MODEL}")
    check(
        "and its detail route refuses too",
        status == 404,
        f"HTTP {status}",
    )

    status, listing = v1("/v1/models", extra={"x-bill-to": SECOND_GROUP})
    names = [entry["id"] for entry in (listing or {}).get("data", [])] if status == 200 else []
    check(
        f"x-bill-to: {SECOND_GROUP} lists it",
        status == 200 and SECOND_MODEL in names,
        f"HTTP {status}, listed: {names}",
    )
    status, _ = v1(f"/v1/models/{SECOND_MODEL}", extra={"x-bill-to": SECOND_GROUP})
    check("and its detail route answers for it", status == 200, f"HTTP {status}")

    print("\n=== a request billed to the second group succeeds ===")
    status, completion = v1(
        "/v1/chat/completions",
        extra={"x-bill-to": SECOND_GROUP},
        method="POST",
        data={"model": SECOND_MODEL, "messages": [{"role": "user", "content": "hi"}]},
    )
    if status == 429:
        skip("a chat completion on the second group's model", "a quota is exhausted in this deployment")
    else:
        check(
            "a chat completion on the second group's model succeeds",
            status == 200,
            f"HTTP {status}: {str(completion)[:160]}",
        )

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}")
        for label in FAILURES:
            print(f"  - {label}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
