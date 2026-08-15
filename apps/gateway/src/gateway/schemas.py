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

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


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
    # Exactly one of these is set: a rolling window in seconds, or a calendar
    # period that resets on its own boundary (ADR 0025).
    window_seconds: int | None = None
    period: str | None = None
    # "3600s" or "month" — one string a UI can print without branching.
    window_label: str = ""
    limit_value: Decimal
    is_active: bool
    current_value: Decimal | None = None
    # Null unless the rule has ever been reset. Consumption before this instant
    # does not count towards the limit; it is still in the billing report.
    last_reset_at: datetime | None = None


# -- administration ---------------------------------------------------------
#
# Deliberately explicit rather than reusing the ORM: an admin API that accepts
# whatever fields happen to exist on a model is how someone eventually PATCHes a
# primary key.


class ProviderResponse(BaseModel):
    """A provider, minus its credential.

    The API key is never returned — only ``api_key_hint``, which is enough to
    tell two keys apart and useless to whoever reads it (ADR 0027).
    """

    id: uuid.UUID
    name: str
    description: str | None
    base_url: str
    api_key_hint: str
    has_api_key: bool
    extra_headers: dict[str, str]
    is_active: bool
    model_count: int
    created_at: datetime
    updated_at: datetime


class ProviderCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9._-]+$")
    description: str | None = None
    base_url: str = Field(min_length=1, max_length=500)
    # Write-only. Optional because a local vLLM or Ollama needs no credential.
    api_key: SecretStr | None = None
    extra_headers: dict[str, str] = Field(default_factory=dict)
    is_active: bool = True


class ProviderUpdateRequest(BaseModel):
    """Every field optional; only what is sent is changed.

    ``api_key`` follows a three-way convention that a plain optional string
    cannot express: omitted leaves the stored key alone, a value replaces it, and
    an empty string removes it. Without the distinction there is no way to clear
    a credential without deleting the provider.
    """

    description: str | None = None
    base_url: str | None = Field(default=None, min_length=1, max_length=500)
    api_key: SecretStr | None = None
    extra_headers: dict[str, str] | None = None
    is_active: bool | None = None


class ProviderTestResponse(BaseModel):
    """The result of calling a provider's ``/models``.

    Run against the row as stored, so it exercises the credential the gateway
    would actually send rather than one the operator retypes.
    """

    ok: bool
    status_code: int | None = None
    detail: str
    model_count: int | None = None
    # A handful of ids, as evidence the response was a real catalogue.
    sample: list[str] = Field(default_factory=list)
    latency_ms: int | None = None


class ModelCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    upstream_model: str = Field(min_length=1, max_length=255)
    # Which endpoint serves it. Required: a model with no provider cannot be
    # routed, and defaulting one would guess at spending money (ADR 0027).
    provider_id: uuid.UUID
    display_name: str | None = Field(default=None, max_length=255)
    description: str | None = None
    context_window: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    is_active: bool = True


class ModelUpdateRequest(BaseModel):
    """Every field optional; only what is sent is changed.

    ``name`` is absent on purpose. Clients send it as ``model`` and usage rows
    denormalise it, so renaming is a migration, not an edit.
    """

    upstream_model: str | None = Field(default=None, min_length=1, max_length=255)
    # Repointing a model at another provider is allowed: it is how you migrate
    # off an endpoint without changing what callers ask for.
    provider_id: uuid.UUID | None = None
    display_name: str | None = Field(default=None, max_length=255)
    description: str | None = None
    context_window: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    is_active: bool | None = None


class PriceResponse(BaseModel):
    id: uuid.UUID
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cache_read_per_mtok: Decimal | None
    cache_write_per_mtok: Decimal | None
    currency: str
    effective_from: datetime
    source: str


class PriceCreateRequest(BaseModel):
    """A new price. Prices are append-only, so this never edits an existing row."""

    input_per_mtok: Decimal = Field(ge=0)
    output_per_mtok: Decimal = Field(ge=0)
    cache_read_per_mtok: Decimal | None = Field(default=None, ge=0)
    cache_write_per_mtok: Decimal | None = Field(default=None, ge=0)
    # Defaults to the gateway's billing currency; anything else is refused.
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    # Future-dated prices are allowed: that is how a change is scheduled.
    effective_from: datetime | None = None


class ModelAdminResponse(BaseModel):
    id: uuid.UUID
    name: str
    upstream_model: str
    provider_id: uuid.UUID
    provider_name: str
    provider_is_active: bool
    display_name: str | None
    description: str | None
    is_active: bool
    context_window: int | None
    max_output_tokens: int | None
    created_at: datetime
    current_price: PriceResponse | None
    granted_to: list[str]
    # Users granted this model personally, over and above their groups.
    granted_to_users: list[str] = Field(default_factory=list)


class GroupAdminResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    source: str
    is_active: bool
    member_count: int
    models: list[str]


class UserAdminResponse(BaseModel):
    id: uuid.UUID
    email: str | None
    display_name: str | None
    issuer: str
    subject: str
    is_active: bool
    is_admin: bool
    groups: list[str]
    default_billing_group: str | None
    active_key_count: int
    last_login_at: datetime | None


class UserUpdateRequest(BaseModel):
    is_active: bool | None = None
    is_admin: bool | None = None


class LimitRuleCreateRequest(BaseModel):
    name: str = Field(default="", max_length=255)
    scope: Literal["global", "group", "user", "api_key"]
    scope_id: uuid.UUID | None = None
    metric: Literal["requests", "tokens", "cost"]
    # A rolling window ("EUR 1 every 6 hours") or a calendar period ("EUR 5 per
    # month", resetting at local midnight on the 1st). Exactly one, enforced below
    # as well as by ck_limit_rules_one_window_kind — a database error is correct
    # but not a good explanation.
    window_seconds: int | None = Field(default=None, ge=1, le=366 * 86_400)
    period: Literal["day", "week", "month", "quarter", "year"] | None = None
    limit_value: Decimal = Field(ge=0)
    is_active: bool = True

    @model_validator(mode="after")
    def _exactly_one_window(self) -> LimitRuleCreateRequest:
        if (self.window_seconds is None) == (self.period is None):
            raise ValueError(
                "give either window_seconds (a rolling window) or period (a calendar "
                "budget), not both and not neither"
            )
        return self


class LimitRuleUpdateRequest(BaseModel):
    """No window changes.

    Editing a rule's window or scope would move its counter key, silently
    abandoning the consumption recorded so far — which reads to an operator as a
    quota that reset itself. Delete the rule and add the one you want, or reset it
    deliberately.
    """

    name: str | None = Field(default=None, max_length=255)
    limit_value: Decimal | None = Field(default=None, ge=0)
    is_active: bool | None = None


class LimitRuleResetRequest(BaseModel):
    """Set a rule's consumption back to zero, from now.

    The reason is mandatory and stored: zeroing a spending cap is a financially
    meaningful act, and "who raised whose budget, and why" is the first question
    asked afterwards.
    """

    reason: str = Field(min_length=3, max_length=500)


class QuotaResetResponse(BaseModel):
    id: uuid.UUID
    rule_id: uuid.UUID
    effective_at: datetime
    reason: str
    created_by: uuid.UUID | None
    # Null when the account has since been erased; the reset itself stays.
    created_by_email: str | None = None


class GroupUsageRow(BaseModel):
    group_id: uuid.UUID | None
    group_name: str
    requests: int
    total_tokens: int
    cost: Decimal
    estimated_requests: int


# -- reporting ---------------------------------------------------------------
#
# Chargeback reporting: what a group spent in a calendar period, in a shape an
# administrator can hand to finance. Boundaries come from gateway.periods, the
# same function calendar quotas use (ADR 0024).


class PeriodResponse(BaseModel):
    """The exact window a report covers.

    Echoed back in full because "August" is ambiguous until you say in which
    timezone, and a report whose boundaries are invisible cannot be reconciled
    against anything.
    """

    label: str
    kind: str | None
    start: datetime
    end: datetime
    timezone: str


class UsageReportRow(BaseModel):
    key: str | None
    label: str
    requests: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost: Decimal
    # Requests whose token counts the provider did not return and we inferred, and
    # requests where usage could not be determined at all. Kept separate from the
    # totals so a reader can see how much of the figure is measured.
    estimated_requests: int = 0
    unavailable_requests: int = 0


class UsageReport(BaseModel):
    period: PeriodResponse
    group_by: str
    currency: str
    rows: list[UsageReportRow]
    totals: UsageReportRow
    # Plain-language caveats about this particular report: estimated usage,
    # in-flight requests excluded, deleted subjects. Rendered verbatim by the UI so
    # a caveat is never lost in a redesign.
    disclosures: list[str] = Field(default_factory=list)


class DiscoveredModel(BaseModel):
    """An upstream model the provider offers that we have not catalogued."""

    upstream_model: str
    suggested_name: str
    input_per_mtok: Decimal | None
    output_per_mtok: Decimal | None
    currency: str | None
    context_window: int | None
    # Set when the model cannot be imported as-is, with the reason. The commonest
    # is a price quoted in a currency this gateway does not bill in.
    blocked_reason: str | None = None


class CatalogueDriftRow(BaseModel):
    name: str
    upstream_model: str
    is_active: bool


class CatalogueDiscoveryResponse(BaseModel):
    """What the provider offers, against what we serve."""

    provider_url: str
    provider_model_count: int
    # Offered upstream, absent from our catalogue: candidates to adopt.
    available: list[DiscoveredModel]
    # Ours, and still offered upstream.
    catalogued: list[CatalogueDriftRow]
    # Ours, and NO LONGER offered upstream. Drift in the direction that breaks at
    # 3am, so it is reported first-class rather than left to be noticed.
    missing_upstream: list[CatalogueDriftRow]
    unparsable: list[str]


class ModelImportItem(BaseModel):
    upstream_model: str
    # Defaults to the upstream id with any provider prefix stripped.
    name: str | None = Field(default=None, max_length=255)


class ModelImportRequest(BaseModel):
    models: list[ModelImportItem] = Field(min_length=1, max_length=200)


class ModelImportResult(BaseModel):
    upstream_model: str
    name: str | None
    imported: bool
    priced: bool
    reason: str | None = None


class ModelImportResponse(BaseModel):
    results: list[ModelImportResult]

    @property
    def imported_count(self) -> int:
        return sum(1 for r in self.results if r.imported)
