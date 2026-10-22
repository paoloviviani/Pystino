#!/usr/bin/env python3
"""Write the gateway's OpenAPI document, deterministically, without a server.

The console's TypeScript API types are generated from this file
(`apps/console/openapi.json`), and a gateway test fails when the committed copy
differs from what the app produces now. The schema depends only on the routes
and models, never on settings values, so the app is built over an in-memory
SQLite URL and nothing connects (the lifespan is not run).

    uv run python scripts/export_openapi.py            # rewrite the committed file
    uv run python scripts/export_openapi.py --stdout   # print instead
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

OUTPUT = Path(__file__).resolve().parent.parent / "apps" / "console" / "openapi.json"


def build_schema() -> dict[str, Any]:
    # create_app configures logging and the console mount logs a notice; keep
    # stdout to the schema, and put the logging switch back for a caller that
    # is a test run (a process-wide disable would silence every later test).
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        from gateway.config import Settings
        from gateway.main import create_app

        app = create_app(Settings(database_url="sqlite+aiosqlite:///:memory:", log_json=False))
        return app.openapi()
    finally:
        logging.disable(previous)


def render(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    text = render(build_schema())
    if "--stdout" in argv:
        sys.stdout.write(text)
    else:
        OUTPUT.write_text(text, encoding="utf-8")
        print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
