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

import sqlalchemy as sa
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
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


class MembershipSource(enum.StrEnum):
    """Who put this person in this group (ADR 0057).

    The load-bearing distinction: a directory may take away what it granted,
    and may not take away what an administrator granted. Provenance is
    per-membership rather than per-group because both kinds occur in the same
    group — a directory grants "engineering" to forty people and an
    administrator adds the contractor who is not in the directory's copy of it.
    """

    #: Granted by an identity provider's answer, and revocable by it.
    OIDC = "oidc"
    #: Granted here, by an administrator or the CLI. Never removed by a login.
    MANUAL = "manual"


class GroupSync(enum.StrEnum):
    """How far an identity provider's answer about groups reaches (ADR 0057).

    Modelled on Keycloak's mapper sync modes, because the question is the same
    one and operators already know the vocabulary. What the three modes govern
    is narrower than it looks: a directory's answer only ever reaches the
    memberships it granted (``MembershipSource.OIDC``). What an administrator
    granted is untouched by all three.
    """

    #: The directory's answer wins on every login. What this gateway has always
    #: done, and the default, because revoking a group in the directory has to
    #: revoke the ability to bill it.
    EVERY_LOGIN = "every_login"
    #: Applied when the account first appears here, and never again. Groups are
    #: seeded from the directory and administered afterwards.
    FIRST_LOGIN = "first_login"
    #: The directory never sets membership at all — not even for a new account.
    #: For a deployment that uses SSO to authenticate and decides authorisation
    #: itself; a new user arrives with no groups and cannot bill until an
    #: administrator puts them in one.
    NEVER = "never"


class LimitScope(enum.StrEnum):
    GLOBAL = "global"
    GROUP = "group"
    USER = "user"
    API_KEY = "api_key"


class RedactionScope(enum.StrEnum):
    """What a redaction rule attaches to.

    Not ``LimitScope``, though the table shape is copied from ``limit_rules``.
    Quotas are about spending, so they scope to who pays; redaction is about
    where personal data goes, so it scopes to **the endpoint text reaches** and
    **the person who wrote it**. Hence ``provider`` and ``model``, which quotas
    have no use for.

    ``ALL`` is the catch-all, and it is a scope rather than a separate
    "deployment policy" object (ADR 0039). Keeping it out of this enum was the
    earlier design's mistake: it gave "what applies here" two answers with
    different shapes — one edited on its own screen, one in a list — and an
    operator had to know that the screen they were looking at was only half the
    answer.
    """

    #: Every request. The catch-all, and the only scope whose ``scope_id`` is
    #: null: there is exactly one subject and it is "everything".
    ALL = "all"
    PROVIDER = "provider"
    MODEL = "model"
    GROUP = "group"
    USER = "user"
    #: The narrowest, and the one a person can set without being an admin only in
    #: the sense that it is their own key. Useful for "this CI key handles
    #: customer data and needs stricter treatment than I do".
    API_KEY = "api_key"


class LimitMetric(enum.StrEnum):
    """What a quota rule counts.

    Three of these are *what a request consumed*. The fourth counts a thing the
    gateway **did**, and it is the only one that is deliberately not money.

    A value added here needs no migration: ``_enum`` stores these as VARCHAR
    with no CHECK, precisely so that a new metric is not a locking DDL change.
    """

    REQUESTS = "requests"
    TOKENS = "tokens"  # total_tokens, i.e. prompt + completion
    COST = "cost"
    #: Calls this gateway made to a web-search backend of its own — Exa, Jina,
    #: Staan, Linkup — counted, never priced.
    #:
    #: Not priced because two of the four rates cannot be established at
    #: source: Jina publishes no per-token price at all, and Staan's higher
    #: "for AI" tier is neither a documented request parameter nor reported
    #: back in the response. A price table where half the rows are guesses
    #: produces invoices that look right and are wrong — the same failure
    #: ``own_prices_fallback`` exists to make loud. A count is never wrong, and
    #: it is also what a vendor dashboard itemises, so it reconciles without a
    #: currency, a rounding rule or a rate table to drift.
    #:
    #: **What this ceiling does not do: bound spend.** Backends charge wildly
    #: different amounts per request — Exa ``deep-reasoning`` is $15 per 1,000
    #: against ``instant`` at $7, and Linkup ``deep`` is ten times ``flash`` —
    #: so 1,000 requests is a volume anyone can reason about and a bill nobody
    #: can. Every surface that offers or reports this metric has to say so;
    #: leaving an operator to discover it from an invoice is the failure.
    OWN_SEARCH_REQUESTS = "own_search_requests"


class UsageStatus(enum.StrEnum):
    IN_PROGRESS = "in_progress"
    #: Refused by redaction before any provider saw it. Recorded rather than
    #: dropped: nothing was billed, but "this deployment refused 400 prompts last
    #: month" is a number a data-protection review asks for, and a 403 that
    #: leaves no trace cannot produce it.
    BLOCKED = "blocked"
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
    #: Document extraction, `POST /v1/ocr`: the Cortecs and Mistral shape,
    #: served either by an upstream OCR model or by this deployment's own
    #: extractor. Metered by the page rather than the token.
    OCR = "ocr"
    #: A web search this gateway ran itself, `POST /v1/search`. The only
    #: surface metered by a count with no rate behind it: `own_search_requests`
    #: rather than `cost`, for the reasons on `LimitMetric.OWN_SEARCH_REQUESTS`.
    SEARCH = "search"


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
    #: A web-search backend rather than an inference endpoint. See
    #: `gateway.plugins.base.ProviderKind.SEARCH` for why this is a kind of
    #: provider and not a table of its own.
    SEARCH = "search"


class PriceSource(enum.StrEnum):
    """Who supplied a price. Stored on every append-only price row.

    This is not decoration. A price is what the gateway invoices from, and the
    three answers carry different weight: a figure typed by an administrator, a
    figure the counterparty published, and a figure a community file supplied
    for a counterparty that publishes none (ADR 0053). When one later proves
    wrong, the row says who to ask.

    No migration was needed to add these: `_enum` stores enums as VARCHAR with
    no CHECK, precisely so that a new value is not a locking DDL change.
    """

    MANUAL = "manual"
    #: The provider's own catalogue, whichever provider it is. `CORTECS` below is
    #: what this was called when Cortecs was the only importer; new rows use this.
    CATALOGUE = "catalogue"
    #: LiteLLM's community price file, used only to fill a price the provider
    #: left out. Distinct from `CATALOGUE` because it is a third party's figure
    #: for someone else's charges — the one kind of price worth re-checking
    #: against an invoice.
    COMMUNITY = "community"
    #: Historical. Written by the original Cortecs importer, and by every
    #: catalogue import until ADR 0053's fill made the distinction matter — which
    #: means existing rows labelled `cortecs` may hold a community figure, and
    #: that cannot be recovered from the row. Kept so those rows still read.
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
    #: The directory's own name for this person — OIDC ``preferred_username``.
    #:
    #: Kept in a column of its own rather than folded into `display_name`,
    #: because it answers a different question. `display_name` is what to
    #: *show* a person; this is what they are *called* in the directory an
    #: administrator created them in, and it is the string that administrator
    #: will type into a search box.
    #:
    #: It was previously read only as a fallback for `display_name`
    #: (`name or preferred_username`), so any directory that sends both threw
    #: the username away. Keycloak sends both by default, which made an account
    #: created as `chat@local` findable only as `chat@example.org` — a real
    #: report, and the reason this column exists.
    #:
    #: Not unique and not identity: identity is `(issuer, subject)`. A username
    #: is mutable and two directories may use the same one for two people.
    username: Mapped[str | None] = mapped_column(String(255), index=True, default=None)

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


class LocalCredential(Base):
    """The email + password credential that lets a user sign in without an IdP.

    A separate table rather than a column on ``users`` on purpose: the users
    table is identity-agnostic — one row per ``(issuer, subject)`` — and most
    rows will never carry a password. Making "can sign in locally" a row here
    means it is enumerable (``SELECT`` over the table is the list of everyone
    with local access, which the audit question an operator actually asks),
    and deleting the row revokes local login without touching the identity.

    The user it points at is keyed ``(issuer="local", subject=email)``, the same
    convention ``gateway seed`` has always used, so by default an OIDC user and
    a local user with the same address are different accounts.

    That default used to be absolute, and the reason was written here: linking
    one person's local credential to their directory identity lets a leaked
    password ride an issuer's trust. That risk is real and has not gone away —
    what changed is who decides. An operator who runs the directory their local
    accounts were named after can now turn linking on **per identity provider**
    (``IdentityProvider.link_local_by_email``, ADR 0056), and the link is
    recorded as a ``UserIdentity`` row rather than by rewriting this row's key.
    Read that ADR before touching either side: the guarantee that makes it
    tolerable is that a verified email is required, and it is what stops an
    ``email`` claim from being a password.
    """

    __tablename__ = "local_credentials"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    # Argon2id via pwdlib (ADR 0043). The column is wider than any current hash
    # so a parameter upgrade never needs a migration.
    password_hash: Mapped[str] = mapped_column(String(512))

    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(foreign_keys=[user_id])


class UserIdentity(Base):
    """An *additional* way one person's account is named by an issuer (ADR 0056).

    ``users`` still holds the identity a row was created with — the primary
    key of a person as far as ``(issuer, subject)`` goes. This table holds the
    others, and exists so that account linking never has to rewrite that pair.

    That is the whole design, and it is not fussiness. ``issuer == "local"`` is
    read in eight places as *"this account's door is a password"* — local
    login, the password reset, both CLI commands, changing your own password,
    the administrator password routes, creating a local account, and the rule
    that refuses an ``is_admin`` edit when the directory is authoritative.
    Rewriting a linked user's issuer to the provider's would flip all eight
    silently: the person would keep an unusable password and the deployment
    would lose the escape hatch that recovers it when the directory is
    misconfigured. So the local row stays local, and the directory identity is
    recorded beside it. ADR 0056 lists the eight.

    Two unique constraints, each preventing a different confusion: one identity
    belongs to one person, and one person has at most one identity per
    directory. Deleting a row unlinks, which is why the link is auditable —
    ``linked_at`` and the address it matched on are kept.
    """

    __tablename__ = "user_identities"
    __table_args__ = (
        UniqueConstraint("issuer", "subject", name="uq_user_identities_issuer_subject"),
        UniqueConstraint("user_id", "issuer", name="uq_user_identities_user_issuer"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    issuer: Mapped[str] = mapped_column(String(512))
    subject: Mapped[str] = mapped_column(String(255))
    # The address the match was made on, kept as it was at the time. The
    # provider may report a different one later and `users.email` follows it;
    # what this link was justified by must not move with it.
    matched_email: Mapped[str | None] = mapped_column(String(320), default=None)
    linked_at: Mapped[datetime] = mapped_column(default=utcnow)

    user: Mapped[User] = relationship(foreign_keys=[user_id])

    def __repr__(self) -> str:
        return f"<UserIdentity {self.issuer}/{self.subject} -> {self.user_id}>"


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
    # Defaults to the administrator's, not the directory's, because four of the
    # five places that create a membership are administrative and exactly one
    # is the login sync — which says so explicitly. A row that does not know
    # where it came from is safer treated as a decision somebody made here.
    source: Mapped[MembershipSource] = mapped_column(
        _enum(MembershipSource, "membership_source"),
        default=MembershipSource.MANUAL,
        server_default=text("'manual'"),
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    user: Mapped[User] = relationship(back_populates="memberships")
    group: Mapped[Group] = relationship(back_populates="memberships", lazy="joined")


class ApiKey(Base):
    """A revocable programmatic credential.

    Keys are high-entropy random values, so they are stored as a plain SHA-256
    digest rather than under a slow KDF: there is no low-entropy secret to
    brute-force, and a per-request Argon2 verification would add tens of
    milliseconds to every call. See ADR 0010
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

    # Set when the gateway minted this key as a short-lived access credential
    # for a named client (ADR 0046). NULL is a key a person created, which is
    # the only kind the listings show; a minted key is never manually creatable
    # and carries no pinned billing group, both enforced in the routes.
    minted_by: Mapped[str | None] = mapped_column(String(64), default=None, index=True)

    user: Mapped[User] = relationship(back_populates="api_keys", lazy="joined")
    billing_group: Mapped[Group | None] = relationship(
        foreign_keys=[billing_group_id], lazy="joined"
    )

    @property
    def is_issued_key(self) -> bool:
        """Was this key issued as a standing credential, rather than minted at login?

        ADR 0046's line, which the ``x-bill-to`` rule in ``deps.py`` also
        follows. A minted key (``minted_by`` set) is a short-lived access
        credential for a named client — including, since the house IdP, the
        deployment's own OIDC session token, which is opaque by design and
        therefore lives in this very table. The two kinds are the same shape
        and different things: a minted credential *is* the proof of who is
        calling, while an issued key is a standing grant whose billing — the
        group it pins, or none — was fixed when it was issued. Steering the
        bill per request suits the first and quietly overrides the second,
        so it is offered to exactly one of them.
        """
        return self.minted_by is None

    def is_usable(self, *, now: datetime | None = None) -> bool:
        moment = now or utcnow()
        if self.revoked_at is not None:
            return False
        return not (self.expires_at is not None and self.expires_at <= moment)


class RefreshCredential(Base):
    """A long-lived credential a *named client* exchanges for access keys.

    ADR 0046. The gateway is the issuer for its own local accounts: a login
    that names a client mints one row here, and ``POST /auth/token`` trades it
    for a short-lived ``ApiKey`` with ``minted_by`` naming the client. One row
    per ``(user, client)``, so a re-login rotates it — the old credential dies
    with the row, which is the correct behaviour for a credential whose only
    holder just authenticated with a password.

    Hashed like an ``ApiKey`` and for the same reason (ADR 0010): a
    2^256-entropy secret has nothing for a slow KDF to defend, and the hash
    means a database dump yields nothing that works.
    """

    __tablename__ = "refresh_credentials"
    __table_args__ = (UniqueConstraint("user_id", "client", name="uq_refresh_user_client"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    client: Mapped[str] = mapped_column(String(64))

    prefix: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    secret_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    # The client's session lifetime; set at mint. An expired credential is
    # refused at exchange, which ends the client's session honestly.
    expires_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(default=None)

    user: Mapped[User] = relationship(foreign_keys=[user_id])


class IdpAuthorizationCode(Base):
    """A single-use authorization code minted at ``/oauth/authorize`` (ADR 0068).

    The one piece of the IdP that cannot be stateless. A signed, self-contained
    code would be a credential that keeps working until its TTL expires no
    matter what the server thinks — replay is exactly the property a code must
    not have, and single-use is a server-side fact, so the code lives here:
    stored only as a SHA-256 hash (the ADR 0010 argument — a 2^256-entropy
    value has nothing for a slow KDF to defend), bound to its client, redirect
    URI and PKCE challenge, and dead the moment it is spent.

    Sixty seconds is the whole lifetime: the code exists to survive one browser
    redirect and one server-to-server POST, and nothing longer.
    """

    __tablename__ = "idp_authorization_codes"

    id: Mapped[uuid.UUID] = _uuid_pk()
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    client_id: Mapped[str] = mapped_column(String(64), index=True)
    # The exact redirect the authorize request carried, stored because the
    # token request must present it back and a mismatch is a refusal.
    redirect_uri: Mapped[str] = mapped_column(String(512))
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    nonce: Mapped[str] = mapped_column(String(128), default="")
    code_challenge: Mapped[str] = mapped_column(String(128))
    scope: Mapped[str] = mapped_column(String(512), default="")

    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(default=utcnow)
    # NULL until spent. The token endpoint sets it in the same transaction that
    # mints the credentials, so two races for one code answer one winner and
    # one refusal.
    used_at: Mapped[datetime | None] = mapped_column(default=None)


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
    #: Document extraction: a document in, its text out, priced per page rather
    #: than per token. Served either by an upstream OCR model or by this
    #: deployment's own extractor, which is the distinction the *provider*
    #: carries and not the kind — `/v1/ocr` is one surface with two kinds of
    #: counterparty behind it.
    OCR = "ocr"
    #: A web search, served by one of this deployment's own search backends.
    #: A "model" here is a **backend at a tier** — `upstream_model` carries
    #: Linkup's `depth` or Exa's `type` — which is what makes the tier a grant
    #: an administrator makes rather than a parameter a caller picks. It is
    #: metered as a count and never priced (`LimitMetric.OWN_SEARCH_REQUESTS`),
    #: so a price row on one of these charges nothing and reserves nothing.
    SEARCH = "search"


class Provider(Base):
    """An OpenAI-compatible inference endpoint the gateway can route to.

    Configured through the admin API rather than the environment, so adding a
    provider is not a deploy (ADR 0027).

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

    # Prepended to every model name this provider contributes, so two vendors
    # whose tiers share a word ("deep", "fast") do not collide in one
    # catalogue. Empty string is no prefix. Applied at import and — via the
    # update route — re-applied to the models already here, because a prefix
    # added after a dozen imports that only reached future ones would leave
    # the catalogue half-disambiguated, which is worse than either.
    prefix: Mapped[str] = mapped_column(String(32), default="", server_default=text("''"))

    # Which plugin carries this counterparty's quirks, and what kind of
    # counterparty it is (ADR 0032). Null plugin means the generic
    # OpenAI-compatible behaviour, which is what every row had before plugins
    # existed. Not an enum column for the same reason the redaction engine is
    # not: the accepted names live in the registry beside the code, and an
    # installed package can add one.
    #
    # This column replaced three that were added reactively, one per discovery:
    # `auth_scheme`, `forward_stream_options` and `upstream_cost_unit`. Each was
    # the right local fix; the pattern was that the next counterparty needed a
    # fourth. Migration 0010 dropped them and translated their values into a
    # plugin choice.
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
    # The "in" half, set separately from the window total: the vendor's nominal
    # context is often larger than what actually works (prompt + completion must
    # fit the window, but some providers cap input harder still), and an operator
    # who has measured the real limit needs somewhere to record it. Null means
    # "no local limit", not zero — absence must never read as a rejected prompt.
    # Never overwritten by catalogue import or price refresh: this is the
    # operator's claim about the upstream, not something the catalogue knows.
    max_input_tokens: Mapped[int | None] = mapped_column(Integer, default=None)

    # Public access (ADR 0045): any authenticated caller may use this model,
    # billed to the caller's own default billing group. Access is granted;
    # billing is not changed — a caller with no billing group is refused
    # exactly as before, because a public model is not a free model. A flag
    # rather than a synthetic "public" group: the group would appear on every
    # membership, make "which models can everyone use" a join instead of a
    # read, and need guarding against deletion and renaming.
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))

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
    # Per page read, for OCR and document extraction, which is what every OCR
    # counterparty charges by. Same shape as `per_image` and for the same
    # reason: the unit is not a token, so the per-million divisor is wrong.
    #
    # A model may carry this *and* the token rates: an OCR model that also
    # returns a summary is billed for both, and leaving one null is how a real
    # charge records as zero.
    per_page: Mapped[Decimal | None] = mapped_column(default=None)
    # Per server-side web search the provider ran on our behalf (ADR 0058).
    # A third non-token unit, and the one that is a *surcharge*: unlike a page
    # or an image it arrives on top of an ordinary chat request's tokens, so a
    # model priced only per token bills a real charge as zero and nothing on
    # the screen says so. Stored per single search, like `per_image` — the
    # providers publish it per thousand ($10 per 1,000 for Anthropic), and the
    # division happens where the figure is entered, not here.
    per_search: Mapped[Decimal | None] = mapped_column(default=None)

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
    has it or they do personally (ADR 0027).
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
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
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
    # Server-side web searches the provider ran and billed for (ADR 0058).
    # A column for the same reason `image_count` is one, and one migration 0023
    # deliberately refused for *cost*: this is the billable **count**, it is
    # what a provider's invoice is itemised by, and it cannot be recovered from
    # the money afterwards. Only ever from the provider's reported figure —
    # counting the tool-use blocks in a response would bill the searches that
    # errored, which Anthropic states it does not charge for.
    search_count: Mapped[int] = mapped_column(default=0, server_default=text("0"))

    # Web searches **this gateway** ran, against a search backend of its own
    # (docs/web-search-plan.md, phase 2). A separate column from `search_count`
    # above, and the separation is the point: that one counts searches the
    # *counterparty* ran inside a chat request and put on its invoice, this one
    # counts calls we made to Exa, Jina, Staan or Linkup. Summed together they
    # would answer "how many searches happened" and nothing else — a report
    # could no longer tell "Anthropic searched" from "we called Staan", which
    # is the distinction the whole `cost` / `computed_cost` / `upstream_cost`
    # family exists to preserve one table over.
    #
    # A count and never a cost. Half the backends' rates cannot be read at
    # source (see `LimitMetric.OWN_SEARCH_REQUESTS`), so a money column here
    # would be a guess wearing the same type as a measurement.
    own_search_requests: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    # Which backend served them, and at which tier or depth — `exa` +
    # `deep-reasoning`, `staan` + `web_for_ai`, `linkup` + `deep`. Labels only:
    # no rate, no arithmetic, nothing reads them to decide anything.
    #
    # They exist now because they cannot be added later. The backend and tier
    # are facts about a request that only that request knows; a migration that
    # introduced these columns once the searches had already happened could
    # backfill nothing, and reconciling a vendor's dashboard against rows that
    # do not say which vendor they went to is not possible at any price. Cheap
    # to carry, impossible to recover.
    own_search_backend: Mapped[str | None] = mapped_column(String(32), default=None)
    own_search_tier: Mapped[str | None] = mapped_column(String(64), default=None)

    cost: Mapped[Decimal] = mapped_column(default=Decimal(0))
    currency: Mapped[str] = mapped_column(String(3))
    # The native figure: what the model's price table produced, in the price's
    # own currency (ADR 0054). `cost` carries the billing-currency figure the
    # quotas and aggregates read; per-provider and per-model breakdowns report
    # this, because it is what the counterparty actually charges. Null on
    # rows priced in the billing currency, where the two are the same.
    cost_native: Mapped[Decimal | None] = mapped_column(default=None)
    cost_currency: Mapped[str | None] = mapped_column(String(3), default=None)
    # The rate that produced `cost` from `cost_native`, recorded so a
    # converted aggregate can be reconstructed and audited later.
    cost_fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 10), default=None)
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
    #: Which scope's rule tightened this request, and which row it was. Without
    #: them, "why was this redacted" is unanswerable six weeks later — the
    #: question a data-protection review actually asks — because the rule may
    #: have been edited since. Null means the deployment policy alone applied.
    redaction_scope: Mapped[str | None] = mapped_column(String(32), default=None)
    redaction_rule_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
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
        # Rolling windows were rendered as raw seconds — "86400s" — which is a
        # value where a reader wanted a duration. Named units, one unit only:
        # the largest that divides the window exactly, so 86400 is "1 day" and
        # not "24 hours", and 5400 is "90 minutes" rather than a fractional
        # "1.5 hours" that invites a decimal nobody set. Calendar periods
        # ("month") already had their name.
        seconds = self.window_seconds
        if not seconds:
            return str(self.period)
        for unit, size, name in (
            (86400, 86400, "day"),
            (3600, 3600, "hour"),
            (60, 60, "minute"),
        ):
            if seconds % unit == 0 and seconds // unit >= 1:
                value = seconds // size
                return f"{value} {name}" if value == 1 else f"{value} {name}s"
        return f"{seconds}s"

    def __repr__(self) -> str:
        return (
            f"<LimitRule {self.scope}:{self.scope_id} {self.metric}"
            f" <= {self.limit_value}/{self.window_label}>"
        )


class PasswordResetToken(Base):
    """A single-use link for a self-service password reset (ADR 0049).

    The table stores the SHA-256 **hash** of the token the email carried, exactly
    as ``api_keys`` stores a key's hash: whoever reads the database cannot reset
    anyone's password. ``used_at`` is set by the confirm endpoint; a spent row is
    kept until the account's next request replaces it, so "was this link spent"
    stays answerable for as long as having asked it could matter.
    """

    __tablename__ = "password_reset_tokens"
    __table_args__ = (
        Index("ix_password_reset_tokens_token_hash", "token_hash", unique=True),
        Index("ix_password_reset_tokens_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime] = mapped_column()
    used_at: Mapped[datetime | None] = mapped_column(default=None)

    def __repr__(self) -> str:
        return f"<PasswordResetToken expires={self.expires_at.isoformat()}>"


class EmailSettings(Base):
    """The deployment's outbound mail server, as the console set it (ADR 0051).

    A single row by convention — there is one mail server. Empty (no row, or a
    row with an empty host) means the environment's SMTP values stand (ADR
    0049's fallback), which is what keeps an upgrade silent. The password is
    encrypted at rest with the same box as upstream credentials (ADR 0027) and
    is never rendered back to a browser.
    """

    __tablename__ = "email_settings"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, default=1)
    smtp_host: Mapped[str] = mapped_column(String(255), default="")
    smtp_port: Mapped[int] = mapped_column(sa.Integer, default=587)
    smtp_username: Mapped[str] = mapped_column(String(255), default="")
    smtp_password_encrypted: Mapped[str | None] = mapped_column(Text, default=None)
    smtp_from: Mapped[str] = mapped_column(String(255), default="")
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    # Nullable for the recorded reason of every table here that keeps a
    # decision: erasing a user must not erase the fact they made one.
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    def __repr__(self) -> str:
        return f"<EmailSettings host={self.smtp_host!r}>"


class IdentityProvider(Base):
    """One configured identity provider (ADR 0051).

    Rows are seeded from the environment at startup when the table is empty,
    and afterwards belong to the console. Users are keyed on ``(issuer,
    subject)``, so two providers are two namespaces of accounts by design — a
    person with a row in each is two users, which is the honest answer rather
    than a guess about which directory entry is really them.

    The client secret is encrypted at rest (ADR 0027): it is the credential
    that mints identities. Group claim, userinfo toggle and the IdP→local
    mappings live here because with more than one IdP each directory names its
    groups differently — they were per-provider facts all along.
    """

    __tablename__ = "identity_providers"
    __table_args__ = (
        Index("ix_identity_providers_name", "name", unique=True),
        Index("ix_identity_providers_issuer", "issuer", unique=True),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(64))
    issuer: Mapped[str] = mapped_column(String(512))
    client_id: Mapped[str] = mapped_column(String(255))
    client_secret_encrypted: Mapped[str] = mapped_column(Text)
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    groups_claim: Mapped[str] = mapped_column(String(255), default="groups")
    fetch_userinfo: Mapped[bool] = mapped_column(Boolean, default=True)
    group_mappings: Mapped[list[list[str]]] = mapped_column(JSON, default=list)
    # May a login here adopt a local account with the same verified address
    # (ADR 0056)? Per provider and off by default, because it is this
    # directory's word that gets to name an existing account: an operator
    # trusts the corporate IdP their local accounts were named after, and
    # says nothing about the next one added.
    link_local_by_email: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    # How far this directory's answer about groups reaches (ADR 0057).
    group_sync: Mapped[GroupSync] = mapped_column(
        _enum(GroupSync, "group_sync"),
        default=GroupSync.EVERY_LOGIN,
        server_default=text("'every_login'"),
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    def __repr__(self) -> str:
        return f"<IdentityProvider name={self.name!r} issuer={self.issuer!r}>"


class FXRate(Base):
    """The last known exchange rate for one currency pair (ADR 0054).

    One row per pair, updated daily. The table exists so the **fallback** —
    "use the last known rate when the API does not answer" — survives restarts
    and cache flushes; a rate that lives only in a rebuildable cache is a rate
    the deployment does not actually have.
    """

    __tablename__ = "fx_rates"
    __table_args__ = (Index("ix_fx_rates_base_quote", "base", "quote", unique=True),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    base: Mapped[str] = mapped_column(String(3))
    quote: Mapped[str] = mapped_column(String(3))
    rate: Mapped[Decimal] = mapped_column(Numeric(18, 10))
    fetched_at: Mapped[datetime] = mapped_column()

    def __repr__(self) -> str:
        return f"<FXRate {self.base}->{self.quote} {self.rate}>"


class QuotaNotificationSetting(Base):
    """A user's decision to be told when a quota reaches a percentage (ADR 0052).

    The rule is the administrator's; the threshold is the user's. Both must
    still hold: the API validates that the rule actually applies to the user
    who is subscribing, because a threshold on a quota that never constrains
    you is a mail subscription to somebody else's budget.
    """

    __tablename__ = "quota_notification_settings"
    __table_args__ = (
        sa.CheckConstraint("threshold >= 1 AND threshold <= 100", name="ck_quota_threshold_range"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    rule_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("limit_rules.id", ondelete="CASCADE"), primary_key=True
    )
    threshold: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class QuotaNotificationState(Base):
    """Where the announcements for one (user, rule) have got to.

    ``notified_pct`` is the highest usage percentage already announced. A
    threshold fires when the live percentage reaches it **above** this mark,
    and the mark is pulled back down whenever usage falls below it — a reset
    or the natural decay of a rolling window re-arms the threshold. Without
    the re-arm, one crossing in March would buy silence forever.
    """

    __tablename__ = "quota_notification_state"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    rule_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("limit_rules.id", ondelete="CASCADE"), primary_key=True
    )
    notified_pct: Mapped[int] = mapped_column(sa.Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class OIDCPolicyConfig(Base):
    """The identity policy, as an administrator set it from the console.

    See ADR 0048.

    **Append-only, newest row wins**, modelled on ``redaction_config`` (ADR
    0033) and for the same reason: whether a stranger can become a user by
    signing in is a governance decision, and "who opened that door, when, and
    why" is a question that outlives the row that changed it.

    **Every policy column is nullable, and null means "this row does not
    decide".** The environment's value stands per field, so a row can turn one
    knob without restating the deployment's other answers, and a deployment
    that never touches the console behaves exactly as it did before this table
    existed.

    What is deliberately *not* here: the issuer, the client secret, the
    redirect URI. Those are connection plumbing read once at startup
    (discovery is fetched once by design); making them hot would put the
    identity provider's reachability on the request path. The policy is what
    an operator actually changes.
    """

    __tablename__ = "oidc_config"
    __table_args__ = (Index("ix_oidc_config_created", "created_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    #: Whether a first-time sign-in may create a user row. Null = env decides.
    auto_provision: Mapped[bool | None] = mapped_column(Boolean, default=None)
    #: What a first-time sign-in does when provisioning is off: "refuse" the
    #: stranger, or create the account inactive for an administrator to enable.
    #: Only meaningful while ``auto_provision`` is false in force.
    unknown_user_policy: Mapped[str | None] = mapped_column(String(16), default=None)
    #: Which claim names the person's groups — every IdP puts them somewhere
    #: different (Keycloak "realm_access.roles", Entra "groups", dot-paths
    #: resolved like the env setting's). Null = env decides.
    groups_claim: Mapped[str | None] = mapped_column(String(255), default=None)
    #: Which local group names confer ``is_admin``. Compared against *mapped*
    #: names — what a group is called here, not what the IdP calls it.
    admin_groups: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    #: [[idp_name, local_name], ...]: what an IdP group means here. Unmapped
    #: groups keep their own name. Many IdP groups may map to one local group.
    group_mappings: Mapped[list[list[str]] | None] = mapped_column(JSON, default=None)
    #: Why. Optional: unlike the redaction engine, no single change here is the
    #: "protects less" case that demands a sentence — refusing strangers is the
    #: cautious direction, and opening the door is at least a deliberate act
    #: this table records.
    reason: Mapped[str] = mapped_column(String(500), default="")
    # Nullable for the same reason as on `redaction_config`: erasing a user
    # must not delete the record of what they changed.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    def __repr__(self) -> str:
        return f"<OIDCPolicyConfig at={self.created_at.isoformat()}>"


class RedactionConfig(Base):
    """Which redaction engine this deployment is running, as an admin set it.

    See ADR 0033.

    **Append-only, and the newest row wins.** Never updated, never deleted:
    switching redaction off is a decision about whether personal data leaves this
    deployment, and "who turned it off, when, and why" is a question a
    data-protection review asks about a window that has already closed. A mutable
    row answers it only for the most recent change, which is the one nobody needs
    to ask about.

    Modelled on ``quota_resets`` — the other table here that records a deliberate
    administrative act rather than a state — and on ``model_prices``, whose rows
    are append-only so a past request keeps the price it was billed at. Same
    argument, different stake.

    **No row means the environment decides.** A deployment that never touches the
    console behaves exactly as it did before this table existed, which is what
    keeps the upgrade silent. The API reports which of the two is in force,
    because "the console says http and the environment says noop" is otherwise
    invisible and is precisely the confusion this table introduces.

    What is deliberately *not* here: the endpoint, the placeholder key, the
    detection parameters. Those stay in the environment for now — the key
    especially, because it is a secret and because rotating it re-labels every
    transcript it ever labelled. Enabling an engine that needs an endpoint is
    refused when the environment has not provided one, rather than saved into a
    configuration that cannot run.
    """

    __tablename__ = "redaction_config"
    __table_args__ = (Index("ix_redaction_config_created", "created_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    #: A registry name, not an enum: engines register through the
    #: `llmp.redactors` entry point and their names cannot be enumerated in a
    #: schema (ADR 0026). Validated against the registry before the row is
    #: written, which is stricter than an enum would be — it also proves the
    #: engine is *installed*, not merely spelled correctly.
    engine: Mapped[str] = mapped_column(String(64))
    #: Why. Required by the API when switching to an engine that redacts nothing,
    #: optional otherwise: turning the layer off is the change that needs
    #: explaining, and demanding a sentence for every change trains people to
    #: type "x".
    reason: Mapped[str] = mapped_column(String(500), default="")
    # Nullable for the same reason as on `quota_resets`: erasing a user under
    # GDPR must not delete the record of what they changed.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    def __repr__(self) -> str:
        return f"<RedactionConfig engine={self.engine} at={self.created_at.isoformat()}>"


class RedactionRule(Base):
    """A redaction policy attached to one provider, model, group, user or key.

    See ADR 0038. The shape is
    ``limit_rules`` deliberately — an operator already understands it and the
    console already renders it — with three differences worth naming:

    * it carries a whole ``RedactionPolicy`` as JSON rather than a boolean,
      because since ADR 0037 "redact or not" is not a question anyone asks; the
      question is per entity type;
    * there is no time axis, so no metric, window or period;
    * there is **no global scope**, because ``redaction_config`` is it.

    Rules are combined by ``RedactionPolicy.combine``, which takes the strictest
    answer for every type. That is why nothing here needs a priority column, and
    why a rule saved wrongly is inert rather than dangerous: a scope can only
    tighten, by construction rather than by validation.

    Mutable, unlike ``redaction_config``. The history a data-protection review
    asks about is the history of *requests*, and ``usage_records`` stamps the
    scope and the rule id onto every one — which answers it even after the rule
    has been edited, and without a second table to keep in step.
    """

    __tablename__ = "redaction_rules"
    __table_args__ = (
        # Same COALESCE trick as limit_rules, for the same reason: scope_id is
        # nullable nowhere here, but the partial-unique-on-active shape needs an
        # expression index anyway, and matching the neighbouring table keeps one
        # idiom rather than two.
        # COALESCE for the same reason limit_rules does it: SQL treats NULLs as
        # distinct, so a plain UNIQUE would happily accept two catch-all rules
        # and then pick between them at request time.
        Index(
            "uq_redaction_rules_identity",
            text("scope"),
            text("coalesce(scope_id, '00000000-0000-0000-0000-000000000000')"),
            unique=True,
        ),
        CheckConstraint(
            "(scope = 'all' AND scope_id IS NULL) OR (scope <> 'all' AND scope_id IS NOT NULL)",
            name="ck_redaction_rules_scope_id_presence",
        ),
        Index("ix_redaction_rules_lookup", "is_active", "scope", "scope_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), default="")
    scope: Mapped[RedactionScope] = mapped_column(_enum(RedactionScope, "redaction_scope"))
    #: Not a foreign key: it points at one of five tables, exactly as
    #: ``limit_rules.scope_id`` does. The scope says which — and it is null for
    #: exactly one scope, ``all``, whose subject is every request.
    scope_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    #: A whole ``RedactionPolicy``, in the shape ``redaction_config.policy``
    #: uses, so one editor, one validator and one combiner serve both.
    policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(default=True)
    #: Optional, unlike the engine switch's. Every scoped rule can only tighten,
    #: so there is no direction here that needs explaining — and asking for a
    #: sentence per checkbox is how a reason field fills up with "x".
    reason: Mapped[str] = mapped_column(String(500), default="")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    def __repr__(self) -> str:
        return f"<RedactionRule {self.scope}:{self.scope_id} active={self.is_active}>"


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
