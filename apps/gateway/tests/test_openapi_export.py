"""The committed OpenAPI document is what the gateway produces now.

The console's TypeScript types are generated from `apps/console/openapi.json`
(`pnpm --filter @llmp/console api:generate`), so a request or response model
changed without regenerating leaves the console typed against a stale shape.
This is the Python half of that check; the console job checks the TypeScript
against the JSON.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "export_openapi.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("export_openapi", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_openapi_matches_the_app() -> None:
    export = _load()
    fresh = export.render(export.build_schema())
    committed = export.OUTPUT.read_text(encoding="utf-8")
    assert fresh == committed, (
        "apps/console/openapi.json is out of date with the gateway's models. "
        "Run `uv run python scripts/export_openapi.py` and then "
        "`pnpm --filter @llmp/console api:generate`, and commit both."
    )


def test_response_fields_with_defaults_are_required_in_the_schema() -> None:
    """A defaulted field of a returned model is always on the wire.

    Pydantic would list it as optional, and the console's generated types
    would then force `?? []` on a value that is never absent. `ResponseModel`
    is what prevents that; this fails if a returned model goes back to a plain
    `BaseModel`.
    """
    schema = _load().build_schema()["components"]["schemas"]
    # `aliases` and `search_backend` have defaults; the route returns them always.
    assert {"aliases", "search_backend"} <= set(schema["GroupAdminResponse"]["required"])
    # A request body with a default really is optional.
    assert "send_email" not in schema["UserCreateRequest"].get("required", [])
