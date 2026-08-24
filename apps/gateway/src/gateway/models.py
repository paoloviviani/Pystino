"""SQLAlchemy 2.0 ORM models.

Ledger design notes:

* ``usage_records`` is the ledger of record. Valkey counters are a rebuildable
  cache derived from this table, never the other way round (ADR 0006).
* A usage row is created **before** the upstream call and finalised after, so
  that a stream interrupted by a suspended mobile client still leaves both the
  accrued usage and the partial assistant text on disk (ADR 0007).
* Cost is computed and **stored** at request time, together with the id of the
  price row that produced it. Re-pricing a model must never rewrite history, and
  an auditor must be able to see which price was applied.
* Identity foreign keys are nullable with ``ON DELETE SET NULL`` so that erasing
  a user under GDPR Art. 17 does not require deleting or falsifying the
  financial ledger.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from gateway.periods import PeriodKind
from gateway.types import Money, TZDateTime, utcnow


class Base(DeclarativeBase):
    # Map bare Python annotations onto our safe column types, so that a
    # `Mapped[Decimal]` can never accidentally become a float column.
    type_annotation_map: ClassVar[dict[object, object]] = {
        Decimal: Money,
        datetime: TZDateTime,
    }


def _enum(python_type: type[enum.Enum], name: str) -> Enum:
    """Portable enum column: VARCHAR + CHECK rather than a native PG type.

    Native PostgreSQL enums need CREATE TYPE and make every future value
    addition a migration with a lock. Values here are stable identifiers, and a
    CHECK constraint gives the same protection.
    """
    return Enum(
        python_type,
        name=name,
        native_enum=False,
        length=32,
        values_callable=lambda cls: [member.value for member in cls],
        validate_strings=True,
    )


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, default=uuid.uuid4)


class MembershipRole(enum.StrEnum):
    MEMBER = "member"
    ADMIN = "admin"


class GroupSource(enum.StrEnum):
    OIDC = "oidc"
    MANUAL = "manual"


class LimitScope(enum.StrEnum):
    GLOBAL = "global"
    GROUP = "group"
    USER = "user"
    API_KEY = "api_key"


class LimitMetric(enum.StrEnum):
    REQUESTS = "requests"
    TOKENS = "tokens"  # total_tokens, i.e. prompt + completion
    COST = "cost"


class UsageStatus(enum.StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CLIENT_DISCONNECTED = "client_disconnected"
    UPSTREAM_ERROR = "upstream_error"


class UsageSource(enum.StrEnum):
    """Where the token counts on a usage row came from.

    ``ESTIMATED`` exists because some upstreams drop the trailing usage frame of
    a stream (LiteLLM issue #25389 does exactly this for vLLM backends, and was
    closed as not-planned). Recording zero in that case would understate spend
    silently, so we count locally and label the row honestly instead.
    """

    UPSTREAM_EXACT = "upstream_exact"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"


class AuthScheme(enum.StrEnum):
    """How a provider expects its API key to be presented."""

    BEARER = "bearer"
    X_API_KEY = "x_api_key"


class ApiSurface(enum.StrEnum):
    """Which endpoint a request came in through.

    Four of these are OpenAI's shapes and one is Anthropic's; all of them meter
    identically, which is the point of recording the difference rather than
    letting each route keep its own books.
    """

    CHAT_COMPLETIONS = "chat_completions"
    EMBEDDINGS = "embeddings"
    RESPONSES = "responses"
    MESSAGES = "messages"
    IMAGES = "images"


class BillingMode(enum.StrEnum):
    """Which figure is the charge for a counterparty's requests (ADR 0032).

    ``OWN_PRICES`` multiplies our price row by the token counts. ``PROVIDER_REPORTED``
    takes the counterparty's own figure, for a deployment whose policy is strict
    pass-through cost recovery. Both figures are recorded in either mode, so a
    divergence is always reconstructable.
    """

    OWN_PRICES = "own_prices"
    PROVIDER_REPORTED = "provider_reported"


class CostSource(enum.StrEnum):
    """Which figure actually produced ``usage_records.cost``.

    ``OWN_PRICES_FALLBACK`` is the one that matters: the provider is configured
    for pass-through but reported nothing for this request, so our arithmetic was
    used instead. Recorded rather than silent — a pass-through deployment quietly
    billing from a price table nobody maintains is the failure this names.
    """

    OWN_PRICES = "own_prices"
    PROVIDER_REPORTED = "provider_reported"
    OWN_PRICES_FALLBACK = "own_prices_fallback"


class ProviderKind(enum.StrEnum):
    """Whether the serving endpoint is implied by the model or chosen per request.

    Mirrors `gateway.plugins.base.ProviderKind`, deliberately duplicated so the
    model layer does not depend on the plugin package — the plugins import the
    models, and the reverse would be a cycle.
    """

    PROVIDER = "provider"
    ROUTER = "router"


class PriceSource(enum.StrEnum):
    MANUAL = "manual"
    CORTECS = "cortecs"


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[uuid.UUID] = _uuid_pk()
    # For OIDC-sourced groups this is the raw claim value, so it must tolerate
    # anything an IdP emits (paths like "/research/ai", URNs, GUIDs).
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    source: Mapped[GroupSource] = mapped_column(
        _enum(GroupSource, "group_source"), default=GroupSource.MANUAL
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )
    model_access: Mapped[list[GroupModelAccess]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Group {self.name}>"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        # Identity is (issuer, subject). Email is not identity: it is mutable and
        # can be reassigned between people.
        UniqueConstraint("issuer", "subject", name="uq_users_issuer_subject"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    issuer: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320), index=True, default=None)
    display_name: Mapped[str | None] = mapped_column(String(255), default=None)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))

    # The user's own choice of which group to bill by default. Users change this
    # themselves; the gateway validates that they are still a member of it at
    # request time, so a stale value cannot be used to bill a group they left.
    default_billing_group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("groups.id", ondelete="SET NULL"), default=None
    )

    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(default=None)

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )
    api_keys: Mapped[list[ApiKey]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    model_access: Mapped[list[UserModelAccess]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    default_billing_group: Mapped[Group | None] = relationship(
        foreign_keys=[default_billing_group_id], lazy="joined"
    )

    def group_ids(self) -> set[uuid.UUID]:
        return {m.group_id for m in self.memberships}

    def __repr__(self) -> str:
        return f"<User {self.issuer}/{self.subject}>"


class Membership(Base):
    __tablename__ = "memberships"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[MembershipRole] = mapped_column(
        _enum(MembershipRole, "membership_role"), default=MembershipRole.MEMBER
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    user: Mapped[User] = relationship(back_populates="memberships")
    group: Mapped[Group] = relationship(back_populates="memberships", lazy="joined")


class ApiKey(Base):
    """A revocable programmatic credential.

    Keys are high-entropy random values, so they are stored as a plain SHA-256
    digest rather than under a slow KDF: there is no low-entropy secret to
    brute-force, and a per-request Argon2 verification would add tens of
    milliseconds to every call. See docs/adr/0010-api-keys.md
    """

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))

    # Public, non-secret handle: indexed so a lookup is one row, after which the
    # full digest is compared in constant time.
    prefix: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    name: Mapped[str] = mapped_column(String(255), default="")

    # Pins this key to a group. NULL means "use the user's default billing group
    # at request time", so changing the default retroactively affects the key.
    billing_group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("groups.id", ondelete="SET NULL"), default=None
    )

    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)
    # Written opportunistically; not part of the auth decision.
    last_used_at: Mapped[datetime | None] = mapped_column(default=None)

    user: Mapped[User] = relationship(back_populates="api_keys", lazy="joined")
    billing_group: Mapped[Group | None] = relationship(
        foreign_keys=[billing_group_id], lazy="joined"
    )

    def is_usable(self, *, now: datetime | None = None) -> bool:
        moment = now or utcnow()
        if self.revoked_at is not None:
            return False
        return not (self.expires_at is not None and self.expires_at <= moment)


class ModelKind(enum.StrEnum):
    """What a model does, and therefore which route may use it.

    Imported from the provider catalogue where it says so — Cortecs reports it in
    `output_modalities` (ADR 0028). Asking the chat route for an embedding model
    is refused here rather than forwarded to fail upstream with a
    provider-specific error nobody can act on.
    """

    CHAT = "chat"
    EMBEDDING = "embedding"
    IMAGE = "image"


class Provider(Base):
    """An OpenAI-compatible inference endpoint the gateway can route to.

    Configured through the admin API rather than the environment, so adding a
    provider is not a deploy ([0027](../../../docs/adr/0027-inference-providers.md)).

    The API key is stored **encrypted** (see :mod:`gateway.secrets`) and is never
    returned by the API — only ``api_key_hint``, which is enough to tell two keys
    apart and useless to anyone who reads it.
    """

    __tablename__ = "providers"

    id: Mapped[uuid.UUID] = _uuid_pk()
    # Referenced by people, not by requests: models point at the id.
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, default=None)

    base_url: Mapped[str] = mapped_column(String(500))
    # Ciphertext, or empty for an endpoint that needs no credential — a local
    # vLLM or Ollama usually does not.
    api_key_encrypted: Mapped[str] = mapped_column(Text, default="")
    # Masked, for display. Derived at write time so reading it never needs the
    # decryption key.
    api_key_hint: Mapped[str] = mapped_column(String(64), default="")

    # Sent with every request to this provider. Some endpoints need a routing or
    # tenant header alongside the bearer token.
    extra_headers: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))

    # Whether to add `stream_options: {"include_usage": true}` to streaming
    # requests. True is right for a generic OpenAI-compatible endpoint, where
    # asking is the only way to get usage. False for one that sends usage
    # unconditionally and rejects or deprioritises unknown fields — Cortecs
    # documents both of those (ADR 0028).
    forward_stream_options: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true")
    )

    # How this provider wants the credential presented. Anthropic's own API
    # takes `x-api-key` and rejects a bearer token; everything OpenAI-shaped —
    # including the reference provider's /v1/messages — takes bearer, which is
    # why this is a property of the provider and not of the route (ADR 0030).
    auth_scheme: Mapped[AuthScheme] = mapped_column(
        _enum(AuthScheme, "auth_scheme"),
        default=AuthScheme.BEARER,
        server_default=AuthScheme.BEARER.value,
    )

    # Which plugin carries this counterparty's quirks, and what kind of
    # counterparty it is (ADR 0032). Null plugin means the generic
    # OpenAI-compatible behaviour, which is what every row had before plugins
    # existed. Not an enum column for the same reason the redaction engine is
    # not: the accepted names live in the registry beside the code, and an
    # installed package can add one.
    plugin: Mapped[str | None] = mapped_column(String(64), default=None)
    # A router chooses the serving endpoint per request; a provider's is implied
    # by the model. That difference decides whether "which endpoint ran" is a
    # fact worth recording per request.
    kind: Mapped[ProviderKind] = mapped_column(
        _enum(ProviderKind, "provider_kind"),
        default=ProviderKind.PROVIDER,
        server_default=ProviderKind.PROVIDER.value,
    )

    # Whose figure is the charge. Defaults to our own prices, which is what
    # every row did before this existed.
    billing_mode: Mapped[BillingMode] = mapped_column(
        _enum(BillingMode, "billing_mode"),
        default=BillingMode.OWN_PRICES,
        server_default=BillingMode.OWN_PRICES.value,
    )

    # How to read this provider's self-reported cost, if it reports one. Not an
    # enum column: the accepted names live in `UPSTREAM_COST_UNITS` beside the
    # arithmetic that uses them, the same way redaction engine names are
    # validated by their registry rather than by the schema.
    #
    # Null means "ignore what it reports", which is the default and the only
    # safe one — nothing in the payload says whether `cost: 136` means 136
    # micro-EUR or 136 of something else, and guessing is wrong by a factor of a
    # million. The reference provider is `micro_eur`.
    upstream_cost_unit: Mapped[str | None] = mapped_column(String(32), default=None)

    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    models: Mapped[list[ModelDef]] = relationship(back_populates="provider")

    def __repr__(self) -> str:
        return f"<Provider {self.name}>"


class ModelDef(Base):
    """A model the gateway is willing to expose, and how to reach it upstream."""

    __tablename__ = "models"

    id: Mapped[uuid.UUID] = _uuid_pk()
    # What clients send as "model". Stable and ours to choose.
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    # What we send upstream. Lets us rename or repoint without breaking clients.
    upstream_model: Mapped[str] = mapped_column(String(255))

    # Which endpoint serves it. NOT NULL and RESTRICT on delete: a model must
    # resolve to exactly one provider, and a provider cannot be removed while
    # anything still points at it — cascading would leave historical spend
    # attributed to a model nobody can explain.
    provider_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("providers.id", ondelete="RESTRICT"), index=True
    )

    kind: Mapped[ModelKind] = mapped_column(
        _enum(ModelKind, "model_kind"), default=ModelKind.CHAT, server_default="chat"
    )

    display_name: Mapped[str | None] = mapped_column(String(255), default=None)
    description: Mapped[str | None] = mapped_column(Text, default=None)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    context_window: Mapped[int | None] = mapped_column(Integer, default=None)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, default=None)

    # What the model can be given, what it can produce, and what it can do
    # (ADR 0031). Free-form string lists rather than a column of booleans:
    # the reference provider documents `supported_features` as an open set —
    # "current values include json_mode, reasoning and tools" — and a boolean
    # per feature would need a migration every time a provider adds one.
    #
    # Imported from the provider's catalogue and editable afterwards, because
    # the catalogue is a claim rather than a contract and an operator who has
    # found out otherwise needs somewhere to record it.
    input_modalities: Mapped[list[str]] = mapped_column(
        JSON, default=list, server_default=text("'[]'")
    )
    output_modalities: Mapped[list[str]] = mapped_column(
        JSON, default=list, server_default=text("'[]'")
    )
    supported_features: Mapped[list[str]] = mapped_column(
        JSON, default=list, server_default=text("'[]'")
    )

    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    prices: Mapped[list[ModelPrice]] = relationship(
        back_populates="model", cascade="all, delete-orphan"
    )
    group_access: Mapped[list[GroupModelAccess]] = relationship(
        back_populates="model", cascade="all, delete-orphan"
    )
    user_access: Mapped[list[UserModelAccess]] = relationship(
        back_populates="model", cascade="all, delete-orphan"
    )
    provider: Mapped[Provider] = relationship(back_populates="models", lazy="joined")

    def __repr__(self) -> str:
        return f"<ModelDef {self.name}>"


class ModelPrice(Base):
    """Effective-dated pricing.

    Prices are append-only and selected by ``effective_from <= now``, so a price
    change never alters the cost already recorded against past requests.
    """

    __tablename__ = "model_prices"
    __table_args__ = (
        UniqueConstraint("model_id", "effective_from", name="uq_model_prices_model_effective"),
        Index("ix_model_prices_model_effective", "model_id", "effective_from"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    model_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("models.id", ondelete="CASCADE"))

    # Per million tokens, matching how every provider publishes prices. Dividing
    # by 1e6 once at the end keeps the arithmetic exact.
    input_per_mtok: Mapped[Decimal]
    output_per_mtok: Mapped[Decimal]
    cache_read_per_mtok: Mapped[Decimal | None] = mapped_column(default=None)
    cache_write_per_mtok: Mapped[Decimal | None] = mapped_column(default=None)
    # Per generated image, for the image models that are not priced per token
    # (ADR 0030). Not per million of anything — the divisor does not apply.
    per_image: Mapped[Decimal | None] = mapped_column(default=None)

    currency: Mapped[str] = mapped_column(String(3))
    effective_from: Mapped[datetime] = mapped_column(default=utcnow)
    source: Mapped[PriceSource] = mapped_column(
        _enum(PriceSource, "price_source"), default=PriceSource.MANUAL
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    model: Mapped[ModelDef] = relationship(back_populates="prices")


class GroupModelAccess(Base):
    """Per-group model availability.

    Absence of a row means no access. There is no global allow-all: a model a
    nobody has been granted is invisible, which is the safe default for a
    per-group billing system.
    """

    __tablename__ = "group_model_access"

    group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )
    model_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("models.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    group: Mapped[Group] = relationship(back_populates="model_access")
    model: Mapped[ModelDef] = relationship(back_populates="group_access")


class UserModelAccess(Base):
    """Per-user model availability, in addition to whatever their groups grant.

    Access is the **union** of the two: a caller may use a model if their group
    has it or they do personally ([0027](../../../docs/adr/0027-inference-providers.md)).
    It exists so that "give this one researcher the expensive model" does not
    require inventing a group for one person.

    There is deliberately no *denial* row. An explicit deny overriding a group
    grant turns "why can this person not use that model" into a question needing
    a search rather than a look.
    """

    __tablename__ = "user_model_access"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    model_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("models.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    user: Mapped[User] = relationship(back_populates="model_access")
    model: Mapped[ModelDef] = relationship(back_populates="user_access")


class UsageRecord(Base):
    __tablename__ = "usage_records"
    __table_args__ = (
        # Quota aggregation reads "rows for this scope since T", so every scope
        # gets a composite index leading with its id.
        Index("ix_usage_group_created", "group_id", "created_at"),
        Index("ix_usage_user_created", "user_id", "created_at"),
        Index("ix_usage_key_created", "api_key_id", "created_at"),
        Index("ix_usage_created", "created_at"),
        Index("ix_usage_status", "status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    # Correlates the row with logs and with the id returned to the client.
    request_id: Mapped[str] = mapped_column(String(64), index=True)

    status: Mapped[UsageStatus] = mapped_column(
        _enum(UsageStatus, "usage_status"), default=UsageStatus.IN_PROGRESS
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("groups.id", ondelete="SET NULL"), default=None
    )
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("api_keys.id", ondelete="SET NULL"), default=None
    )
    model_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("models.id", ondelete="SET NULL"), default=None
    )
    # Denormalised so the ledger stays readable after a model is retired.
    model_name: Mapped[str] = mapped_column(String(255))

    streamed: Mapped[bool] = mapped_column(Boolean, default=False)

    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # Prompt tokens written to a provider-side cache. Billed at its own rate and
    # until now computed but never stored, so a cached request could not be
    # explained after the fact and the ledger could not be repriced.
    cache_write_tokens: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )
    reasoning_tokens: Mapped[int] = mapped_column(Integer, default=0)

    usage_source: Mapped[UsageSource] = mapped_column(
        _enum(UsageSource, "usage_source"), default=UsageSource.UNAVAILABLE
    )

    # What the provider said actually served the request, as opposed to what we
    # asked for. A router with model fallback can substitute (ADR 0028), and
    # `model_name` above is the request, not the answer. Null when the provider
    # reported neither, which is most non-routing endpoints.
    upstream_model: Mapped[str | None] = mapped_column(String(255), default=None)
    upstream_provider: Mapped[str | None] = mapped_column(String(128), default=None)
    # Whether that differs from the model we actually asked the provider for.
    #
    # Decided at write time, not by comparing columns later: `model_name` is our
    # client-facing name and `upstream_model` is the provider's, so those two
    # always differ and comparing them would call every request a substitution.
    # The recorder holds both the requested upstream name and the reported one,
    # so it is the only place that can tell.
    model_substituted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )

    # Which API surface served the request (ADR 0030). Derivable from the model
    # for chat vs embedding, but not for the two surfaces that serve the *same*
    # chat models — a caller can reach one model through /v1/chat/completions,
    # /v1/responses or /v1/messages, and "can we retire the Anthropic surface"
    # is a question only this column answers.
    api_surface: Mapped[ApiSurface] = mapped_column(
        _enum(ApiSurface, "api_surface"),
        default=ApiSurface.CHAT_COMPLETIONS,
        server_default=ApiSurface.CHAT_COMPLETIONS.value,
    )
    # Images produced, and the size asked for. Recorded rather than derived
    # because many image models are priced per image by size, and without both
    # the ledger cannot be repriced if that pricing is ever modelled properly.
    image_count: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    image_size: Mapped[str | None] = mapped_column(String(32), default=None)

    cost: Mapped[Decimal] = mapped_column(default=Decimal(0))
    currency: Mapped[str] = mapped_column(String(3))
    # What the *provider* said this cost, in the provider's own currency, when it
    # says so at all and the operator has declared how to read it. Never billed
    # from — it is pre-rounded and possibly in another currency — but it is the
    # only signal that catches a stale price row or an upstream rate change.
    upstream_cost: Mapped[Decimal | None] = mapped_column(default=None)
    upstream_cost_currency: Mapped[str | None] = mapped_column(String(3), default=None)
    # The counterparty's own breakdown, kept verbatim. JSON because the shape is
    # theirs: the reference router reports five components including
    # `prompt_audio_cost`, and the next one will report something else.
    # Never put this in a DISTINCT or GROUP BY — PostgreSQL has no equality
    # operator for `json`.
    upstream_cost_details: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # Always our arithmetic from the price row in force, whichever figure was
    # billed. Equal to `cost` under `own_prices`; the evidence under
    # `provider_reported`.
    computed_cost: Mapped[Decimal] = mapped_column(default=Decimal(0), server_default=text("0"))
    cost_source: Mapped[CostSource] = mapped_column(
        _enum(CostSource, "cost_source"),
        default=CostSource.OWN_PRICES,
        server_default=CostSource.OWN_PRICES.value,
    )
    # Which price row produced `cost`. Null when the model had no price.
    price_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("model_prices.id", ondelete="SET NULL"), default=None
    )

    finish_reason: Mapped[str | None] = mapped_column(String(64), default=None)
    upstream_status: Mapped[int | None] = mapped_column(Integer, default=None)
    latency_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    ttfb_ms: Mapped[int | None] = mapped_column(Integer, default=None)

    # Written incrementally while streaming so a suspended client does not lose
    # the answer. May be partial whenever status != completed.
    assistant_text: Mapped[str | None] = mapped_column(Text, default=None)

    redaction_engine: Mapped[str | None] = mapped_column(String(64), default=None)
    redacted_entity_count: Mapped[int] = mapped_column(Integer, default=0)

    error_code: Mapped[str | None] = mapped_column(String(64), default=None)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)

    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    finalised_at: Mapped[datetime | None] = mapped_column(default=None)

    def __repr__(self) -> str:
        return f"<UsageRecord {self.request_id} {self.status} {self.total_tokens}t>"


class LimitRule(Base):
    """A quota rule: one metric, one scope, one rolling window.

    Rules are additive and all matching rules must pass. There is intentionally
    no priority or override mechanism: "most specific wins" reliably surprises
    people when a user-scoped rule silently raises a group ceiling.
    """

    __tablename__ = "limit_rules"
    __table_args__ = (
        # A plain UNIQUE over these columns does not work, because three of them are
        # nullable and SQL treats NULLs as distinct: two identical global rules
        # (scope_id NULL) or two identical rolling rules (period NULL) would both be
        # accepted. COALESCE to sentinels that no real value can take, so "one rule
        # per scope, metric and window" is actually enforced.
        Index(
            "uq_limit_rules_identity",
            text("scope"),
            text("coalesce(scope_id, '00000000-0000-0000-0000-000000000000')"),
            text("metric"),
            text("coalesce(window_seconds, -1)"),
            text("coalesce(period, '')"),
            unique=True,
        ),
        CheckConstraint(
            "(scope = 'global' AND scope_id IS NULL)"
            " OR (scope <> 'global' AND scope_id IS NOT NULL)",
            name="ck_limit_rules_scope_id_presence",
        ),
        # Correct as-is for a nullable column: a CHECK passes when its expression
        # is NULL, so a calendar rule with no window_seconds satisfies it.
        CheckConstraint("window_seconds > 0", name="ck_limit_rules_window_positive"),
        CheckConstraint(
            "(window_seconds IS NOT NULL AND period IS NULL)"
            " OR (window_seconds IS NULL AND period IS NOT NULL)",
            name="ck_limit_rules_one_window_kind",
        ),
        CheckConstraint("limit_value >= 0", name="ck_limit_rules_limit_non_negative"),
        Index("ix_limit_rules_lookup", "is_active", "scope", "scope_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), default="")

    scope: Mapped[LimitScope] = mapped_column(_enum(LimitScope, "limit_scope"))
    # Group id or user id depending on scope; NULL for global. Not a real FK,
    # because it points at one of two tables depending on `scope`.
    scope_id: Mapped[uuid.UUID | None] = mapped_column(default=None)

    metric: Mapped[LimitMetric] = mapped_column(_enum(LimitMetric, "limit_metric"))

    # Exactly one of these. A rolling window is bucket-approximated and right for
    # rate limiting; a calendar period is exact, resets at the boundary, and is what
    # a budget needs so it reconciles with the monthly report (ADR 0025).
    window_seconds: Mapped[int | None] = mapped_column(Integer, default=None)
    period: Mapped[PeriodKind | None] = mapped_column(
        _enum(PeriodKind, "period_kind"), default=None
    )
    # Numeric for all three metrics: requests and tokens are integral but sharing
    # one column keeps rule evaluation uniform.
    limit_value: Mapped[Decimal]

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    resets: Mapped[list[QuotaReset]] = relationship(
        back_populates="rule", cascade="all, delete-orphan"
    )

    @property
    def window_label(self) -> str:
        return f"{self.window_seconds}s" if self.window_seconds else str(self.period)

    def __repr__(self) -> str:
        return (
            f"<LimitRule {self.scope}:{self.scope_id} {self.metric}"
            f" <= {self.limit_value}/{self.window_label}>"
        )


class QuotaReset(Base):
    """A point in time before which usage no longer counts against a rule.

    Deliberately a watermark rather than a counter mutation. Counters are a
    rebuildable cache and the gateway recomputes them from the ledger when it finds
    the cache cold (ADR 0006), so zeroing a counter would be silently undone by the
    next rebuild. A row here survives that, because the rebuild reads it.

    It also cannot corrupt billing: ``usage_records`` is never touched, so a reset
    changes what the *quota* counts and leaves the monthly report exactly as it was.

    Rows are append-only and never deleted while the rule exists — zeroing someone's
    spending cap is a financially meaningful act and the trail is the point.
    """

    __tablename__ = "quota_resets"
    __table_args__ = (Index("ix_quota_resets_rule_effective", "rule_id", "effective_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    rule_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("limit_rules.id", ondelete="CASCADE"), index=True
    )
    # When the reset takes effect. Always "now" today: resets are immediate, and a
    # future value would be a schedule, which is deliberately not supported.
    effective_at: Mapped[datetime] = mapped_column(default=utcnow)
    # Who did it. Nullable so erasing a user under GDPR does not delete the audit
    # trail of the reset itself.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    reason: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    rule: Mapped[LimitRule] = relationship(back_populates="resets")

    def __repr__(self) -> str:
        return f"<QuotaReset rule={self.rule_id} at={self.effective_at.isoformat()}>"
