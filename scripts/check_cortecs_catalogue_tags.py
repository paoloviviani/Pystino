#!/usr/bin/env python3
"""How does one enumerate the catalogue tags of Cortecs' ``/v1/models``?

The gateway hard-won knowledge that the endpoint **defaults to
``tag=Instruct``** — an unfiltered request is a filtered one, which is how
eleven embedding models and three OCR models sat unseen (AGENTS.md). The
discovery dialog asks the operator for a tag, and free text there is a
vocabulary the operator has to already know. A dropdown needs the *list* of
tags, and no documentation names one.

So it is a script rather than a guess. Read-only GETs only — it spends
nothing.

    export CORTECS_API_KEY=...
    uv run python scripts/check_cortecs_catalogue_tags.py

It answers, in order:

1. what an untagged request really returns (count, and the union of the
   entries' ``tags`` arrays — if Cortecs ships every model regardless and
   merely *sorts* by tag, the default response is itself the vocabulary);
2. whether an explicit ``tag`` value enumerates: ``All``, ``all``, ``*``, an
   empty ``tag=``, a repeated ``tag`` parameter;
3. whether the response carries a top-level tag field beside ``data``.

Read the conclusion it prints. Whatever mechanism works becomes the gateway's
tag-listing fetch; if none does, the fallback is a per-plugin known-tag list
and this script says so.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import httpx

BASE_URL = os.environ.get("CORTECS_BASE_URL", "https://api.cortecs.ai/v1")


def die(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(2)


def harvest_tags(payload: dict[str, Any]) -> list[str]:
    """The union of the entries' ``tags`` arrays, sorted, case-preserving."""
    tags: set[str] = set()
    for entry in payload.get("data") or []:
        if isinstance(entry, dict) and isinstance(entry.get("tags"), list):
            tags.update(str(tag).strip() for tag in entry["tags"] if str(tag).strip())
    return sorted(tags)


def describe(label: str, response: httpx.Response) -> None:
    print(f"  {label}")
    print(f"    http    : {response.status_code}")
    if response.status_code >= 400:
        print(f"    body    : {response.text[:300]}")
        return
    payload = response.json()
    entries = payload.get("data") or []
    top_level = sorted(k for k in payload if k != "data")
    print(f"    models  : {len(entries)}")
    print(f"    tags    : {harvest_tags(payload) or '(none carried)'}")
    print(f"    other   : {top_level or '(nothing beside data)'}")


def main() -> int:
    api_key = os.environ.get("CORTECS_API_KEY")
    if not api_key:
        die("set CORTECS_API_KEY. This check reads the real API (GETs only).")

    variants: list[tuple[str, str]] = [
        ("untagged (the gateway's default fetch)", f"{BASE_URL}/models"),
        ("tag=Instruct (the documented default)", f"{BASE_URL}/models?tag=Instruct"),
        ("tag=All", f"{BASE_URL}/models?tag=All"),
        ("tag=all", f"{BASE_URL}/models?tag=all"),
        ("tag=*", f"{BASE_URL}/models?tag=*"),
        ("tag= (empty)", f"{BASE_URL}/models?tag="),
        ("tag=Instruct&tag=OCR (repeated)", f"{BASE_URL}/models?tag=Instruct&tag=OCR"),
        ("tag=OCR (known slice)", f"{BASE_URL}/models?tag=OCR"),
    ]

    with httpx.Client(
        timeout=httpx.Timeout(connect=10, read=60, write=30, pool=10),
        headers={"authorization": f"Bearer {api_key}"},
    ) as client:
        for label, url in variants:
            try:
                response = client.get(url)
            except httpx.HTTPError as exc:
                print(f"  {label}\n    transport error: {exc}")
                continue
            describe(label, response)
            print()

    print("=== conclusion ===")
    print("  Compare the model counts. A variant matching or exceeding the")
    print("  untagged count *and* carrying more tags is the enumeration")
    print("  mechanism; if every variant matches its own slice, no enumeration")
    print("  exists and the fallback is a per-plugin known-tag list.")
    print(f"  (raw transcript above; {len(variants)} GETs issued)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
