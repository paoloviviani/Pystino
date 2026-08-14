"""Wire schemas.

Two different philosophies live here on purpose.

The ``/v1`` surface is a **proxy**, so its request model is permissive:
``extra="allow"`` keeps every parameter we do not care about (``temperature``,
``tools``, ``response_format``, whatever a provider added last week) and forwards
it untouched. A gateway that validated the full OpenAI schema would reject valid
requests every time a provider shipped a new field, and would need a release to
catch up. We validate only what we must act on.

The management surface is ours, so it is strict.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatCompletionRequest(BaseModel):
    """Only the fields the gateway itself reasons about; the rest passes through."""

    model_config = ConfigDict(extra="allow", protected_namespaces=())

    model: str
    messages: list[dict[str, Any]]
    stream: bool = False
    stream_options: dict[str, Any] | None = None
    # Both spellings are in the wild; the newer one wins where both appear.
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    n: int | None = None

    def client_wants_usage(self) -> bool:
        """Whether the *client* asked for a usage frame.

        We always ask the upstream for one, so this decides whether the frame is
        forwarded or stripped on the way back out.
        """
        options = self.stream_options or {}
        return bool(options.get("include_usage"))

    def requested_max_output_tokens(self, default: int) -> int:
        """Upper bound on completion length, for the pre-call reservation."""
        for value in (self.max_completion_tokens, self.max_tokens):
            if value is not None and value > 0:
                return value
        return default

    def choice_count(self) -> int:
        return self.n if self.n and self.n > 0 else 1


class ModelCard(BaseModel):
    """One entry of ``GET /v1/models``, in OpenAI's shape."""

    id: str
    object: Literal["model"] = "model"
    created: int
    owned_by: str = "gateway"
    # Non-standard but useful additions; OpenAI clients ignore unknown fields.
    context_window: int | None = None
    display_name: str | None = None


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelCard] = Field(default_factory=list)


# -- management API ---------------------------------------------------------


class GroupSummary(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None = None


class MeResponse(BaseModel):
    id: uuid.UUID
    email: str | None
    display_name: str | None
    is_admin: bool
    groups: list[GroupSummary]
    default_billing_group: GroupSummary | None


class SetDefaultBillingGroupRequest(BaseModel):
    group_id: uuid.UUID


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(default="", max_length=255)
    # None means "resolve against the user's default billing group at request
    # time", so the key follows the user's later changes.
    billing_group_id: uuid.UUID | None = None
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class ApiKeyResponse(BaseModel):
    id: uuid.UUID
    name: str
    prefix: str
    billing_group: GroupSummary | None
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None


class ApiKeyCreatedResponse(ApiKeyResponse):
    """Includes the secret. Returned exactly once, never retrievable again."""

    secret: str


class UsageSummaryResponse(BaseModel):
    window_seconds: int
    requests: int
    total_tokens: int
    cost: Decimal
    currency: str
    # Split so a reader can tell measured spend from inferred spend.
    estimated_requests: int = 0


class LimitRuleResponse(BaseModel):
    id: uuid.UUID
    name: str
    scope: str
    scope_id: uuid.UUID | None
    metric: str
    window_seconds: int
    limit_value: Decimal
    is_active: bool
    current_value: Decimal | None = None
