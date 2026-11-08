#!/usr/bin/env python3
"""Privacy pipelines against the running stack (ADR 0113).

What only this can prove, that the unit suite on SQLite cannot:

* pipelines, policies, profiles and the `routing` log entry round-trip through
  **PostgreSQL** (a JSON column read with `->`, the migration's seeded profile);
* an embedding pipeline refuses health data and the *fake upstream receives
  nothing at all*; a pipeline that redacts with a policy hides what it names;
* `/v1/ocr` reads a real PDF on the real extractor service: a file-name rule
  refuses before anything is read, a scan rule reads first on the built-in
  extractor and then refuses (and lets a PDF with a text layer through), and a
  "read with" rule's engine reaches the service, whose own answer (and fallback
  reason) lands in the log;
* every refusal is a 403 naming the rule and its pipeline and never the content.

Usage: see test_providers_live.py (same stack, same environment). Needs the
`redaction` profile (the extractor service) and the bundled Authelia.
"""

from __future__ import annotations

import base64
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from live_session import GATEWAY, admin_credentials, check, login, skip, user_credentials
from test_providers_live import api, sql
from test_surfaces_live import call, key_for

FAILURES: list[str] = []
UPSTREAM = "http://localhost:8081"
SUMMARY = {"x-pystino-redaction-summary": "1"}
HEALTH = "Il paziente presenta una diagnosi di diabete di tipo 2 e ipertensione."
PLAIN = "La riunione di lunedi parlera del budget trimestrale e delle ferie."
EMBED = "route-embed"


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


def pipeline(
    dave: Any, kind: str, name: str, rules: list[dict[str, Any]], **extra: Any
) -> dict[str, Any]:
    body = {"kind": kind, "name": name, "rules": rules, **extra}
    status, created = api(dave, "/api/admin/privacy/pipelines", json_body=body, method="POST")
    expect(f"saved pipeline {name!r}", status == 201, f"HTTP {status}: {created}")
    return created if isinstance(created, dict) else {}


def rule(name: str, conditions: list[dict[str, Any]], **action: Any) -> dict[str, Any]:
    return {
        "name": name,
        "conditions": conditions,
        "action": action or {"level": "refuse"},
    }


def wipe(dave: Any) -> None:
    for existing in api(dave, "/api/admin/privacy/pipelines?limit=200")[1]["items"]:
        api(dave, f"/api/admin/privacy/pipelines/{existing['id']}", method="DELETE")
    for existing in api(dave, "/api/admin/privacy/policies?limit=200")[1]["items"]:
        api(dave, f"/api/admin/privacy/policies/{existing['id']}", method="DELETE")


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
    wipe(dave)

    print()
    print("=== embedding: a pipeline refuses health data and nothing reaches the upstream ===")
    providers = api(dave, "/api/admin/providers?limit=200")[1]["items"]
    provider = next((p for p in providers if p.get("plugin") != "extractor"), None)
    if provider is None:
        print("FAILED: no provider is catalogued")
        return 1
    group = (api(dave, "/api/me")[1] or {}).get("default_billing_group", {}) or {}
    if ensure_embedding(dave, EMBED, provider["id"], group.get("id")) is None:
        return 1
    pipeline(
        dave,
        "embed",
        "Remote embeddings",
        [rule("No health text", [{"key": "health"}])],
    )
    before = last_upstream()
    status, body = call(
        secret, "/v1/embeddings", {"model": EMBED, "input": [HEALTH + " ZZ-MARKER"]}
    )
    text = json.dumps(body)
    expect("it is a 403", status == 403, f"HTTP {status}")
    expect(
        "naming the rule and the pipeline", "No health text" in text and "Remote embeddings" in text
    )
    expect("and never the content", "diabete" not in text and "ZZ-MARKER" not in text)
    expect("the upstream was not called", last_upstream() == before)
    blocked = sql(
        "select status, error_code from usage_records where api_surface = 'embeddings' "
        "order by created_at desc limit 1;"
    )
    expect("the ledger has a blocked row", blocked == "blocked|routing_refused", blocked)
    status, body = call(secret, "/v1/embeddings", {"model": EMBED, "input": [PLAIN]})
    expect("plain text is embedded", status == 200, f"HTTP {status}: {str(body)[:120]}")
    row = sql(
        "select redaction_detail->'routing'->>'kind', redaction_detail->'routing'->>'outcome' "
        "from usage_records where api_surface = 'embeddings' and status = 'completed' "
        "and redaction_detail is not null order by created_at desc limit 1;"
    )
    expect("PostgreSQL stores the routing block and can read it back", row == "embed|default", row)

    print()
    print("=== reading: a file-name rule refuses before anything is read ===")
    wipe(dave)
    pipeline(
        dave,
        "read",
        "Reports",
        [rule("Medical files stay", [{"key": "filename", "values": ["referto*"]}])],
    )
    models = api(dave, "/api/admin/models?limit=200")[1]["items"]
    reader = next(
        (m for m in models if m["kind"] == "ocr" and m["provider_name"] == "extractor"), None
    )
    if reader is None:
        skip(
            "reading pipelines",
            "no built-in extractor model is catalogued (start the redaction profile)",
        )
    else:
        text_pdf = pdf("Referto di prova")

        def read(name: str, document: str) -> tuple[int, Any]:
            return call(
                secret,
                "/v1/ocr",
                {
                    "model": reader["name"],
                    "document": {
                        "type": "document_url",
                        "document_url": document,
                        "document_name": name,
                    },
                },
            )

        status, body = read("referto-rossi.pdf", text_pdf)
        expect(
            "a file named referto* is refused",
            status == 403 and "Medical files stay" in json.dumps(body),
            f"HTTP {status}",
        )
        expect("without echoing its name", "referto-rossi" not in json.dumps(body))
        status, body = read("fattura.pdf", text_pdf)
        expect(
            "another file is read",
            status == 200 and "Referto di prova" in json.dumps(body),
            f"HTTP {status}: {str(body)[:160]}",
        )

        print()
        print("=== reading: a scan rule reads first, then refuses a scan and passes a text PDF ===")
        wipe(dave)
        pipeline(dave, "read", "Scans", [rule("No scans", [{"key": "scan"}])])
        status, body = read("a.pdf", pdf(None))
        expect(
            "a PDF with no text layer is refused by the rule",
            status == 403 and "No scans" in json.dumps(body),
            f"HTTP {status}: {str(body)[:200]}",
        )
        status, body = read("b.pdf", pdf("Testo normale"))
        expect("a PDF with text goes through", status == 200, f"HTTP {status}: {str(body)[:200]}")

        print()
        print("=== reading: a read-with rule's engine reaches the extractor, and it is logged ===")
        wipe(dave)
        pipeline(
            dave,
            "read",
            "PDFs with Docling",
            [
                rule(
                    "PDFs with Docling",
                    [{"key": "filetype", "values": ["pdf"]}],
                    level="read_with",
                    reader="builtin",
                    engine="docling",
                )
            ],
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
            "the rule and its pipeline are recorded",
            (handled.get("routing_rule") or {}).get("name") == "PDFs with Docling"
            and (handled.get("routing_rule") or {}).get("pipeline") == "PDFs with Docling",
            str(handled),
        )
        expect("with the engine it asked for", handled.get("engine") == "docling")
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
    wipe(dave)
    remaining = api(dave, "/api/admin/privacy/pipelines")[1]["total"]
    expect("no pipelines are left behind", remaining == 0, str(remaining))
    audit = api(dave, "/api/admin/audit?target_type=privacy_pipeline&limit=200")[1]
    expect("every change was audited", audit["total"] >= 6, str(audit["total"]))

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("all privacy pipeline checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
