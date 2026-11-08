#!/usr/bin/env python3
"""Privacy routing against the running stack (ADR 0113).

What only this can prove, that the unit suite on SQLite cannot:

* the rules, the profiles and the `routing` log entry round-trip through
  **PostgreSQL** (a JSON column read with `->`, the migration's seeded profile);
* an embedding rule reroutes to a provider marked local and the *fake upstream
  receives the local model's name*, or, for a refusal, receives nothing at all;
* `/v1/ocr` reads a real PDF on the real extractor service: a file-name rule
  refuses before anything is read, a scan rule reads first and then refuses (and
  lets a PDF with a text layer through), and a reader rule's `X-Extractor-Engine`
  reaches the service, whose own answer (and fallback reason) lands in the log;
* every refusal is a 403 naming the rule and never the content.

Usage: see test_providers_live.py (same stack, same environment). Needs the
`redaction` profile (the extractor service) and the bundled Authelia.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from live_session import GATEWAY, admin_credentials, check, login, request, skip, user_credentials
from test_providers_live import api, sql
from test_surfaces_live import call, key_for

FAILURES: list[str] = []
UPSTREAM = "http://localhost:8081"
SUMMARY = {"x-pystino-redaction-summary": "1"}
HEALTH = "Il paziente presenta una diagnosi di diabete di tipo 2 e ipertensione."
PLAIN = "La riunione di lunedi parlera del budget trimestrale e delle ferie."
CLOUD, LOCAL = "route-embed-cloud", "route-embed-local"


def expect(label: str, condition: bool, detail: str = "") -> None:
    if not check(label, condition, detail):
        FAILURES.append(label)


def pdf(text: str | None) -> str:
    """A one-page PDF as a data URI: with a text layer, or blank (a "scan")."""
    stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET" if text else ""
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = "%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    out += "".join(f"{offset:010d} 00000 n \n" for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return "data:application/pdf;base64," + base64.b64encode(out.encode("latin-1")).decode()


def last_upstream() -> dict[str, Any]:
    import urllib.request

    seen: dict[str, Any] = json.loads(
        urllib.request.urlopen(f"{UPSTREAM}/_last_request", timeout=30).read()
    )
    return seen


def ensure_embedding(dave: Any, name: str, provider_id: str, group_id: str | None) -> str | None:
    models = api(dave, "/api/admin/models?limit=200")[1]["items"]
    found = next((m for m in models if m["name"] == name), None)
    if found is None:
        status, found = api(
            dave,
            "/api/admin/models",
            json_body={
                "name": name,
                "upstream_model": f"upstream/{name}",
                "provider_id": provider_id,
                "kind": "embedding",
            },
            method="POST",
        )
        if status != 201:
            expect(f"catalogued {name}", False, f"HTTP {status}: {found}")
            return None
    prices = api(dave, f"/api/admin/models/{found['id']}/prices")[1]["items"]
    if not prices:
        api(
            dave,
            f"/api/admin/models/{found['id']}/prices",
            json_body={"input_per_mtok": "0", "output_per_mtok": "0"},
            method="POST",
        )
    if group_id:
        api(dave, f"/api/admin/groups/{group_id}/models/{found['id']}", method="PUT")
    return str(found["id"])


def rule(dave: Any, body: dict[str, Any]) -> dict[str, Any]:
    status, created = api(dave, "/api/admin/privacy/rules", json_body=body, method="POST")
    expect(f"saved rule {body['name']!r}", status == 201, f"HTTP {status}: {created}")
    return created if isinstance(created, dict) else {}


def main() -> int:
    credentials = admin_credentials()
    if credentials is None:
        print("FAILED: PYSTINO_LIVE_ADMIN_PASSWORD is not set (see scripts/live_session.py)")
        return 1
    dave = login(*credentials)
    if dave is None:
        return 1
    user = user_credentials()
    alice = login(*user) if user else dave
    if alice is None:
        return 1
    if not user:
        skip("a non-admin session", "using the admin's own key")
    secret = key_for(alice, "routing-check")
    if secret is None:
        return 1

    print("=== the migration seeded the default profile; the API sees PostgreSQL ===")
    status, profiles = api(dave, "/api/admin/privacy/profiles")
    names = [p["name"] for p in profiles["items"]] if status == 200 else []
    expect("the default profile Fast exists", "Fast" in names, str(names))
    expect(
        "and it is available and can decide",
        status == 200
        and any(
            p["name"] == "Fast" and p["status"] == "available" and p["decides"]
            for p in profiles["items"]
        ),
    )

    print()
    print("=== a local provider, two embedding models ===")
    providers = api(dave, "/api/admin/providers?limit=200")[1]["items"]
    cloud_provider = next(
        (p for p in providers if p["is_active"] and p["plugin"] != "extractor"), None
    )
    if cloud_provider is None:
        expect("an active provider exists", False)
        return 1
    local_provider = next((p for p in providers if p["name"] == "route-local"), None)
    if local_provider is None:
        status, local_provider = api(
            dave,
            "/api/admin/providers",
            json_body={
                "name": "route-local",
                "base_url": cloud_provider["base_url"],
                "is_local": True,
            },
            method="POST",
        )
        expect(
            "created a provider marked local",
            status == 201 and local_provider["is_local"],
            str(local_provider)[:120],
        )
    _, _, me_raw = request(alice, f"{GATEWAY}/api/me")
    group = ((json.loads(me_raw)).get("default_billing_group") or {}).get("id")
    if ensure_embedding(dave, CLOUD, cloud_provider["id"], group) is None:
        return 1
    if ensure_embedding(dave, LOCAL, local_provider["id"], group) is None:
        return 1

    # Start from no rules, so earlier runs and other checks cannot interfere.
    for existing in api(dave, "/api/admin/privacy/rules")[1]["items"]:
        api(dave, f"/api/admin/privacy/rules/{existing['id']}", method="DELETE")

    print()
    print("=== embedding: health text is rerouted to the local model, plain text is not ===")
    health_rule = rule(
        dave,
        {
            "kind": "embed",
            "name": "Health stays local",
            "conditions": [
                {"key": "verdict", "values": ["health"], "decide": "patterns"},
                {"key": "target", "values": ["cloud"]},
            ],
            "action": {"type": "local", "model": LOCAL},
        },
    )
    status, body = call(secret, "/v1/embeddings", {"model": CLOUD, "input": [HEALTH]}, sse=False)
    expect("a health text is embedded", status == 200, f"HTTP {status}: {str(body)[:200]}")
    expect(
        "by the local model: the upstream received its name",
        last_upstream().get("model") == f"upstream/{LOCAL}",
        str(last_upstream().get("model")),
    )
    expect(
        "and the caller still sees the name it sent",
        isinstance(body, dict) and body.get("model") == CLOUD,
    )
    status, body = call(secret, "/v1/embeddings", {"model": CLOUD, "input": [PLAIN]})
    expect(
        "plain text goes to the model the caller named",
        status == 200 and last_upstream().get("model") == f"upstream/{CLOUD}",
        str(last_upstream().get("model")),
    )

    print()
    print("=== embedding: the summary and the log carry the routing block ===")
    import urllib.request

    request_obj = urllib.request.Request(
        f"{GATEWAY}/v1/embeddings",
        data=json.dumps({"model": CLOUD, "input": [HEALTH]}).encode(),
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {secret}",
            **SUMMARY,
        },
        method="POST",
    )
    with urllib.request.urlopen(request_obj, timeout=60) as response:
        summary = json.loads(response.read()).get("pystino_redaction", {})
    routing = summary.get("routing", {})
    expect(
        "the summary names the kind and the outcome",
        routing.get("kind") == "embed" and routing.get("outcome") == "rerouted",
        str(routing)[:200],
    )
    expect(
        "and the rule",
        (routing.get("handled", {}).get("routing_rule") or {}).get("name") == "Health stays local",
    )
    row = sql(
        "select redaction_detail->'routing'->>'kind', redaction_detail->'routing'->>'outcome', "
        "redaction_detail->'routing'->'handled'->'routing_rule'->>'name' "
        "from usage_records where api_surface = 'embeddings' and redaction_detail is not null "
        "order by created_at desc limit 1;"
    )
    expect(
        "PostgreSQL stores it and can read it back", row == "embed|rerouted|Health stays local", row
    )

    print()
    print("=== embedding: refuse is a 403 naming the rule, and nothing reaches the upstream ===")
    api(dave, f"/api/admin/privacy/rules/{health_rule['id']}", method="DELETE")
    rule(
        dave,
        {
            "kind": "embed",
            "name": "No health text in the cloud",
            "conditions": [
                {"key": "verdict", "values": ["health"]},
                {"key": "target", "values": ["cloud"]},
            ],
            "action": {"type": "refuse"},
        },
    )
    before = last_upstream()
    status, body = call(
        secret, "/v1/embeddings", {"model": CLOUD, "input": [HEALTH + " ZZ-MARKER"]}
    )
    text = json.dumps(body)
    expect("it is a 403", status == 403, f"HTTP {status}")
    expect("naming the rule", "No health text in the cloud" in text)
    expect("and never the content", "diabete" not in text and "ZZ-MARKER" not in text)
    expect("the upstream was not called", last_upstream() == before)
    blocked = sql(
        "select status, error_code from usage_records where api_surface = 'embeddings' "
        "order by created_at desc limit 1;"
    )
    expect("the ledger has a blocked row", blocked == "blocked|routing_refused", blocked)

    print()
    print("=== reading: a file-name rule refuses before anything is read ===")
    for existing in api(dave, "/api/admin/privacy/rules")[1]["items"]:
        api(dave, f"/api/admin/privacy/rules/{existing['id']}", method="DELETE")
    rule(
        dave,
        {
            "kind": "read",
            "name": "Medical files stay",
            "conditions": [{"key": "filename", "values": ["referto*"]}],
            "action": {"type": "refuse"},
        },
    )
    models = api(dave, "/api/admin/models?limit=200")[1]["items"]
    reader = next(
        (m for m in models if m["kind"] == "ocr" and m["provider_name"] == "extractor"), None
    )
    if reader is None:
        skip(
            "reading rules", "no local extractor model is catalogued (start the redaction profile)"
        )
    else:
        text_pdf = pdf("Referto di prova")
        status, body = call(
            secret,
            "/v1/ocr",
            {
                "model": reader["name"],
                "document": {
                    "type": "document_url",
                    "document_url": text_pdf,
                    "document_name": "referto-rossi.pdf",
                },
            },
        )
        expect(
            "a file named referto* is refused",
            status == 403 and "Medical files stay" in json.dumps(body),
            f"HTTP {status}",
        )
        expect("without echoing its name", "referto-rossi" not in json.dumps(body))
        status, body = call(
            secret,
            "/v1/ocr",
            {
                "model": reader["name"],
                "document": {
                    "type": "document_url",
                    "document_url": text_pdf,
                    "document_name": "fattura.pdf",
                },
            },
        )
        expect(
            "another file is read",
            status == 200 and "Referto di prova" in json.dumps(body),
            f"HTTP {status}: {str(body)[:160]}",
        )

        print()
        print("=== reading: a scan rule reads first, then refuses a scan and passes a text PDF ===")
        for existing in api(dave, "/api/admin/privacy/rules")[1]["items"]:
            api(dave, f"/api/admin/privacy/rules/{existing['id']}", method="DELETE")
        rule(
            dave,
            {
                "kind": "read",
                "name": "No scans",
                "conditions": [{"key": "filetype", "values": ["scan"]}],
                "action": {"type": "refuse"},
            },
        )
        status, body = call(
            secret,
            "/v1/ocr",
            {
                "model": reader["name"],
                "document": {"type": "document_url", "document_url": pdf(None)},
            },
        )
        expect(
            "a PDF with no text layer is refused by the rule",
            status == 403 and "No scans" in json.dumps(body),
            f"HTTP {status}: {str(body)[:200]}",
        )
        status, body = call(
            secret,
            "/v1/ocr",
            {
                "model": reader["name"],
                "document": {"type": "document_url", "document_url": pdf("Testo normale")},
            },
        )
        expect("a PDF with text goes through", status == 200, f"HTTP {status}: {str(body)[:200]}")

        print()
        print("=== reading: a reader rule's engine reaches the extractor, and it is logged ===")
        for existing in api(dave, "/api/admin/privacy/rules")[1]["items"]:
            api(dave, f"/api/admin/privacy/rules/{existing['id']}", method="DELETE")
        rule(
            dave,
            {
                "kind": "read",
                "name": "PDFs with Docling",
                "conditions": [{"key": "filetype", "values": ["pdf"]}],
                "action": {"type": "reader", "reader": "docling"},
            },
        )
        request_obj = urllib.request.Request(
            f"{GATEWAY}/v1/ocr",
            data=json.dumps(
                {
                    "model": reader["name"],
                    "document": {"type": "document_url", "document_url": pdf("Con Docling")},
                }
            ).encode(),
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {secret}",
                **SUMMARY,
            },
            method="POST",
        )
        with urllib.request.urlopen(request_obj, timeout=180) as response:
            payload = json.loads(response.read())
        routing = payload.get("pystino_redaction", {}).get("routing", {})
        handled = routing.get("handled", {})
        expect("the request was read", bool(payload.get("pages")), str(payload)[:160])
        expect(
            "the rule is recorded",
            (handled.get("routing_rule") or {}).get("name") == "PDFs with Docling",
            str(handled),
        )
        expect("with the reader it asked for", handled.get("reader") == "docling")
        # The pattern image has no Docling: the extractor falls back and says why.
        expect(
            "and the extractor's own answer (the fallback reason) is in the log",
            bool(handled.get("extractor")) and "docling" in (handled.get("fallback_reason") or ""),
            str(handled),
        )
        row = sql(
            "select redaction_detail->'routing'->'observed'->>'filetype', "
            "redaction_detail->'routing'->'handled'->>'extractor' "
            "from usage_records where api_surface = 'ocr' and redaction_detail is not null "
            "order by created_at desc limit 1;"
        )
        expect("PostgreSQL has the file type and the extractor", row.startswith("pdf|"), row)

    print()
    print("=== cleanup ===")
    for existing in api(dave, "/api/admin/privacy/rules")[1]["items"]:
        api(dave, f"/api/admin/privacy/rules/{existing['id']}", method="DELETE")
    remaining = api(dave, "/api/admin/privacy/rules")[1]["total"]
    expect("no rules are left behind", remaining == 0, str(remaining))
    audit = api(dave, "/api/admin/audit?target_type=privacy_rule&limit=200")[1]
    expect("every change was audited", audit["total"] >= 8, str(audit["total"]))

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("all privacy routing checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
