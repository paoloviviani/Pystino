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
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    SecretStr,
    model_validator,
)

from gateway.config import RedactionPolicy
from gateway.plugins import registry as plugin_registry

if TYPE_CHECKING:
    from gateway.redaction import RedactionOutcome as RedactionOutcomeLike
else:  # pragma: no cover - runtime only needs the name to exist
    RedactionOutcomeLike = Any


def _plain_decimal(value: Decimal | None) -> str | None:
    """A money amount as digits, never in scientific notation.

    `Numeric(24,12)` round-trips a zero as `Decimal("0E-12")`, and both of the
    obvious renderings of that are wrong where it lands: a spreadsheet cell
    reading `0E-12`, and a UI that strips the sign and shows `0E12.00`. Neither
    is a rounding error anybody notices in review — the number is *zero* — but
    the same path carries every other price, so it is fixed at the boundary
    rather than in each consumer.

    `format(value, "f")` is exact and never returns an exponent.
    """
    return None if value is None else format(value, "f")


#: A `Decimal` that serialises to a plain decimal string. Money stays a string
#: end to end — parsing it into a float anywhere would reintroduce binary
#: floating point at the last moment.
Money = Annotated[Decimal, PlainSerializer(_plain_decimal, return_type=str, when_used="json")]


#: Lower-cased, deduplicated and sorted, so that re-importing a model is
#: visibly a no-op rather than a change nobody made. Validated as *shape*, not
#: as vocabulary: the reference provider documents `supported_features` as an
#: open set, and rejecting a value it added last week would make discovery fail
#: on exactly the models an operator most wants to hear about (ADR 0031).
Capabilities = Annotated[
    list[str],
    AfterValidator(
        lambda items: sorted({str(item).strip().lower() for item in items if str(item).strip()})
    ),
]


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


class EmbeddingRequest(BaseModel):
    """Only the fields the gateway acts on; the rest passes through.

    Same philosophy as ChatCompletionRequest: a gateway that validated the full
    schema would reject valid requests every time a provider shipped a field.
    ``encoding_format``, ``dimensions`` and any routing parameters are forwarded
    untouched for the provider to interpret.
    """

    model_config = ConfigDict(extra="allow", protected_namespaces=())

    model: str
    # A single string or a batch. Token arrays (OpenAI allows list[int]) are not
    # accepted: they cannot be redacted, and forwarding pre-tokenised text would
    # be a hole in the redaction layer (ADR 0028).
    input: str | list[str]

    def texts(self) -> list[str]:
        return [self.input] if isinstance(self.input, str) else list(self.input)


class ResponsesRequest(BaseModel):
    """``POST /v1/responses``.

    Same passthrough philosophy as the chat request: only the fields the
    gateway acts on are declared, and ``extra="allow"`` forwards the rest —
    ``reasoning``, ``text``, ``tools`` and whatever a provider added last week.
    """

    model_config = ConfigDict(extra="allow", protected_namespaces=())

    model: str
    #: A bare string, or a list of items. The items are usually chat-shaped
    #: messages but may be tool outputs or content-part objects.
    input: str | list[dict[str, Any]]
    instructions: str | None = None
    stream: bool = False
    max_output_tokens: int | None = None
    # Declared so the route can refuse them; see the module docstring for why.
    previous_response_id: str | None = None
    store: bool = False

    def as_messages(self) -> list[dict[str, Any]]:
        """The input as chat-shaped messages, for redaction and counting.

        Items that are not messages — tool outputs, function-call results — are
        passed through as-is so redaction can still walk their text, and are
        put back in place afterwards. Dropping them would under-count the
        prompt and, worse, leave un-redacted text on the wire.
        """
        if isinstance(self.input, str):
            return [{"role": "user", "content": self.input}]
        return [dict(item) for item in self.input if isinstance(item, dict)]

    def upstream_payload(
        self, outcome: RedactionOutcomeLike, *, upstream_model: str
    ) -> dict[str, Any]:
        payload = self.model_dump(exclude_unset=True)
        payload["model"] = upstream_model
        # Returned in the shape it arrived in: a caller who sent a string gets
        # a string. Wrapping it in a list would change what the provider sees
        # and, for some, how it is templated.
        if isinstance(self.input, str):
            first = outcome.messages[0] if outcome.messages else {}
            payload["input"] = str(first.get("content") or "")
        else:
            payload["input"] = outcome.messages
        # Never forwarded: both are refused by the route, and leaving a default
        # in the payload would send `store: false` to a provider that has no
        # such field.
        payload.pop("previous_response_id", None)
        payload.pop("store", None)
        return payload


class MessagesRequest(BaseModel):
    """``POST /v1/messages`` — Anthropic's shape.

    ``max_tokens`` is required by that API and is not given a default here: a
    gateway that invented one would silently truncate answers, and the error
    from omitting it is clear.
    """

    model_config = ConfigDict(extra="allow", protected_namespaces=())

    model: str
    messages: list[dict[str, Any]]
    max_tokens: int
    system: str | list[dict[str, Any]] | None = None
    stream: bool = False

    def as_messages(self) -> list[dict[str, Any]]:
        """Messages plus the system prompt, so redaction sees all the text.

        The system prompt is prepended as a message rather than redacted
        separately because it is prompt text like any other — a name in a
        system prompt is exactly as sensitive as one in a user turn.
        """
        prefix: list[dict[str, Any]] = []
        if isinstance(self.system, str) and self.system:
            prefix.append({"role": "system", "content": self.system})
        elif isinstance(self.system, list):
            for part in self.system:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    prefix.append({"role": "system", "content": part["text"]})
        return prefix + [dict(message) for message in self.messages]

    def system_message_count(self) -> int:
        if isinstance(self.system, str) and self.system:
            return 1
        if isinstance(self.system, list):
            return sum(
                1
                for part in self.system
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
        return 0

    def upstream_payload(
        self, outcome: RedactionOutcomeLike, *, upstream_model: str
    ) -> dict[str, Any]:
        payload = self.model_dump(exclude_unset=True)
        payload["model"] = upstream_model

        split = self.system_message_count()
        system_parts = [str(m.get("content") or "") for m in outcome.messages[:split]]
        payload["messages"] = outcome.messages[split:]

        # Put the system prompt back in the shape it arrived in.
        if isinstance(self.system, str):
            payload["system"] = system_parts[0] if system_parts else ""
        elif isinstance(self.system, list):
            rebuilt: list[dict[str, Any]] = []
            index = 0
            for part in self.system:
                copied = dict(part) if isinstance(part, dict) else part
                if isinstance(copied, dict) and isinstance(copied.get("text"), str):
                    copied["text"] = system_parts[index] if index < len(system_parts) else ""
                    index += 1
                rebuilt.append(copied)
            payload["system"] = rebuilt
        return payload


class ImageGenerationRequest(BaseModel):
    """``POST /v1/images/generations``.

    ``n`` is validated here rather than left to the provider because it
    multiplies the bill: a per-image-priced model charges ``n`` times, and the
    reservation has to know the number before the request is made.
    """

    model_config = ConfigDict(extra="allow", protected_namespaces=())

    model: str
    prompt: str
    n: int = Field(default=1, ge=1, le=10)
    size: str | None = None
    response_format: str | None = None

    def upstream_payload(
        self, outcome: RedactionOutcomeLike, *, upstream_model: str
    ) -> dict[str, Any]:
        payload = self.model_dump(exclude_unset=True)
        payload["model"] = upstream_model
        first = outcome.messages[0] if outcome.messages else {}
        payload["prompt"] = str(first.get("content") or "")
        return payload


class ModelCard(BaseModel):
    """One entry of ``GET /v1/models``, in OpenAI's shape."""

    id: str
    object: Literal["model"] = "model"
    created: int
    owned_by: str = "gateway"
    # Non-standard but useful additions; OpenAI clients ignore unknown fields.
    context_window: int | None = None
    max_output_tokens: int | None = None
    display_name: str | None = None
    # What the model accepts, produces and can do. Not in OpenAI's schema
    # either, and the reason a client can pick a model that does tool calling
    # without a lookup table of its own (ADR 0031).
    input_modalities: list[str] = Field(default_factory=list)
    output_modalities: list[str] = Field(default_factory=list)
    supported_features: list[str] = Field(default_factory=list)
    # Which of this gateway's routes will serve it. Not in OpenAI's schema,
    # where the listing is per-endpoint and the question does not arise — here
    # one listing covers chat, embedding and image models, and without this a
    # client has to guess and get a 400 to find out (ADR 0030).
    kind: str = "chat"


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
    cost: Money
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
    limit_value: Money
    is_active: bool
    current_value: Money | None = None
    # Null unless the rule has ever been reset. Consumption before this instant
    # does not count towards the limit; it is still in the billing report.
    last_reset_at: datetime | None = None


class MyLimitResponse(BaseModel):
    """One quota rule that constrains the caller, and how much of it is spent.

    Deliberately narrower than :class:`LimitRuleResponse`. It carries no
    ``scope_id`` — the only scopes here are global, this user, or a group they
    are in, so an id would name either themselves or something they already know
    — and no ``is_active``, because an inactive rule is not returned at all.

    ``current_value`` is absent, not zero, when the counter store cannot be
    reached. A budget shown as untouched because Valkey is down is worse than one
    shown as unknown.
    """

    id: uuid.UUID
    name: str
    #: "global", "group" or "user" — enough to say whose ceiling this is.
    scope: str
    metric: str
    window_label: str = ""
    limit_value: Money
    current_value: Money | None = None


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
    plugin: str | None = None
    kind: str = "provider"
    billing_mode: str = "own_prices"
    #: Active models behind this provider with no price row. Reported because a
    #: provider in `provider_reported` billing mode still needs prices: admission
    #: happens *before* the request and the counterparty's figure only exists
    #: after, so an unpriced model reserves nothing and has no cost ceiling at
    #: all. Worth a warning on the console rather than an overspend.
    unpriced_model_count: int = 0
    #: What the named plugin actually is, so the console can show that a router
    #: is configured as one rather than only that somebody typed the word.
    plugin_kind: str | None = None
    model_count: int
    created_at: datetime
    updated_at: datetime


def _known_plugin(value: str | None) -> str | None:
    """Refuse a plugin name the registry does not know.

    At the boundary, because the alternative is discovering it at request time —
    and the recorder's fallback there is the generic reader, which reports no
    cost and no serving endpoint. A router accepted by name and silently read as
    a plain provider looks like a working deployment that has stopped recording
    where its money went.
    """
    if value is None or value == "":
        return None
    normalised = value.strip().lower()
    if normalised not in plugin_registry.available():
        raise ValueError(
            f"unknown provider plugin {value!r}; installed: "
            f"{', '.join(plugin_registry.available())}"
        )
    return normalised


#: Empty string clears it, back to the generic OpenAI-compatible behaviour.
PluginName = Annotated[str | None, AfterValidator(_known_plugin)]


class ProviderCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9._-]+$")
    description: str | None = None
    # Optional because a plugin may know it: choosing the Cortecs type is the
    # act of choosing its endpoint, and an operator re-typing a URL the plugin
    # already knows is a typo with extra steps. Refused at the route when the
    # named plugin has no default to fall back to.
    base_url: str | None = Field(default=None, min_length=1, max_length=500)
    # Write-only. Optional because a local vLLM or Ollama needs no credential.
    api_key: SecretStr | None = None
    extra_headers: dict[str, str] = Field(default_factory=dict)
    is_active: bool = True
    # Which plugin carries this counterparty's quirks, and what kind it is
    # (ADR 0032). Null plugin is the generic OpenAI-compatible behaviour. It
    # replaced `auth_scheme`, `forward_stream_options` and `upstream_cost_unit`,
    # each of which was a column added for one counterparty's habit.
    plugin: PluginName = None
    kind: Literal["provider", "router"] = "provider"
    # Whose figure is the charge (ADR 0032 decision 6). Validated against the
    # named plugin below: pass-through needs a plugin that asserts its figure is
    # the counterparty's actual charge.
    billing_mode: Literal["own_prices", "provider_reported"] = "own_prices"

    @model_validator(mode="after")
    def _billing_mode_needs_a_plugin_that_can_claim_it(self) -> ProviderCreateRequest:
        _check_billing_mode(self.plugin, self.billing_mode)
        return self


class ProviderUpdateRequest(BaseModel):
    """Every field optional; only what is sent is changed.

    ``api_key`` follows a three-way convention that a plain optional string
    cannot express: omitted leaves the stored key alone, a value replaces it, and
    an empty string removes it. Without the distinction there is no way to clear
    a credential without deleting the provider.
    """

    # Same rule as on create, so a rename cannot produce a name that could not
    # have been created.
    name: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9._-]+$"
    )
    description: str | None = None
    base_url: str | None = Field(default=None, min_length=1, max_length=500)
    api_key: SecretStr | None = None
    extra_headers: dict[str, str] | None = None
    is_active: bool | None = None
    plugin: PluginName = None
    kind: Literal["provider", "router"] | None = None
    billing_mode: Literal["own_prices", "provider_reported"] | None = None

    @model_validator(mode="after")
    def _billing_mode_needs_a_plugin_that_can_claim_it(self) -> ProviderUpdateRequest:
        # `plugin` may be unset on a partial update, in which case this validates
        # against the generic default and refuses — which is the safe answer: a
        # mode change and the plugin that justifies it belong in one request.
        _check_billing_mode(self.plugin, self.billing_mode)
        return self


def _check_billing_mode(plugin: str | None, mode: str | None) -> None:
    """Pass-through billing needs a plugin that asserts an authoritative figure.

    Refused here rather than discovered later: with no such assertion every
    request would take the fallback path and bill from our own prices anyway, so
    the configuration would claim one thing and do another.
    """
    if mode != "provider_reported":
        return
    try:
        resolved = plugin_registry.resolve(plugin)
    except plugin_registry.UnknownPluginError as exc:  # pragma: no cover - caught earlier
        raise ValueError(str(exc)) from exc
    if not getattr(resolved, "reports_authoritative_cost", False):
        raise ValueError(
            f"the {resolved.name!r} plugin does not assert that the cost it reads is the "
            "counterparty's actual charge. Name a plugin that does, or leave "
            "billing_mode as 'own_prices'."
        )


class ProviderPluginResponse(BaseModel):
    """An installed provider type, for the console's selector.

    The plugin *is* the provider type as far as an operator is concerned: it
    decides how the counterparty is talked to and what can be believed about
    what it charged. So the console offers these rather than a free-text field.
    """

    name: str
    label: str
    description: str
    #: `provider` implies the serving endpoint; `router` chooses it per request.
    kind: str
    #: The billing modes this plugin can actually support. Pass-through appears
    #: only where the plugin asserts its reported figure is the real charge, so
    #: the UI cannot offer a configuration the API would refuse.
    billing_modes: list[str]
    #: The counterparty's public endpoint, when its plugin knows one — the
    #: console pre-fills it, and a create that omits the URL gets it here too.
    #: Null for a type whose endpoints vary.
    default_base_url: str | None
    #: What a provider row with no plugin named resolves to.
    is_default: bool


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
    kind: Literal["chat", "embedding", "image"] = "chat"
    display_name: str | None = Field(default=None, max_length=255)
    description: str | None = None
    context_window: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    input_modalities: Capabilities = Field(default_factory=list)
    output_modalities: Capabilities = Field(default_factory=list)
    supported_features: Capabilities = Field(default_factory=list)
    is_active: bool = True
    # Public access (ADR 0045): any authenticated caller, billed to their own
    # default billing group. Defaults closed — widening access is a decision,
    # not a default.
    is_public: bool = False


class ModelUpdateRequest(BaseModel):
    """Every field optional; only what is sent is changed.

    ``name`` is absent on purpose. Clients send it as ``model`` and usage rows
    denormalise it, so renaming is a migration, not an edit.
    """

    upstream_model: str | None = Field(default=None, min_length=1, max_length=255)
    # Repointing a model at another provider is allowed: it is how you migrate
    # off an endpoint without changing what callers ask for.
    provider_id: uuid.UUID | None = None
    # Editable because discovery infers it from the provider's modality tags,
    # and a mis-inferred kind takes a model off the only route that would serve
    # it. Historical usage rows record the surface they actually went through,
    # so correcting this does not make past spend unreadable (ADR 0030).
    kind: Literal["chat", "embedding", "image"] | None = None
    display_name: str | None = Field(default=None, max_length=255)
    description: str | None = None
    context_window: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    # Editable, because the catalogue is a claim rather than a contract: an
    # operator who has found out that a model does not really do tool calling
    # needs somewhere to record it that discovery will not immediately undo.
    input_modalities: Capabilities | None = None
    output_modalities: Capabilities | None = None
    supported_features: Capabilities | None = None
    is_active: bool | None = None
    is_public: bool | None = None


class PriceResponse(BaseModel):
    id: uuid.UUID
    input_per_mtok: Money
    output_per_mtok: Money
    cache_read_per_mtok: Money | None
    cache_write_per_mtok: Money | None
    per_image: Money | None
    currency: str
    effective_from: datetime
    source: str


class PriceCreateRequest(BaseModel):
    """A new price. Prices are append-only, so this never edits an existing row."""

    input_per_mtok: Money = Field(ge=0)
    output_per_mtok: Money = Field(ge=0)
    cache_read_per_mtok: Money | None = Field(default=None, ge=0)
    cache_write_per_mtok: Money | None = Field(default=None, ge=0)
    # Per generated image, for image models priced that way. Not per million of
    # anything, and set alongside the token rates rather than instead of them —
    # a model can be metered both ways (ADR 0030).
    per_image: Money | None = Field(default=None, ge=0)
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
    # "chat" or "embedding": which /v1 route may use it (ADR 0028).
    kind: str
    display_name: str | None
    description: str | None
    is_active: bool
    is_public: bool
    context_window: int | None
    max_output_tokens: int | None
    input_modalities: list[str]
    output_modalities: list[str]
    supported_features: list[str]
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
    # True when the account can sign in with a password (ADR 0043). The hash
    # itself never leaves the database; the fact of its existence is what the
    # console's user screen needs.
    has_password: bool = False
    groups: list[str]
    default_billing_group: str | None
    active_key_count: int
    last_login_at: datetime | None


class UserPasswordRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


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
    limit_value: Money = Field(ge=0)
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
    limit_value: Money | None = Field(default=None, ge=0)
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
    cost: Money
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
    # Pictures generated. Present because an image row can legitimately show a
    # real cost against zero tokens, which reads as a bug unless the report
    # says what was actually bought (ADR 0030).
    images: int = 0
    cost: Money
    # Requests whose token counts the provider did not return and we inferred, and
    # requests where usage could not be determined at all. Kept separate from the
    # totals so a reader can see how much of the figure is measured.
    estimated_requests: int = 0
    unavailable_requests: int = 0


class BillingDrift(BaseModel):
    """Our figure against the provider's, for one provider.

    **Per provider, and never summed across them.** A total mixes requests whose
    provider reports a cost with requests whose provider does not, so the two
    sides describe different sets of requests and the comparison is meaningless
    — it reads as a huge discrepancy when nothing is wrong. Currencies may differ
    between providers too, and this gateway does not convert.

    `requests` and `cost` cover **only** the requests that carried a provider
    figure, which is narrower than that provider's total spend: a request made
    before the provider named a plugin that reads its figure, or one the provider
    reported no cost for, is excluded from both sides rather than from one.
    """

    provider: str
    requests: int
    #: Ours, over the comparable requests only.
    cost: Money
    currency: str
    #: Theirs, in their unit's currency.
    upstream_cost: Money
    upstream_currency: str


class UsageReport(BaseModel):
    period: PeriodResponse
    group_by: str
    currency: str
    rows: list[UsageReportRow]
    totals: UsageReportRow
    #: One entry per provider that reported a cost. Empty when none did.
    reconciliation: list[BillingDrift] = Field(default_factory=list)
    # Plain-language caveats about this particular report: estimated usage,
    # in-flight requests excluded, deleted subjects. Rendered verbatim by the UI so
    # a caveat is never lost in a redesign.
    disclosures: list[str] = Field(default_factory=list)


class RedactionServiceHealth(BaseModel):
    """What the detection service says about itself, asked at read time.

    Asked rather than remembered: a service that was reachable at startup and is
    not reachable now is the failure that matters, and with ``fail_open`` false
    it means every request is currently being refused. That is not something to
    discover from a user's ticket.
    """

    reachable: bool
    detail: str = ""
    latency_ms: int | None = None
    engine: str | None = None
    engine_version: str | None = None
    #: Languages it can analyse, and the NER model backing each.
    languages: list[str] = Field(default_factory=list)
    models: dict[str, str] = Field(default_factory=dict)
    #: Served without an NER model, so fewer entities are found. Surfaced because
    #: "we detect less in Italian" is invisible otherwise.
    degraded_languages: list[str] = Field(default_factory=list)
    #: Every entity type it can detect, which is the superset any scope can ask
    #: for.
    entities: list[str] = Field(default_factory=list)


class RedactionActivity(BaseModel):
    """Evidence that it is working, not merely configured.

    The count of entities actually removed is the only figure that distinguishes
    a working redaction layer from one that is switched on and detecting nothing
    — a wrong language or too high a threshold both look like healthy silence.
    """

    window_seconds: int
    requests: int
    #: Requests that went through a redacting engine, whether or not it found
    #: anything.
    requests_redacted: int
    entities_redacted: int
    #: Engine names seen in the window. More than one means the configuration
    #: changed inside it.
    engines: list[str] = Field(default_factory=list)


class RedactionEngineOption(BaseModel):
    """One installed engine, as something an admin can choose.

    Built from the registry rather than written out in the console, so installing
    an engine through the ``llmp.redactors`` entry point makes it selectable
    without a console release (ADR 0026, ADR 0033).
    """

    name: str
    label: str
    description: str
    #: Whether it calls a detection service. Decides what must be validated
    #: before it can be enabled, and whether "unreachable" means anything for it.
    needs_endpoint: bool = False
    #: Whether it removes anything at all. ``noop`` is a real recorded engine
    #: rather than an absence, so the console cannot derive this from the name
    #: without hardcoding that name.
    redacts: bool = True
    #: The engine currently in force.
    is_active: bool = False
    #: Null when it can be enabled. Otherwise the reason it cannot, in words —
    #: a missing endpoint, an unset placeholder key, a service not answering.
    #: Computed here so the console offers no button that the PATCH would refuse.
    blocked_reason: str | None = None


class RedactionConfigChange(BaseModel):
    """Who last changed the engine, when, and why.

    Present only when the console set it. Rows are append-only, so this is the
    newest one; the trail behind it is not exposed yet because nothing asks for
    it, and the table keeps it either way.
    """

    engine: str
    reason: str
    changed_at: datetime
    #: Null once a user has been erased. The record of the change survives them,
    #: which is the point of the column being nullable rather than cascading.
    changed_by: str | None = None


class RedactionEngineRequest(BaseModel):
    """Enable one engine, or switch the layer off by naming ``noop``.

    One field plus a reason, deliberately. The endpoint, the placeholder key and
    the detection parameters stay in the environment: the key is a secret whose
    rotation re-labels every transcript it ever labelled, and an endpoint that
    can be typed here is an endpoint that can be pointed at a logger.
    """

    engine: str = Field(min_length=1, max_length=64)
    #: Required when the chosen engine redacts nothing, optional otherwise.
    #: Checked in the route rather than here, because the rule depends on the
    #: registry — which engines redact — and a schema that had to consult the
    #: registry to validate one field would be the wrong place for it.
    reason: str = Field(default="", max_length=500)


class RedactionStatusResponse(BaseModel):
    """The redaction layer as it is actually running.

    Everything except ``engine`` still comes from process configuration read at
    startup (ADR 0012). ``engine`` is now an admin decision that may override it
    (ADR 0033), which is why ``source`` exists: "the console says one thing and
    the environment says another" is otherwise invisible, and it is exactly the
    confusion a database override introduces.
    """

    #: The engine in force, taken from the constructed redactor rather than from
    #: the setting, so a mismatch cannot hide behind agreeing documentation.
    engine: str
    #: False when the engine redacts nothing: configured, and stripping nothing.
    enabled: bool
    endpoint: str | None = None
    #: Names the registry will accept. Kept alongside ``engines`` because it is
    #: the flat answer to "did my install register" and costs nothing.
    installed_engines: list[str] = Field(default_factory=list)
    #: Every installed engine as a choice, with what blocks each one.
    engines: list[RedactionEngineOption] = Field(default_factory=list)
    #: ``console`` when a stored decision is in force, ``environment`` otherwise.
    source: str = "environment"
    #: The stored decision, when there is one.
    configured: RedactionConfigChange | None = None
    #: How long another worker may still be running the previous engine after a
    #: change. Bounded by the resolver's poll interval and reported rather than
    #: implied, because a change that looks instant and is not is worse than one
    #: that says how long it takes.
    propagation_seconds: float = 0.0

    #: On failure: refuse the request, or forward it unredacted. Default false,
    #: and true deserves to be shown as a warning rather than a setting.
    fail_open: bool
    restore_in_response: bool
    language: str
    score_threshold: float
    #: Null means "everything the engine offers" rather than "none". Superseded
    #: by ``policy`` and still reported, because a deployment that sets it is
    #: entitled to see the value it set.
    entity_types: list[str] | None = None
    #: What applies to a request no rule is narrower about: the catch-all rule
    #: when one exists, the environment's policy otherwise (ADR 0039). Read-only
    #: here — it is edited as a rule, like every other scope.
    policy: RedactionPolicy
    #: ``rule`` when a catch-all rule decides it, ``environment`` when nobody
    #: has written one and the deployment's own setting stands.
    policy_source: str = "environment"
    timeout_seconds: float
    cache_size: int
    #: Whether the HMAC key placeholders derive from is set. Never the key.
    placeholder_key_set: bool

    service: RedactionServiceHealth | None = None
    activity: RedactionActivity
    #: Plain-language problems with this configuration, computed here rather than
    #: in the console so the wording lives with the rule and can be tested. Same
    #: convention as a report's disclosures: rendered verbatim.
    warnings: list[str] = Field(default_factory=list)


# -- scoped redaction rules --------------------------------------------------
#
# One policy document per subject (ADR 0038). The shape is deliberately the
# quota rules': an operator who has set a spending cap already knows where the
# scope, the subject and the active flag live, and a second idiom for the same
# idea is one more thing to learn for nothing.

#: The five subjects a rule can name. Not ``LimitScope``: quotas scope to who
#: pays, redaction to the endpoint text reaches and the person who wrote it.
RedactionScopeName = Literal["all", "provider", "model", "group", "user", "api_key"]


class RedactionRuleResponse(BaseModel):
    """One scoped rule, with enough about its subject to render a row.

    ``subject_label`` is resolved server-side because the console would otherwise
    have to hold five listings in memory to turn a ``scope_id`` into a word — and
    would get it wrong for a subject it has no permission to list.
    """

    id: uuid.UUID
    name: str
    scope: RedactionScopeName
    #: Null for the catch-all scope, whose subject is every request.
    scope_id: uuid.UUID | None
    #: The model name, provider name, group name, user email or key prefix.
    #: **Null means the subject no longer exists**, which is the one thing this
    #: field must be able to say: a rule pointing at a deleted group is inert and
    #: looks identical to a working one without it. ``scope_id`` is not a foreign
    #: key — it points at one of five tables — so nothing else catches that.
    subject_label: str | None = None
    policy: RedactionPolicy
    is_active: bool
    reason: str = ""
    created_by: uuid.UUID | None = None
    #: Null once the account is erased; the rule outlives its author.
    created_by_email: str | None = None
    created_at: datetime
    updated_at: datetime


class RedactionRuleCreateRequest(BaseModel):
    """Attach a policy to one subject.

    No reason is required, unlike the engine switch and unlike a deployment
    policy change. A scoped rule can only ever tighten — ``RedactionPolicy.combine``
    takes the strictest answer for every type — so there is no direction here
    that a later review would ask about, and demanding a sentence per rule is how
    a reason column fills up with "x" (ADR 0033's argument, applied one level
    down).
    """

    name: str = Field(default="", max_length=255)
    scope: RedactionScopeName
    #: Null for, and only for, the catch-all scope, whose subject is every
    #: request. Checked in the route rather than here: "which scopes need a
    #: subject" is the model's rule and the database's CHECK, and a third copy
    #: in a schema is a third thing to keep in step.
    scope_id: uuid.UUID | None = None
    policy: RedactionPolicy
    is_active: bool = True
    reason: str = Field(default="", max_length=500)


class RedactionRuleUpdateRequest(BaseModel):
    """No scope changes.

    A rule *is* a decision about one subject; re-pointing it at another is a
    different decision that happens to reuse a row, and it would silently make
    the two subjects' histories read as one. Delete it and write the one you
    want. (The same argument as ``LimitRuleUpdateRequest``, for a different
    reason: there is no counter here to abandon, only a record to confuse.)
    """

    name: str | None = Field(default=None, max_length=255)
    policy: RedactionPolicy | None = None
    is_active: bool | None = None
    reason: str | None = Field(default=None, max_length=500)


class RedactionPreviewRequest(BaseModel):
    """A sample to run the real redaction path over.

    ``scope``/``scope_id`` preview *as* a subject, so an admin can see what a
    group's rule actually does before saving it. Omitted, the deployment policy
    applies.

    **This body is never logged.** It will contain real personal data within a
    week of shipping — an admin pastes the prompt that came back wrong — and a
    tool for inspecting redaction that writes the unredacted sample to a log file
    has defeated itself.
    """

    text: str = Field(min_length=1, max_length=20_000)
    scope: RedactionScopeName | None = None
    scope_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _scope_and_subject_together(self) -> RedactionPreviewRequest:
        if (self.scope is None) != (self.scope_id is None):
            raise ValueError("give both scope and scope_id, or neither")
        return self


class RedactionPreviewSpan(BaseModel):
    """One thing the detector found, and what the policy says about it.

    ``mode`` and ``threshold`` are the policy's answers for this type, and
    ``allow_listed`` whether the matched text is exempt. They explain the
    rewrite; they do not decide it — ``entity_count`` on the response is what
    ``apply_spans`` actually replaced, and that is the authoritative number.
    """

    entity_type: str
    start: int
    end: int
    score: float
    mode: str
    threshold: float
    allow_listed: bool = False


class RedactionPreviewResponse(BaseModel):
    """What the model would receive, and why.

    Item 4 of ADR 0037's "not done": until this existed, nothing anywhere showed
    an operator *which* spans were replaced. The Italian bug — a verb read as a
    name, the news site the user asked for read as identity — was only findable
    by reading an upstream request body.
    """

    engine: str
    #: The narrowest rule that contributed, and its id. Null when only the
    #: deployment policy applied.
    scope: RedactionScopeName | None = None
    rule_id: uuid.UUID | None = None
    #: The policy that was actually run, folded. Returned because a preview whose
    #: result surprises an operator is exactly when they need to see the inputs.
    policy: RedactionPolicy
    spans: list[RedactionPreviewSpan] = Field(default_factory=list)
    #: What the provider would receive. **Null when blocked**: nothing is
    #: rewritten in that case, and echoing the sample back would read as "this is
    #: what would be sent".
    redacted_text: str | None = None
    entity_count: int = 0
    #: True when the policy would refuse the request. Reported as a field on a
    #: 200, not as a 403 — this is a preview, and a preview that fails is a tool
    #: that looks broken at the moment it is working.
    blocked: bool = False
    blocked_reason: str | None = None
    #: Set when the engine in force cannot detect anything (``noop``), so an
    #: empty result is not read as "nothing here to redact".
    note: str | None = None


class DiscoveredModel(BaseModel):
    """An upstream model the provider offers that we have not catalogued."""

    upstream_model: str
    suggested_name: str
    input_per_mtok: Money | None
    output_per_mtok: Money | None
    currency: str | None
    context_window: int | None
    # What the provider says it can do, shown before importing so the choice is
    # informed — "does this one do tool calling" is the commonest question at
    # exactly this moment (ADR 0031).
    kind: str = "chat"
    input_modalities: list[str] = Field(default_factory=list)
    output_modalities: list[str] = Field(default_factory=list)
    supported_features: list[str] = Field(default_factory=list)
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
