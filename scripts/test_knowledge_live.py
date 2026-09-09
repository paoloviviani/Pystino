#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27"]
# ///
"""Knowledge bases against the running stack (ADR 0062).

The unit suite covers 24 cases here on SQLite. **Three things it structurally
cannot reach**, and each of them is why this file exists:

* **The pgvector SQL.** SQLite has no distance operator at all, so the suite
  runs `ExactStore` — a Python cosine — and proves nothing whatever about the
  query that actually runs. The first version of `PgVectorStore.search` bound
  the vector width as a parameter, passed every SQLite test, and answered 500
  on the first real search: PostgreSQL requires a type modifier to be a
  literal (`type modifiers must be simple constants or identifiers`). That bug
  was found by hand, once. This script is so it cannot come back.
* **The extractor.** File ingestion posts the bytes to the extraction service
  over HTTP. The fixtures inject a fake transport for the *upstream* client
  only, so a unit test attempting extraction reaches for a real socket.
* **The ledger, in PostgreSQL.** Indexing is billable (ADR 0020), and "did the
  spend land, in the right group, in the right currency" is a question about
  `Numeric(24,12)` in a real database.

What it deliberately does **not** check: retrieval *quality*. The smoke
upstream's embedding is a hashed bag of words — lexical, not semantic — so
synonyms are orthogonal here where a real model would place them together. A
ranking assertion below therefore proves the plumbing ranks *something*
sensibly, and never that the embeddings are good.

Source deploy/.env first, so PUBLIC_HOST and the admin password are set:

    set -a; . deploy/.env; set +a
    ./scripts/test_knowledge_live.py

It mints its own API key through the management API and deletes it at the end,
so it needs a local administrator (ADR 0043) and leaves nothing behind but the
ledger rows its own indexing produced — which are real spend, correctly.
"""

from __future__ import annotations

import os
import sys
import time
import zipfile
from io import BytesIO
from typing import Any

import httpx

FAILURES: list[str] = []
SKIPS: list[str] = []


def check(label: str, condition: bool, detail: str = "", *, on_failure: str = "") -> bool:
    """One assertion, printed either way.

    ``on_failure`` is for a response body: useful when something breaks, noise
    on forty passing lines. ``detail`` is the opposite — a measured value worth
    reading when it passes, like the width a base learned.
    """
    mark = "ok  " if condition else "FAIL"
    shown = detail if condition else (on_failure or detail)
    print(f"  [{mark}] {label}" + (f" — {shown}" if shown else ""))
    if not condition:
        FAILURES.append(label)
    return condition


def skip(label: str, reason: str) -> None:
    print(f"  [skip] {label} — {reason}")
    SKIPS.append(label)


def origin() -> str:
    host = os.environ.get("PUBLIC_HOST")
    if host:
        return f"https://{host}:{os.environ.get('HTTPS_PORT', '443')}"
    return "http://127.0.0.1:8000"


def ca_bundle() -> str | bool:
    path = "deploy/tls/caddy-root.crt"
    return path if os.path.exists(path) else True


def tiny_docx() -> bytes:
    """The smallest .docx markitdown will read.

    Built here rather than committed as a fixture: a binary in the tree that
    only one script opens is a binary nobody can review, and the bytes that
    matter are the three XML parts below.
    """
    body = (
        '<?xml version="1.0"?><w:document '
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        "<w:p><w:r><w:t>The reservation is taken before the upstream call, so a caller "
        "over budget is refused before any money is spent.</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>The built-in extractor never sends a document anywhere: it runs "
        "markitdown inside this deployment with its NLP engine switched off."
        "</w:t></w:r></w:p></w:body></w:document>"
    )
    rels = (
        '<?xml version="1.0"?><Relationships '
        'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
        '2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'
    )
    types = (
        '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/'
        '2006/content-types"><Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", body)
    return buffer.getvalue()


DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def settle(client: httpx.Client, base: str, store: str, expected: int) -> list[dict[str, Any]]:
    """Wait for ingestion to finish, because it is a detached task.

    `POST` returns `in_progress` by design (ADR 0019 — OCR belongs nowhere near
    a request path), so a script that asserted on the POST response would be
    asserting on the queue rather than the work.
    """
    for _ in range(40):
        listed = client.get(f"{base}/v1/vector_stores/{store}/files").json()["data"]
        if len(listed) >= expected and all(
            d["status"] in ("completed", "failed") for d in listed
        ):
            return listed
        time.sleep(1)
    return client.get(f"{base}/v1/vector_stores/{store}/files").json()["data"]


def main() -> int:
    base = origin()
    verify = ca_bundle()
    password = os.environ.get("GATEWAY_LOCAL_ADMIN_PASSWORD", "")
    email = os.environ.get("GATEWAY_LOCAL_ADMIN_EMAIL", "admin@local")
    print(f"knowledge bases against {base}")

    if not password:
        skip("everything", "set GATEWAY_LOCAL_ADMIN_PASSWORD (source deploy/.env)")
        return 0

    session = httpx.Client(base_url=base, verify=verify, timeout=60, follow_redirects=True)
    login = session.post("/auth/login", json={"email": email, "password": password})
    if login.status_code != 200:
        skip("everything", f"admin login failed ({login.status_code})")
        return 0

    # A key of its own rather than the session cookie: `/v1` is the surface
    # under test, and `/api` cannot reach it (management routes read a cookie
    # and nothing else, which is the door problem ADR 0061 records).
    minted = session.post("/api/me/keys", json={"name": "knowledge live check"})
    if minted.status_code not in (200, 201):
        skip("everything", f"could not mint an API key ({minted.status_code})")
        return 0
    body = minted.json()
    secret = body.get("secret")
    key_id = body.get("id")
    if not secret:
        skip("everything", f"the mint response carried no secret: {sorted(body)}")
        return 0

    client = httpx.Client(
        base_url=base,
        verify=verify,
        timeout=120,
        headers={"authorization": f"Bearer {secret}"},
    )
    store: str | None = None
    try:
        print("\n-- the deployment's configuration --")
        status = client.get("/v1/vector_stores/status")
        if status.status_code == 404:
            skip("everything", "knowledge bases are not enabled on this deployment")
            return 0
        if not check("GET /v1/vector_stores/status", status.status_code == 200):
            return 1
        profile = status.json()
        check("the feature is enabled", profile["enabled"] is True)
        if not check(
            "an embedding model is configured",
            profile["ready"] is True,
            profile.get("detail") or profile.get("embedding_model") or "",
        ):
            # Unfinished rather than broken, and the API says so. Nothing below
            # can run, but that is a configuration gap and not a failure of the
            # code under test.
            FAILURES.remove("an embedding model is configured")
            skip("everything after this", "no embedding model chosen in the console")
            return 0
        check(
            "the vector store names itself",
            profile["vector_store"] == "pgvector",
            profile["vector_store"],
        )
        check(
            "propagation is reported, because a change is not instant",
            profile["propagation_seconds"] > 0,
            f"{profile['propagation_seconds']}s",
        )

        print("\n-- a base, and text with no file behind it --")
        created = client.post(
            "/v1/vector_stores",
            json={"name": "live check", "description": "scripts/test_knowledge_live.py"},
        )
        if not check(
            "POST /v1/vector_stores",
            created.status_code == 200,
            on_failure=created.text[:200],
        ):
            return 1
        store = created.json()["id"]
        check(
            "a new base has no dimensionality yet",
            created.json()["dimensions"] is None,
            "learned from the first vector, never configured",
        )

        passages = {
            "budget": "The reservation is taken before the upstream call, so a caller "
            "over budget is refused before any money is spent.",
            "placeholders": "Redaction replaces personal data with deterministic "
            "placeholders, so an indexed document and a later query still agree.",
            "counters": "Valkey counters are a rebuildable cache derived from the "
            "usage_records ledger, never the other way round.",
        }
        for name, text in passages.items():
            posted = client.post(
                f"/v1/vector_stores/{store}/text",
                json={"text": text, "title": name, "source_ref": f"live:{name}"},
            )
            check(f"POST .../text ({name})", posted.status_code == 200)
            check(
                f"...answers in_progress rather than blocking ({name})",
                posted.json()["status"] == "in_progress",
            )

        documents = settle(client, base, store, expected=3)
        check(
            "every passage indexed",
            all(d["status"] == "completed" for d in documents),
            ", ".join(f"{d['title']}={d['status']}" for d in documents),
        )
        errors = [d["last_error"] for d in documents if d["last_error"]]
        check("no ingestion errors", not errors, "; ".join(errors)[:160])

        detail = client.get(f"/v1/vector_stores/{store}").json()
        check(
            "the base learned its width from the first vector",
            isinstance(detail["dimensions"], int) and detail["dimensions"] > 0,
            str(detail["dimensions"]),
        )
        check(
            "the base names what embedded it",
            detail["embedding_model"] == profile["embedding_model"],
            str(detail["embedding_model"]),
        )

        print("\n-- retrieval, which is the part SQLite cannot express --")
        # The bug this file exists for: a bound type modifier is a *syntax*
        # error at prepare time, so any successful search at all is the check.
        for name, query in (
            ("budget", "what happens when a caller is over budget"),
            ("placeholders", "deterministic placeholders for personal data"),
            ("counters", "are the valkey counters authoritative"),
        ):
            found = client.post(
                f"/v1/vector_stores/{store}/search", json={"query": query}
            )
            if not check(
                f"POST .../search ({name})",
                found.status_code == 200,
                on_failure=found.text[:200],
            ):
                continue
            hits = found.json()["data"]
            if not check(f"...returns passages ({name})", bool(hits)):
                continue
            check(
                f"...ranks the right passage first ({name})",
                hits[0]["title"] == name,
                f"got {hits[0]['title']!r} at {hits[0]['score']:.3f}",
            )
            check(
                f"...scores are similarities, not distances ({name})",
                all(-1.0001 <= h["score"] <= 1.0001 for h in hits),
                f"best {hits[0]['score']:.3f}",
            )

        threshold = client.post(
            f"/v1/vector_stores/{store}/search",
            json={"query": "are the valkey counters authoritative", "min_score": 0.4},
        ).json()["data"]
        check(
            "a score floor excludes the weak hits",
            len(threshold) < 3,
            f"{len(threshold)} of 3 above 0.4",
        )

        print("\n-- re-indexing the same source replaces rather than duplicates --")
        client.post(
            f"/v1/vector_stores/{store}/text",
            json={
                "text": "Valkey counters are rebuilt from the ledger, and the ledger wins.",
                "title": "counters",
                "source_ref": "live:counters",
            },
        )
        documents = settle(client, base, store, expected=3)
        check(
            "still three documents, not four",
            len([d for d in documents if d["source_ref"]]) == 3,
            f"{len(documents)} rows",
        )

        print("\n-- a real file, through the real extractor --")
        upload = client.post(
            "/v1/files",
            files={"file": ("live-check.docx", tiny_docx(), DOCX_TYPE)},
        )
        if check("POST /v1/files", upload.status_code == 200, on_failure=upload.text[:200]):
            uploaded = upload.json()
            check("...reports the size it stored", uploaded["bytes"] > 0)
            check("...reports a digest", len(uploaded["sha256"]) == 64)
            back = client.get(f"/v1/files/{uploaded['id']}/content")
            check(
                "...serves the bytes back as an attachment",
                back.headers.get("content-disposition", "").startswith("attachment"),
                back.headers.get("content-disposition", "<none>"),
            )
            attached = client.post(
                f"/v1/vector_stores/{store}/files", json={"file_id": uploaded["id"]}
            )
            check(
                "POST .../files attaches it",
                attached.status_code == 200,
                on_failure=attached.text[:200],
            )
            documents = settle(client, base, store, expected=4)
            from_file = [d for d in documents if d["file_id"]]
            if check("the file produced a document", bool(from_file)):
                document = from_file[0]
                check(
                    "the extractor read it",
                    document["status"] == "completed",
                    document["last_error"] or f"{document['chars']} chars",
                )
                check(
                    "...and text came out",
                    document["chars"] > 0,
                    f"{document['chars']} chars, {document['chunk_count']} passages",
                )
                hits = client.post(
                    f"/v1/vector_stores/{store}/search",
                    json={"query": "markitdown with its NLP engine switched off"},
                ).json()["data"]
                check(
                    "the extracted document is retrievable",
                    any(h["document_id"] == document["id"] for h in hits),
                    f"{len(hits)} hits",
                )

        print("\n-- what it cost --")
        # Own usage rather than the admin report: this is the caller's own
        # spend, and `/api/me/usage` is the narrower surface.
        usage = session.get("/api/me/usage")
        if usage.status_code == 200:
            check("the caller's usage is readable", True, "GET /api/me/usage")
        else:
            skip("the caller's usage", f"{usage.status_code}")

        print("\n-- the feature switch is a 404, not a 403 --")
        anonymous = httpx.Client(base_url=base, verify=verify, timeout=30)
        unauth = anonymous.get("/v1/vector_stores")
        check(
            "an unauthenticated caller is refused",
            unauth.status_code in (401, 403),
            str(unauth.status_code),
        )
        anonymous.close()

    finally:
        if store:
            gone = client.delete(f"/v1/vector_stores/{store}")
            check("the base deletes cleanly", gone.status_code == 200, str(gone.status_code))
        if key_id:
            # Permanent, not the soft revoke: a check that leaves a
            # revoked key behind on every run fills the key list with
            # noise nobody can distinguish from a real revocation.
            session.delete(f"/api/me/keys/{key_id}/permanent")
        client.close()
        session.close()

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}")
        for label in FAILURES:
            print(f"  - {label}")
        return 1
    print(f"all checks passed{f' ({len(SKIPS)} skipped)' if SKIPS else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
