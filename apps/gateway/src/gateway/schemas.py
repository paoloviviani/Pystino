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
    field_validator,
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


class OcrDocument(BaseModel):
    """The document to read, in the shape Cortecs and Mistral both accept.

    Two forms, and the difference is not cosmetic — it decides whether this
    deployment ever sees the bytes:

    * ``document_url`` / ``image_url`` pointing at an address. The *provider*
      fetches it, so the document never passes through the gateway, and nothing
      here can inspect or redact it. That is a property of the request, not a
      gap to fix: we cannot read what we never receive.
    * a ``data:`` URI in either field, which is the document itself. That one we
      hold, and it is the form local extraction and inspection can act on.
    """

    model_config = ConfigDict(extra="allow")

    #: ``document_url`` or ``image_url``. Passed through as the provider's own
    #: discriminator rather than reinterpreted.
    type: str
    document_url: str | None = None
    image_url: str | None = None

    def location(self) -> str:
        """Wherever the document is, whichever field carries it."""
        return self.document_url or self.image_url or ""

    def inline_bytes(self) -> tuple[bytes, str] | None:
        """``(data, media type)`` when the document travelled with the request.

        ``None`` for an address, which is the case where the provider does the
        fetching and there is nothing local to read.
        """
        location = self.location()
        if not location.startswith("data:"):
            return None
        header, _, encoded = location.partition(",")
        if not encoded:
            return None
        media_type = header[5:].split(";")[0] or "application/octet-stream"
        if ";base64" not in header:
            # A plain data: URI is percent-encoded text, not a document anyone
            # sends an OCR model. Treated as absent rather than guessed at.
            return None
        import base64
        import binascii

        try:
            return base64.b64decode(encoded, validate=True), media_type
        except (binascii.Error, ValueError):
            return None


def _page_range_count(value: str) -> int:
    """The inclusive page count of a Mistral/Cortecs range string, ``"0-5"`` -> 6.

    Parsed here, at the boundary, rather than left for the reservation math to
    do its own thing with the raw string: ``len("0-5")`` is 3, and would
    silently reserve the wrong number of pages for every range whose printed
    length differs from its page count — worse the wider the range gets. A
    malformed value is refused with a plain message instead of reaching
    admission as a confusing arithmetic result.
    """
    parts = value.split("-")
    if len(parts) != 2 or not all(part.strip().isdigit() for part in parts):
        raise ValueError(
            f"'pages' range must look like '<start>-<end>' with non-negative "
            f"integers, got {value!r}"
        )
    start, end = (int(part.strip()) for part in parts)
    if end < start:
        raise ValueError(f"'pages' range end must not be before its start, got {value!r}")
    return end - start + 1


class OcrRequest(BaseModel):
    """``POST /v1/ocr`` — the Cortecs and Mistral shape.

    Same passthrough philosophy as every other ``/v1`` request model: the fields
    the gateway acts on are declared and the rest is forwarded, because the
    option set here is long and provider-specific (``table_format``,
    ``include_blocks``, ``confidence_scores_granularity``, the annotation
    formats, and the routing preferences ``eu_native`` and
    ``allow_zero_data_retention``). A gateway that validated all of it would
    reject valid requests every time the counterparty added a field.
    """

    model_config = ConfigDict(extra="allow", protected_namespaces=())

    model: str
    document: OcrDocument
    #: Which pages to read, when the caller wants a subset: a list of 0-based
    #: indices, or an inclusive range string ("0-5") in the same shape Mistral
    #: and Cortecs both document. Declared because it bounds the bill: the page
    #: count is otherwise unknown until the response.
    pages: list[int] | str | None = None
    #: Ask the provider to return page images alongside markdown. Declared
    #: rather than left to ``extra="allow"`` so it is a documented part of the
    #: contract instead of a stray that happens to be forwarded: the local
    #: extractor accepts it and always answers with no images, which is a
    #: known behaviour rather than a silently ignored request.
    include_image_base64: bool | None = None

    @field_validator("pages")
    @classmethod
    def _valid_pages(cls, value: list[int] | str | None) -> list[int] | str | None:
        if isinstance(value, str):
            _page_range_count(value)  # raises ValueError on a malformed range
        return value

    def page_count(self) -> int | None:
        """How many pages this selection reserves. ``None`` for "no selection".

        The reservation floor: what admission can prove before the call is the
        caller's own page selection, list or range, or nothing at all. A
        malformed range never reaches here — ``_valid_pages`` above refuses it
        at validation, before a request row is even opened.
        """
        if self.pages is None:
            return None
        if isinstance(self.pages, str):
            return _page_range_count(self.pages)
        return len(self.pages) or None

    def upstream_payload(self, *, upstream_model: str) -> dict[str, Any]:
        payload = self.model_dump(exclude_unset=True)
        payload["model"] = upstream_model
        return payload


class SearchRequest(BaseModel):
    """``POST /v1/search`` — one search through the caller's billing-group policy.

    **The one ``/v1`` request model that forbids unknown fields.** Every other
    one allows them, because every other one *forwards* the body it was given
    and a gateway that validated a counterparty's whole option set would refuse
    valid requests each time the counterparty added a field. Nothing is
    forwarded here: the body is rebuilt in the backend's own shape by its
    plugin, because Linkup wants ``q``/``depth`` and Exa wants
    ``query``/``numResults`` and no passthrough can be both. An unrecognised
    field would therefore be *dropped*, silently, and a caller who asked for a
    date range and did not get one deserves a refusal rather than results they
    will trust. (A 400, not FastAPI's 422: this app rewrites validation errors
    into the OpenAI error shape every ``/v1`` client parses.)

    There is no ``backend``, ``depth``, ``type`` or ``tier`` field, and that is
    the design. Which backend runs is the billing group's policy
    (``groups.search_model_id``) — one endpoint per group, resolved
    server-side, never a request parameter — and it always runs at the
    vendor's default depth: the ledger records no tier because none was asked
    for, and a ceiling counted in requests bounds volume, not spend. A caller
    that needs a dearer tier names the backend directly on the passthrough.

    This revisits ADR 0071's refusal to translate, narrowly and for a reason
    that did not exist then: a group policy is unenforceable while the vendor
    dialect lives in the caller. The translation surface is three fields in
    and three fields out, and anything fancier stays on the passthrough.
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    query: str = Field(min_length=1, max_length=2000)
    #: How many results to ask for. Ten is the most a rendered answer can use,
    #: and Exa's per-result charge above ten is invisible to this ledger — a
    #: bound here rather than none because the number multiplies a vendor's
    #: bill in a way no row here can see.
    max_results: int = Field(default=5, ge=1, le=10)


class SearchResultItem(BaseModel):
    """One result of ``POST /v1/search``, in the gateway's own shape.

    Title, URL and snippet are the intersection every backend can supply:
    Linkup answers ``name``/``url``/``content``, Exa answers
    ``title``/``url``/``summary``, Jina answers ``title``/``url``/``description``,
    DuckDuckGo answers ``result__a``/``result__snippet`` HTML.
    Anything richer — scores, dates, favicons — stays on the passthrough,
    where the vendor's own shape survives verbatim.
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    title: str
    url: str
    snippet: str = ""


class SearchResponse(BaseModel):
    """The unified answer: normalised results, and which backend ran.

    ``backend`` names the plugin that ran (``linkup``, ``exa``, ``jina``,
    ``duckduckgo``), not the model row: it is what the ledger's
    ``own_search_backend`` column carries, and a single search never mixes
    backends, so one name is the whole story.
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    results: list[SearchResultItem] = Field(default_factory=list)
    backend: str


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
    # The "in" half. Distinct from ``context_window`` because a provider can
    # cap prompt harder than the window total implies; null means unknown.
    max_input_tokens: int | None = None
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
    # Who owns this identity: "local" means a password here is the way in, and
    # the console can offer password management; anything else is the IdP's
    # (ADR 0049). `has_password` distinguishes a local account with a password
    # from one without — the set of states ADR 0043 made three.
    issuer: str
    has_password: bool = False


class CallerIdentity(BaseModel):
    """What ``GET /v1/me`` answers, deliberately beside :class:`MeResponse`.

    The two answer the same question through different doors, and keeping them
    in one file is what makes the differences visible instead of accidental.
    ``MeResponse`` is behind a session cookie and talks to the console, so it
    carries what a console screen needs — the issuer, whether a password
    exists, group ids for the pickers. This one is behind a ``/v1`` credential
    and talks to programs, so it carries names: a client that has only ever
    seen ``x-bill-to`` and ``/v1/billing/groups`` has never needed a group id.

    ``is_admin`` is the one field that is not the same fact in both. See
    ``routers/identity.py``: here it answers for the *credential*, which is why
    ``credential`` is beside it rather than left to be inferred.
    """

    object: str = "user"
    id: uuid.UUID
    email: str | None
    display_name: str | None
    is_admin: bool
    #: Which kind of credential authenticated this call, because ``is_admin``
    #: is false for every API key and a client must be able to tell that from
    #: "this person is not an administrator".
    credential: Literal["access_token", "api_key"]
    #: Effective membership names — what the person holds here, not what a
    #: token claims (ADR 0057).
    groups: list[str]
    default_billing_group: str | None
    #: What *this* caller's requests bill right now, which is not always the
    #: default: a key may pin a group, and a bearer request may name one.
    #: Null for a caller with no usable group at all (ADR 0093 to-do item 2)
    #: — this route authenticates but never requires one, unlike every
    #: metered ``/v1`` route, which still refuses that caller outright.
    billing_group: str | None
    #: A session the chat opened before this instant is no longer good: the
    #: account was disabled, merged into, or recovered (ADR 0093). Null when
    #: nothing has ever revoked this person's sessions.
    sessions_valid_after: datetime | None = None
    #: The newest time another account was merged into this one. A chat that
    #: has not folded its records since then asks `/v1/me/identities`.
    merged_at: datetime | None = None


class IdentityRef(BaseModel):
    """One ``(issuer, subject)`` that signs in as a gateway user (ADR 0093)."""

    issuer: str
    subject: str


class MeIdentities(BaseModel):
    """``GET /v1/me/identities``: everything that names this person here.

    The chat keys its own records on the gateway's ``id`` and uses the rest to
    adopt records it has not keyed yet (``identities``) and to fold records of
    accounts merged into this one (``merged_from``, chains resolved).
    """

    id: uuid.UUID
    identities: list[IdentityRef]
    merged_from: list[uuid.UUID]


class SessionAnnounce(MeIdentities):
    """``POST /v1/session/announce``: the chat's sign-in door (ADR 0093).

    Called by the chat's login callback with the fresh access token (``azp``
    must be the chat client). The gateway fetches userinfo itself and runs
    the same sign-in sequence as the console callback — links, bindings,
    admin rules, bootstrap — then answers who this is. A disabled account
    answers 403, never this body.
    """

    is_active: bool
    is_admin: bool
    sessions_valid_after: datetime | None = None
    merged_at: datetime | None = None


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
    #: The caller's own notification thresholds on this rule (ADR 0052).
    notification_thresholds: list[int] = []


class PystinoUsageResponse(BaseModel):
    """``GET /v1/pystino/usage`` (ADR 0074): a bearer caller's own quotas and spend.

    The gateway's own shape under a clearly-custom path, not a mimicry of
    OpenAI's usage endpoints — the ``/v1`` usage surface was deferred
    specifically because *that* shape does not fit five-scope, cost-and-token
    quotas, and this does not reopen that question. It exists because
    ``/api/me/limits``, ``/api/me/usage`` and ``/api/me/usage/groups`` are
    behind a session cookie: unreachable from a cross-origin client and from
    any bearer-only program. One document rather than three round trips, built
    from the exact same models those routes return — a client already parsing
    the console's numbers parses this without a second shape to learn.
    """

    limits: list[MyLimitResponse]
    usage: UsageSummaryResponse
    #: Per-group spend, keyed by group name — as `/api/me/usage/groups` keys it.
    groups: dict[str, UsageSummaryResponse]


class MyNotificationThresholdsRequest(BaseModel):
    """Replace the caller's thresholds for one rule. Percentages 1-100."""

    thresholds: list[int] = Field(default_factory=list)


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
    #: Prepended to every model name this provider contributes, so two vendors
    #: whose tiers share a word do not collide. Empty means none.
    prefix: str = ""
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
    kind: Literal["provider", "router", "search"] = "provider"
    # Prepended to every model name this provider contributes, so two vendors
    # whose tiers share a word ("deep", "fast") do not collide. Optional:
    # empty is the honest default, and a prefix is a judgement about a
    # catalogue the operator can already see.
    prefix: str = Field(default="", max_length=32, pattern=r"^[a-zA-Z0-9._-]*$")
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
    kind: Literal["provider", "router", "search"] | None = None
    billing_mode: Literal["own_prices", "provider_reported"] | None = None
    # Setting or changing this re-applies it to the models already here: a
    # prefix that only reached future imports would leave the catalogue
    # half-disambiguated. An empty string removes the prefix and un-names the
    # models that carry it.
    prefix: str | None = Field(default=None, max_length=32, pattern=r"^[a-zA-Z0-9._-]*$")

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


class SearchBackendDeleteResponse(BaseModel):
    """What deleting a search backend took down with it.

    A search backend and its tiers are one concept on the console's search
    screen (ADR 0071), so deleting the backend cascades instead of refusing —
    and the answer names what the cascade touched, the way a *refusal* names
    what blocked it elsewhere. The console repeats the cleared groups in its
    notice: a policy that silently vanished would otherwise be discovered the
    first time a group's unified search 404s, which is the wrong moment.
    """

    #: How many model rows (the anchor and its tiers) went with the backend.
    tiers_deleted: int
    #: Names of the groups whose unified-search policy pointed at one of the
    #: deleted tiers, and now point at nothing.
    cleared_groups: list[str]


class PluginBaseURLOption(BaseModel):
    """One endpoint a provider type documents: the URL and what it is.

    The label is the plugin's knowledge — which host is the EU-only one is a
    fact about the counterparty, not a sentence the console should have to
    hold — which is why the pair travels together instead of the console
    styling URLs it cannot vouch for.
    """

    url: str
    label: str


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
    #: The endpoints the plugin documents, when there is more than one, so the
    #: console offers them as a choice rather than free-text URL entry — for a
    #: decision with exactly two documented values, free text is a typo waiting
    #: to happen. Empty means the default above is the whole of the answer; the
    #: API itself keeps accepting any base URL, because a hand-set one is the
    #: operator's own (a private gateway, a proxy) and stays legitimate.
    base_url_options: list[PluginBaseURLOption] = []
    #: Whether creating a provider of this type needs a credential. False for
    #: the keyless search backend, so the console offers creation without one
    #: instead of demanding a dummy key. True for everything else, which is
    #: also what an installed third-party plugin reports by saying nothing.
    requires_api_key: bool = True
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
    kind: Literal["chat", "embedding", "image", "ocr", "search"] = "chat"
    display_name: str | None = Field(default=None, max_length=255)
    description: str | None = None
    context_window: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    # Set by hand: the upstream's own input cap, which no catalogue reports.
    max_input_tokens: int | None = Field(default=None, ge=1)
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
    kind: Literal["chat", "embedding", "image", "ocr", "search"] | None = None
    display_name: str | None = Field(default=None, max_length=255)
    description: str | None = None
    context_window: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    max_input_tokens: int | None = Field(default=None, ge=1)
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
    per_page: Money | None
    per_search: Money | None
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
    # Per page read, for OCR models. Same shape, same reason: an OCR
    # counterparty charges by the page, and a page is not a million of
    # anything.
    per_page: Money | None = Field(default=None, ge=0)
    # Per provider-side web search (ADR 0058). Per *one* search, though every
    # provider publishes it per thousand — $10 per 1,000 is 0.01 — because the
    # ledger multiplies by a count of searches, and a rate whose unit differs
    # from the count's is how a bill comes out a thousand times wrong.
    per_search: Money | None = Field(default=None, ge=0)
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
    # What kind of counterparty the provider names. Screens that list models
    # as things a caller could pick read this to leave infrastructure out
    # (`internal`, the extractor) the way they already leave search tiers out
    # by the model's own kind.
    provider_kind: str
    # "chat" or "embedding": which /v1 route may use it (ADR 0028).
    kind: str
    display_name: str | None
    description: str | None
    is_active: bool
    is_public: bool
    context_window: int | None
    max_output_tokens: int | None
    max_input_tokens: int | None
    input_modalities: list[str]
    output_modalities: list[str]
    supported_features: list[str]
    created_at: datetime
    current_price: PriceResponse | None
    granted_to: list[str]
    # Users granted this model personally, over and above their groups.
    granted_to_users: list[str] = Field(default_factory=list)


class IdentityProviderResponse(BaseModel):
    """A provider row. The secret is never returned — only whether one is set."""

    id: uuid.UUID
    name: str
    issuer: str
    client_id: str
    has_client_secret: bool = True
    scopes: list[str]
    groups_claim: str
    fetch_userinfo: bool
    group_mappings: list[OidcMappingRule]
    link_by_email: bool
    group_sync: Literal["every_login", "first_login", "never"]
    is_enabled: bool
    source: str
    internal_base_url: str = ""
    logout_url: str = ""
    #: What signing out will use when logout_url is empty (shown as a hint).
    default_logout_url: str = ""
    kind: str = "generic"
    group_source: Literal["claim", "directory", "none"] = "claim"
    admin_source: Literal["console", "claim"] = "console"
    admin_claim: str = "groups"
    admin_values: list[str] = Field(default_factory=list)
    subject_claim: str = "sub"
    sync_adapter: str = "none"
    sync_interval_minutes: int = 60
    sync_deprovision: Literal["disable", "ignore"] = "disable"
    sync_create_users: bool = True
    sync_confirmed: bool = False
    #: What this kind of directory can do; the console shows only these controls.
    capabilities: dict[str, Any] = Field(default_factory=dict)
    #: People signed in under this row's issuer, disabled or not (ADR 0093
    #: §14): what tells an operator a disabled previous row is still worth
    #: keeping the read-only card open on, versus one nobody ever used.
    user_count: int = 0


class EmailSettingsResponse(BaseModel):
    host: str
    port: int
    username: str
    from_address: str
    has_password: bool
    source: str
    enabled: bool


class EmailTestRequest(BaseModel):
    to: str = Field(min_length=3, max_length=255)


class EmailTestResponse(BaseModel):
    ok: bool
    detail: str


class GroupCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=500)


class GroupMemberAddRequest(BaseModel):
    user_id: uuid.UUID


class GroupAdminResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    source: str
    is_active: bool
    member_count: int
    models: list[str]
    #: The search backend this group searches through on ``POST /v1/search``,
    #: by model-row name. Null means no policy: the group cannot use the
    #: unified route at all.
    search_backend: str | None = None


class GroupSearchBackendRequest(BaseModel):
    """Point a group's unified-search policy at a backend, or clear it.

    ``model_id`` is the backend's anchor row. Setting it requires the group to
    already be granted that backend — a policy the group may not use would
    resolve to a 404 on every search, which is a misconfiguration, not a
    permission. Null clears the policy: the group loses the unified route,
    and keeps the passthrough exactly as granted.
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model_id: uuid.UUID | None = None


class UserAdminResponse(BaseModel):
    id: uuid.UUID
    email: str | None
    display_name: str | None
    #: The directory's own name for this person, `preferred_username`. Distinct
    #: from `display_name`: this is the name an administrator created the
    #: account under and will search for. Null for a local account, and null
    #: for a directory account that has not signed in since the column existed
    #: — the claim only arrives with a login.
    username: str | None = None
    issuer: str
    subject: str
    is_active: bool
    is_admin: bool
    # True when the account can sign in with a password (ADR 0043). The hash
    # itself never leaves the database; the fact of its existence is what the
    # console's user screen needs.
    has_password: bool = False
    # The directories that also name this account (ADR 0056), by issuer. A
    # linked account has two doors, and an operator reading a user screen that
    # says only "has a password" would not know the second one exists.
    linked_identities: list[str] = Field(default_factory=list)
    groups: list[str]
    # Who granted *this* person's membership of the group being listed (ADR
    # 0057): "manual" for an administrator's grant, "oidc" for the directory's.
    # Only the group-members route can answer it — a user has many memberships
    # and the question is per group — so it is None everywhere else rather than
    # a value that would be a guess.
    membership_source: str | None = None
    default_billing_group: str | None
    active_key_count: int
    last_login_at: datetime | None
    #: Set only by `PATCH /admin/users/{id}` when it just tried to sync
    #: Authelia's own `disabled` flag and that write failed — the gateway
    #: side of the same action already stands regardless. `None` on a
    #: successful sync, and on every other response this schema serves
    #: (the list, `create_user`, …), which never set it at all. The console
    #: shows `authelia_sync_message` and resending the same PATCH is the
    #: retry (ADR 0093 §9.1).
    authelia_sync: Literal["failed"] | None = None
    authelia_sync_message: str | None = None


def _clean_optional_text(value: str | None) -> str | None:
    """Strip surrounding whitespace; an empty result clears the field.

    A PATCH that means "no value here" should write ``NULL``, not an empty
    string — the two render differently in the console (blank versus unset)
    and only the first is honest about what the row holds.
    """
    if value is None:
        return None
    value = value.strip()
    return value or None


def _well_formed_email(value: str | None) -> str | None:
    """The same shape check ``create_user`` applies, so both doors agree.

    Deliberately shallow — one ``@`` with something on both sides — because a
    stricter parser rejects real addresses (quoted local parts, IDN) that the
    deployment's own mail path would accept. The column is indexed, not
    unique: an address two accounts share is a data state, not a refusal.
    """
    if value is None:
        return None
    if "@" not in value or value.startswith("@") or value.endswith("@"):
        raise ValueError("A valid email address is required.")
    return value


class UserUpdateRequest(BaseModel):
    """Everything an administrator may change about one account.

    What is deliberately absent: ``issuer`` and ``subject``. The pair *is* the
    login identity (``uq_users_issuer_subject``) — rewriting it would re-key
    the person, sever their memberships' meaning and re-provision them as a
    stranger at the next login. Pydantic ignores unknown keys, so a client that
    sends them gets an edit of the fields it did send and no error about the
    ones it did not; identity is simply not on offer here.

    The profile fields are optional in the PATCH sense: absent leaves the
    column alone, an explicit ``null`` (or an empty string, which the
    validators fold to ``null``) clears it. A cleared field is also recorded
    as administrator-edited — "no display name" is a decision too, and the
    directory disagreeing at the next login would undo it.
    """

    is_active: bool | None = None
    is_admin: bool | None = None
    email: Annotated[
        str | None, AfterValidator(_clean_optional_text), AfterValidator(_well_formed_email)
    ] = Field(default=None, max_length=320)
    display_name: Annotated[str | None, AfterValidator(_clean_optional_text)] = Field(
        default=None, max_length=255
    )
    #: The directory's own name for this person — editable here like the other
    #: profile fields, and (uniquely among them) watched by
    #: ``sync_user_from_claims``'s backfill clause, which must therefore treat
    #: an edited value as settled rather than as a divergence to provision over.
    username: Annotated[str | None, AfterValidator(_clean_optional_text)] = Field(
        default=None, max_length=255
    )


class BundledUserCreateRequest(BaseModel):
    """``POST /admin/users``, bundled Authelia only (ADR 0093 §8.1/§8.2).

    ``groups`` are console groups — ``manual`` memberships, granted the same
    way a pre-assigned directory group is — never Authelia file groups, which
    stop being administrator-editable at all (§8.3, every entry is
    ``["users"]``). The shape check here is deliberately shallow, the same
    one ``UserUpdateRequest.email`` applies; ``UsersFile.create``'s stricter
    §6.1 validator is what actually decides whether the login file will
    accept the address, and duplicating its rules into the request schema
    would only let the two quietly disagree later.
    """

    login: str = Field(min_length=1, max_length=64)
    display_name: str = Field(default="", max_length=255)
    email: Annotated[str, AfterValidator(_well_formed_email)] = Field(max_length=320)
    groups: list[str] = Field(default_factory=list, max_length=100)


class BundledUserCreatedResponse(UserAdminResponse):
    """Includes the one-time password, shown once (§8.1)."""

    password: str


class SignInCreateRequest(BaseModel):
    """``POST /admin/users/{id}/sign-in``: a bundled login for an existing
    gateway user who has none yet (§8.1's "Create sign-in" — the
    after-a-switch and after-break-glass case)."""

    login: str = Field(min_length=1, max_length=64)


class PasswordResetResponse(BaseModel):
    password: str


class MergePreviewResponse(BaseModel):
    """``GET /admin/users/{source}/merge-preview?into=`` (ADR 0093 §7.1): the
    dry run the console shows before an operator can even reach the typed
    confirmation."""

    source_id: uuid.UUID
    target_id: uuid.UUID
    counts: dict[str, int]
    identities_moving: list[IdentityRef]
    identities_dropped: list[IdentityRef]
    resulting_is_admin: bool
    bundled_logins_disabled: list[str]
    #: A `limit_rules`/`redaction_rules` row the source holds that collides
    #: with one the target already has (same metric/window/period, or -- for
    #: `redaction_rules` -- any at all): kept as the target's, the source's
    #: dropped rather than moved. Already excluded from `counts`.
    duplicate_rules_dropped: int
    chat_note: str


class MergeRequest(BaseModel):
    """``POST /admin/users/{source}/merge``. ``confirm`` must equal the
    source's email, or its id when it has none -- typed by hand, not a
    checkbox, because this is irreversible."""

    into: uuid.UUID
    confirm: str
    reason: str = Field(min_length=1)


class MergeResponse(BaseModel):
    target_id: uuid.UUID
    counts: dict[str, int]
    identities_dropped: list[IdentityRef]
    bundled_logins_disabled: list[str]
    duplicate_rules_dropped: int


class ChatSharedResource(BaseModel):
    """One resource of this person that someone else can see, named, with who
    can see it (§9.2: "every resource of the user that someone else can see,
    named, with who can see it") -- passed through from the chat's own
    erasure preview verbatim, not recomputed here."""

    kind: str
    id: str
    title: str
    audience: str


class DeletePreviewResponse(BaseModel):
    """``GET /admin/users/{id}/delete-preview`` (ADR 0093 §9.2). The dialog
    shows this verbatim before the confirmation.
    """

    user_id: uuid.UUID
    gateway_counts: dict[str, int]
    #: The bundled Authelia login bound to this account, if any.
    bundled_login: str | None
    #: `None` when the chat could not be reached at all -- shown as "chat
    #: counts unavailable; the erasure will be queued and retried" rather
    #: than blocking the preview on it.
    chat_counts: dict[str, int] | None
    chat_reachable: bool
    #: Named and audienced, per `ChatSharedResource`'s own doc comment.
    #: `DELETE` refuses without `confirm_shared_loss` while this (or the
    #: legacy count below) is non-empty.
    shared: list[ChatSharedResource]
    #: `bool(shared) or bool(chat_unattributed_legacy_shares)` -- kept
    #: alongside `shared` rather than left for the console to recompute, since
    #: the legacy count is a system-wide caveat with no resource to name.
    shared_with_others: bool
    chat_unattributed_legacy_shares: int


class DeleteUserRequest(BaseModel):
    #: Required (true) once the preview reports `shared_with_others`;
    #: otherwise ignored. §9.2: "delete isn't refused because of shares... the
    #: tick box makes the consequence explicit instead."
    confirm_shared_loss: bool = False


class DeleteUserResponse(BaseModel):
    erasure_id: uuid.UUID
    #: Whether the chat confirmed inline, right after the commit -- `False`
    #: means it is `pending` and the retry loop has it now.
    chat_erasure_done: bool


class PendingErasuresResponse(BaseModel):
    """``GET /admin/erasures/pending`` (ADR 0093 §9.3): the Users page's own
    banner, "N erasures waiting for the chat", while any are pending."""

    pending: int


class OidcMappingRule(BaseModel):
    """One IdP group → local group mapping (ADR 0048)."""

    idp: str = Field(min_length=1, max_length=255)
    local: str = Field(min_length=1, max_length=255)


class OidcPolicyUpdateRequest(BaseModel):
    """A new identity-policy decision.

    Unset fields are ``null`` on the stored row, which means "the environment
    decides this one" — a row can turn one knob without restating the
    deployment's other answers.
    """

    auto_provision: bool | None = None
    # Only meaningful while auto-provisioning is off; the API refuses it
    # alongside `auto_provision: true`, where it would silently never apply.
    unknown_user_policy: Literal["refuse", "create_inactive"] | None = None
    groups_claim: str | None = Field(default=None, min_length=1, max_length=255)
    group_mappings: list[OidcMappingRule] | None = None
    reason: str = Field(default="", max_length=500)


class OidcPolicyChange(BaseModel):
    reason: str
    changed_at: datetime
    changed_by: str | None = None


class OidcPolicyResponse(BaseModel):
    """The policy in force on this worker, and where each part came from."""

    auto_provision: bool
    unknown_user_policy: Literal["refuse", "create_inactive"]
    groups_claim: str
    group_mappings: list[OidcMappingRule]
    # "console" when any field is a stored decision, "environment" otherwise;
    # `sources` says which, per field — "the console says X and the
    # environment says Y" is otherwise invisible.
    source: str
    sources: dict[str, str] = {}
    configured: OidcPolicyChange | None = None
    # How long a saved change may take to reach every worker (the poll
    # interval). Reported rather than implied: a change that looks instant and
    # is not is worse than one that says how long it takes.
    propagation_seconds: float = 0.0


class LimitRuleCreateRequest(BaseModel):
    name: str = Field(default="", max_length=255)
    scope: Literal["global", "group", "user", "api_key"]
    scope_id: uuid.UUID | None = None
    # `own_search_requests` bounds **volume, not spend**: it counts calls to
    # our own web-search backends, whose per-request prices differ by more than
    # an order of magnitude between tiers (Exa `deep-reasoning` against
    # `instant`, Linkup `deep` against `flash`). A caller setting one is
    # limiting how many searches happen, not how much they cost, and every
    # surface that offers this has to say so — see
    # `LimitMetric.OWN_SEARCH_REQUESTS`.
    metric: Literal["requests", "tokens", "cost", "own_search_requests"]
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
    # Provider-side web searches, charged per search on top of tokens
    # (ADR 0058). Reported for the reason `images` is: part of this cost was
    # not tokens, and a reader reconciling against a provider's invoice needs
    # the count the invoice is itemised by.
    searches: int = 0
    # Searches *we* made, against a backend of our own (Exa, Jina, Staan,
    # Linkup). A separate figure from `searches` above and never folded into
    # it: one is the counterparty searching while serving a chat request, the
    # other is this deployment calling a search API, and a reader reconciling
    # against either invoice needs to know which they are looking at. Counted,
    # never priced — the decision recorded on `LimitMetric.OWN_SEARCH_REQUESTS`.
    own_searches: int = 0
    cost: Money
    # The native figure — what the model's price table produced, in its own
    # currency (ADR 0054). Present on model rows only: a model's rows share one
    # price currency, so the native sum means something; a group, user or day
    # mixes currencies and reports in the billing currency alone.
    native_cost: Money | None = None
    native_currency: str | None = None
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
    #: The installed entities partitioned by recognizer family. Older detection
    #: services do not report these; the gateway then leaves `entities` alone.
    pattern_entities: list[str] = Field(default_factory=list)
    model_entities: list[str] = Field(default_factory=list)
    #: False on detection services that predate family selection. The gateway
    #: must not treat their unpartitioned entity list as narrowed support.
    family_partition: bool = False


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
    #: Null leaves the Presidio sidecar's deployment default unchanged.
    presidio_pattern_matching: bool | None = None
    presidio_ner: bool | None = None
    reason: str
    changed_at: datetime
    #: Null once a user has been erased. The record of the change survives them,
    #: which is the point of the column being nullable rather than cascading.
    changed_by: str | None = None


class RedactionEngineRequest(BaseModel):
    """Enable one engine, or switch the layer off by naming ``noop``.

    One engine plus a reason and its Presidio family switches, deliberately. The
    endpoint, the placeholder key and the other detection parameters stay in the
    environment: the key is a secret whose rotation re-labels every transcript
    it ever labelled, and an endpoint that can be typed here is an endpoint that
    can be pointed at a logger.
    """

    engine: str = Field(min_length=1, max_length=64)
    #: Independently enable Presidio's regex/checksum recognizers. Null preserves
    #: the detector deployment's default for compatibility.
    presidio_pattern_matching: bool | None = None
    #: Independently enable Presidio named-entity recognition. Null preserves the
    #: detector deployment's default for compatibility.
    presidio_ner: bool | None = None
    #: Required when the chosen engine redacts nothing, optional otherwise.
    #: Checked in the route rather than here, because the rule depends on the
    #: registry — which engines redact — and a schema that had to consult the
    #: registry to validate one field would be the wrong place for it.
    reason: str = Field(default="", max_length=500)


class RedactionStatusResponse(BaseModel):
    """The redaction layer as it is actually running.

    Everything except ``engine`` and the Presidio detector families still comes
    from process configuration read at startup (ADR 0012). ``engine`` is now an
    admin decision that may override it (ADR 0033), which is why ``source``
    exists: "the console says one thing and the environment says another" is
    otherwise invisible, and it is exactly the confusion a database override
    introduces.
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
    presidio_pattern_matching: bool | None = None
    presidio_ner: bool | None = None
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
    """Everything about a rule may change, its subject included.

    The subject was frozen once, on the argument that a rule *is* a decision
    about one subject and re-pointing it would silently make two subjects'
    histories read as one. That argument does not survive contact with the
    ledger: ``usage_records`` stamps the scope and the rule id onto every
    request at request time, so the past explains itself no matter what the row
    says now — and freezing had a real cost the argument ignored. ``scope_id``
    is not a foreign key, so a deleted group leaves its rule pointing at
    nothing: inert, labelled "deleted subject", and unrepairable — delete it and
    retype a whole policy, or live with a rule that matches nothing. Re-pointing
    is therefore accepted, validated exactly as creation is, and a subject that
    already has a rule stays a 409: one rule per subject is the database's
    promise and this endpoint does not get to merge two policies quietly.
    """

    name: str | None = Field(default=None, max_length=255)
    #: Together with ``scope_id`` or in place of it: a new scope without a
    #: subject is legal only for the catch-all, which names none. Omitted
    #: entirely, the subject is untouched — an absent field is not a null one.
    scope: RedactionScopeName | None = None
    scope_id: uuid.UUID | None = None
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
    #: Per page, for an OCR model, whose token rates are zero and whose real
    #: price is this. Shown before adopting for the same reason the token rates
    #: are: an operator approving a price should see the one that will be charged.
    per_page: Money | None = None
    currency: str | None
    context_window: int | None
    max_input_tokens: int | None = None
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
    # Where the figures above came from: "provider" for the counterparty's own
    # catalogue, "community" for a gap the LiteLLM file filled, None for a model
    # the provider listed and nobody has priced. Shown per row rather than once
    # per import, because a single import can mix all three and "which of these
    # prices did a third party supply" is the question an operator asks before
    # adopting them (ADR 0053).
    price_source: str | None = None


class CatalogueDriftRow(BaseModel):
    # Carried so the console can link straight to the model. The drift that
    # matters is "we serve this and the provider does not offer it", and the only
    # useful next click is the screen where it can be repointed or deactivated.
    id: uuid.UUID
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


class CatalogueTagsResponse(BaseModel):
    """The tag vocabulary a provider slices its catalogue by.

    Feeds the discovery dialog's dropdown, read live from the counterparty so
    the list is theirs rather than one compiled here that goes stale. A
    provider whose plugin knows no way to ask for the whole catalogue reports
    ``supported: false`` with an empty list — the dialog keeps free text for
    those rather than offering a dropdown of one default option and pretending
    it is the vocabulary.
    """

    provider_url: str
    supported: bool
    tags: list[str] = Field(default_factory=list)


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
    # As on `DiscoveredModel`, and for the same reason: the price history records
    # what was adopted, and this records who supplied it. A community figure that
    # later proves wrong is then traceable to the import that took it, rather
    # than looking like a hand-typed mistake.
    price_source: str | None = None


class ModelImportResponse(BaseModel):
    results: list[ModelImportResult]

    @property
    def imported_count(self) -> int:
        return sum(1 for r in self.results if r.imported)
