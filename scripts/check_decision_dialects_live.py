#!/usr/bin/env python3
"""The decision dialect translation against both real vendors' APIs.

Direct to the vendors, no stack needed. For each vendor, one predicate is sent
in the *other* vendor's dialect: the request goes through
``gateway.translate.decisions.translate_request`` to the vendor's native dialect
and path (``DecisionSupport``), and the vendor's answer is translated back.
That is the gateway's translated path (``/v1/systemone`` to Cortecs,
``/v1/decisions`` to Melious) without the gateway's own accounting around it.
Spends well under EUR 0.0001. Keys are read from ``MELIOUS_API_KEY_FILE`` and
``CORTECS_API_KEY_FILE`` (paths), sent only to their vendor, never printed.

    MELIOUS_API_KEY_FILE=... CORTECS_API_KEY_FILE=... \
      uv run python scripts/check_decision_dialects_live.py

Never run by CI: it needs real keys. Exit status 1 when a check fails.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps" / "gateway" / "src"))

from gateway.plugins.registry import resolve
from gateway.translate.decisions import translate_request, translate_response

VENDORS = {
    "cortecs": ("https://api.cortecs.ai/v1", "CORTECS_API_KEY_FILE", "systemone"),
    "melious": ("https://api.melious.ai/v1", "MELIOUS_API_KEY_FILE", "decisions"),
}
BODIES: dict[str, dict[str, Any]] = {
    "systemone": {
        "model": "clef-flash",
        "state": "The agent runs `npm test` in the project directory.",
        "questions": {"safe": {"type": "noul", "instructions": "Is the action routine and safe?"}},
    },
    "decisions": {
        "model": "clef-flash",
        "input": "The agent runs `npm test` in the project directory.",
        "questions": [
            {"type": "predicate", "name": "safe", "instructions": "Is the action routine and safe?"}
        ],
    },
}
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILURES.append(label)


async def main() -> None:
    async with httpx.AsyncClient(timeout=120) as http:
        for vendor, (base, key_env, caller) in VENDORS.items():
            support = resolve(vendor).decisions  # type: ignore[attr-defined]
            print(f"{vendor}: speaks {support.dialect}; caller sends {caller} (translated)")
            wire, plan = translate_request(BODIES[caller], source=caller, target=support.dialect)
            secret = Path(os.environ[key_env]).read_text().strip()
            r = await http.post(
                f"{base}{support.path}", json=wire, headers={"authorization": f"Bearer {secret}"}
            )
            check("vendor accepts the translated body", r.status_code == 200, f"{r.status_code}")
            if r.status_code != 200:
                continue
            out = translate_response(r.json(), plan)
            answers = out["answers"]
            if caller == "systemone":
                probability = answers["safe"]["noul"]
            else:
                probability = answers[0]["probability"]
            check("answer is in the caller's form", isinstance(probability, float))
            check("probability in [0, 1]", 0 <= probability <= 1, str(probability))
            print(f"       p(safe) = {probability}")


asyncio.run(main())
print("\nFAILED: " + ", ".join(FAILURES) if FAILURES else "\nall checks passed")
sys.exit(1 if FAILURES else 0)
