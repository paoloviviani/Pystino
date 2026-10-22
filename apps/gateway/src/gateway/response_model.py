"""Base class for the models the management and /v1 routes return.

Pydantic's OpenAPI schema for a *response* marks a field that has a default
(`x: list[str] = []`, `y: str | None = None`) as not required, because the
default is what validation fills in. But the route serialises the whole model,
so the client always receives the field. The console's TypeScript types are
generated from the schema, and without this every defaulted response field
came out as `x?: ...`, forcing a `?? []` at each call site for a value that is
never absent. `json_schema_serialization_defaults_required` makes the schema
say what the wire does.

Only for models that are returned, never ones a client also sends: a request
body with a default really is optional.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ResponseModel(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
