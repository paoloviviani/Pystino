"""Typed configuration, loaded from the environment.

Every knob the brief calls "configurable" is here, and nothing reads os.environ
directly anywhere else. Env vars are prefixed ``GATEWAY_`` and nested groups use
a double underscore, e.g. ``GATEWAY_OIDC__ISSUER``.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class UpstreamSettings(BaseModel):
    """Transport tuning for upstream calls, plus the bootstrap endpoint.

    Since ADR 0027 the endpoint and credentials of a *running* request come from
    the ``providers`` table, not from here: ``base_url`` and ``api_key`` are read
    once, by migration 0003, to create the ``default`` provider so an upgrade
    changes nothing.

    Everything else on this model is still live. Timeouts and pool sizes are
    properties of this gateway's HTTP client rather than of any provider, so they
    stay global and apply to every provider's client.
    """

    base_url: str = "https://api.cortecs.ai/v1"
    api_key: SecretStr = SecretStr("")
    # Sent to the upstream as-is. Useful for provider-specific routing headers.
    extra_headers: dict[str, str] = Field(default_factory=dict)
    # httpx's default read timeout is 5s, which silently kills long streams.
    # Read must be None; connect must stay finite so a dead provider fails fast.
    # See docs/adr/0013-upstream-http-client.md
    connect_timeout_seconds: float = 10.0
    write_timeout_seconds: float = 30.0
    pool_timeout_seconds: float = 10.0
    max_connections: int = 200
    max_keepalive_connections: int = 50

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")


class OIDCSettings(BaseModel):
    """OIDC for the *management* API only.

    Programmatic ``/v1`` traffic authenticates with API keys, never with OIDC
    tokens: an API key is revocable server-side and carries a billing group,
    which a bearer ID token does not.
    """

    enabled: bool = False
    issuer: str = ""
    client_id: str = ""
    client_secret: SecretStr = SecretStr("")
    # Absolute URL of this gateway's callback route.
    redirect_uri: str = ""
    scopes: list[str] = Field(default_factory=lambda: ["openid", "profile", "email"])

    # The group claim is configurable because every IdP puts groups somewhere
    # different: Keycloak uses "realm_access.roles", Entra ID "groups", others
    # "https://example.org/claims/groups". Dotted paths are resolved against
    # nested objects; a literal dot in a claim name can be escaped with "\.".
    groups_claim: str = "groups"
    # Some IdPs only return groups on the userinfo endpoint, not in the ID token.
    fetch_userinfo: bool = True
    # If set, only these groups are imported; everything else is ignored. Empty
    # means "import every group the IdP reports".
    group_allowlist: list[str] = Field(default_factory=list)
    # Groups are created on first sight when true. Turn off to make group
    # membership purely an admin decision.
    auto_create_groups: bool = True

    # Membership of any of these groups grants `is_admin`. Leave empty and the
    # flag is never touched by login, so it stays a manual database decision.
    # Once set, the identity provider is authoritative in both directions:
    # leaving the group removes admin, exactly as leaving a group removes the
    # ability to bill it.
    admin_groups: list[str] = Field(default_factory=list)

    # Clock skew tolerance when validating ID token exp/iat/nbf.
    leeway_seconds: int = 60
    # JWKS documents are cached for this long.
    jwks_cache_seconds: int = 3600

    @field_validator("issuer")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")


class EntityMode(StrEnum):
    """What happens to one kind of detected entity.

    Two independent questions, and collapsing them into one switch is what makes
    a redaction screen confusing: *what does the model see*, and *what does the
    reader get back*. Four combinations are useful and the fifth
    (opaque upstream, real value back) is not — an opaque label cannot be mapped
    to anything, which is the whole point of choosing it.

    Ordered weakest to strongest, which is also the order the console lists them
    and the order ``weakens`` compares by.
    """

    #: Detected and left alone. For things that are context rather than identity
    #: — a news site's domain, today's date — where redacting destroys the
    #: request and protects nobody.
    OFF = "off"
    #: Stable placeholder upstream, real value restored in the answer. The
    #: default, and the only mode where the reader never sees the machinery.
    ANONYMISE_RESTORE = "anonymise_restore"
    #: Stable placeholder upstream and in the answer. For values that must not
    #: re-enter text this deployment stores or displays, where the model still
    #: needs to tell one from another.
    ANONYMISE = "anonymise"
    #: ``<PERSON>``, with no derived token. Strongest and lossiest: two people
    #: in one prompt become the same label, so the model cannot tell them apart.
    REDACT = "redact"


class EntityPolicy(BaseModel):
    """How one entity type is treated, and how sure the detector must be."""

    mode: EntityMode = EntityMode.ANONYMISE_RESTORE
    #: Overrides ``score_threshold`` for this type alone. ``PERSON`` at 0.85 and
    #: ``URL`` at 0.5 are not the same judgement, and one global number forces
    #: them to be. None means the global value applies.
    threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class RedactionPolicy(BaseModel):
    """Per-entity redaction policy: the admin's answer to "redact *what*".

    Before this existed, ``entity_types`` was ``None`` — "everything the engine
    knows" — and nobody had chosen it; it was the default of a field never
    filled in. On a real request that meant *"Riassumi le notizie del giorno da
    ilpost.it"* reaching the provider as ``<PERSON_…> le notizie del giorno da
    <URL_…>``: the verb read as a name, and the source the user asked for read as
    identity. See ADR 0037.

    ``default_mode`` is the answer for a type nobody has listed, and it defaults
    to *on* rather than off. That direction matters: a detector that gains a new
    national-identifier recogniser next release should start protecting it
    without an admin noticing, and an entity type nobody has ruled on is exactly
    the case to be conservative about.
    """

    default_mode: EntityMode = EntityMode.ANONYMISE_RESTORE
    entities: dict[str, EntityPolicy] = Field(default_factory=dict)
    #: Values never redacted, whatever a detector says about them. Compared
    #: case-insensitively against the matched text, exactly — a substring rule
    #: would let "it" allow every Italian domain. This is the answer to a
    #: detector that is right about the shape and wrong about the meaning: a
    #: corporate domain *is* a URL, and it identifies nobody.
    allow_list: list[str] = Field(default_factory=list)

    @field_validator("entities")
    @classmethod
    def _upper(cls, value: dict[str, EntityPolicy]) -> dict[str, EntityPolicy]:
        # Entity labels are free-form strings from whatever engine is installed
        # (ADR 0026), and case is the one difference that is never meaningful.
        return {key.upper(): policy for key, policy in value.items()}

    def mode_for(self, entity_type: str) -> EntityMode:
        entry = self.entities.get(entity_type.upper())
        return entry.mode if entry is not None else self.default_mode

    def threshold_for(self, entity_type: str, default: float) -> float:
        entry = self.entities.get(entity_type.upper())
        if entry is None or entry.threshold is None:
            return default
        return entry.threshold

    def allows(self, text: str) -> bool:
        return text.strip().casefold() in self._allowed

    @property
    def _allowed(self) -> set[str]:
        return {item.strip().casefold() for item in self.allow_list if item.strip()}

    def detected_types(self) -> list[str] | None:
        """What to ask the detector for, or None for "everything it knows".

        Only a narrowing: when the default is on, the set cannot be enumerated —
        an engine's entity list is its own and may grow — so everything is
        requested and the disabled types are dropped here. When the default is
        off, the enabled set *is* enumerable, and sending it saves the detector
        the work.
        """
        if self.default_mode is not EntityMode.OFF:
            return None
        return sorted(
            name for name, entry in self.entities.items() if entry.mode is not EntityMode.OFF
        )

    def protects(self) -> set[str]:
        """Entity types this policy does something about. Used to compare two."""
        return {name for name, entry in self.entities.items() if entry.mode is not EntityMode.OFF}

    def weakens(self, previous: RedactionPolicy) -> bool:
        """Whether moving from *previous* to this one protects strictly less.

        The direction that has to be explained. Turning protection *on* needs no
        justification — a prompt with no reader trains people to type "x"
        (ADR 0033) — but turning it off, or downgrading a mode, is a decision a
        later review asks about.
        """
        if previous.default_mode is not EntityMode.OFF and self.default_mode is EntityMode.OFF:
            return True
        names = set(self.entities) | set(previous.entities)
        order = list(EntityMode)
        for name in names:
            if order.index(self.mode_for(name)) < order.index(previous.mode_for(name)):
                return True
        # Exempting more values is the same decision by another route: an
        # allow-list entry means "never redact this", whatever the detector says.
        return not set(self.allow_list).issubset(previous.allow_list)


#: What a deployment redacts when nobody has said otherwise.
#:
#: Everything the engine finds, except four types that are context rather than
#: identity. Not a curated allowlist of "real PII": a list of what to protect
#: would silently omit whatever the detector learns next, and the failure
#: direction of this file must be over-protection, never under.
#:
#: The four exclusions are the ones measured to break ordinary requests. A URL is
#: the source someone asked to be read; a date is when they asked; a location and
#: a nationality are usually the subject of the question rather than the identity
#: of the asker. Each is still *detected* — an admin can switch any of them on in
#: the console and see it take effect.
DEFAULT_REDACTION_POLICY = RedactionPolicy(
    default_mode=EntityMode.ANONYMISE_RESTORE,
    entities={
        "URL": EntityPolicy(mode=EntityMode.OFF),
        "DATE_TIME": EntityPolicy(mode=EntityMode.OFF),
        "LOCATION": EntityPolicy(mode=EntityMode.OFF),
        "NRP": EntityPolicy(mode=EntityMode.OFF),
    },
)


class RedactionSettings(BaseModel):
    """Redaction/guardrail layer.

    Phase 1 ships ``noop``. ``http`` calls an out-of-process detection service
    (Presidio in Phase 2) — never in-process, because spaCy inference is
    CPU-bound and synchronous and would stall every concurrent stream on the
    worker. See docs/adr/0012-redaction-interface.md
    """

    # Not a Literal: third-party engines register through the `llmp.redactors`
    # entry point and their names cannot be enumerated here (ADR 0026). Validated
    # at startup by the registry, which refuses an unknown name and lists what is
    # installed — rather than falling back to noop.
    engine: str = "noop"
    endpoint: str = ""
    timeout_seconds: float = 5.0

    # Keyed HMAC secret for placeholder derivation. Must be stable for as long as
    # the transcripts it labelled are kept: rotating it re-labels every entity.
    placeholder_key: SecretStr = SecretStr("")

    # When the detection service errors or times out: fail_open forwards the
    # unredacted prompt, fail-closed returns 502. Default closed, because a
    # redaction layer that silently stops redacting is worse than an outage.
    fail_open: bool = False

    language: str = "en"
    score_threshold: float = 0.5
    #: Superseded by ``policy`` and kept because deployments set it. When
    #: present it *is* the policy: exactly these types, anonymised and restored,
    #: everything else off. One rule rather than two mechanisms that can
    #: disagree — and the console shows the result either way.
    entity_types: list[str] | None = None
    #: Per-entity policy: which types are acted on, how, and how sure the
    #: detector must be (ADR 0037). Overridden by the newest ``redaction_config``
    #: row when the console has set one; this is the deployment's fallback.
    policy: RedactionPolicy = Field(
        default_factory=lambda: DEFAULT_REDACTION_POLICY.model_copy(deep=True)
    )

    # Swap placeholders back to real values in the response. See the asymmetry
    # note in llmp_shared.redaction.
    restore_in_response: bool = True

    # Detection results cached per process, keyed by a hash of the text. On turn
    # 40 the client resends all 40 messages; without this, inference re-runs over
    # the whole history every turn. Safe as a pure optimisation — detection is a
    # function of the text, and placeholders are derived afterwards. 0 disables.
    cache_size: int = 2048

    @field_validator("endpoint")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def _entity_types_become_the_policy(self) -> RedactionSettings:
        """``GATEWAY_REDACTION__ENTITY_TYPES`` still means what it always did.

        Translated into a policy rather than consulted beside one: two
        mechanisms deciding the same question is how a deployment ends up
        redacting something the screen says it does not.
        """
        if self.entity_types is not None and "policy" not in self.model_fields_set:
            self.policy = RedactionPolicy(
                default_mode=EntityMode.OFF,
                entities={name: EntityPolicy() for name in self.entity_types},
            )
        return self

    @model_validator(mode="after")
    def _check_http_engine(self) -> RedactionSettings:
        if self.engine == "http":
            if not self.endpoint:
                raise ValueError("redaction.endpoint is required when engine='http'")
            if not self.placeholder_key.get_secret_value():
                raise ValueError("redaction.placeholder_key is required when engine='http'")
        return self


class QuotaSettings(BaseModel):
    enabled: bool = True

    # Sliding windows are approximated with fixed buckets. The window edge is
    # accurate to one bucket, and a window is covered by at most
    # window/granularity keys, read in a single MGET. See
    # docs/adr/0009-quota-model.md
    min_bucket_seconds: int = 1
    max_buckets_per_window: int = 60

    # Reserve-then-settle: what we assume a request may cost before we know.
    # Too low and concurrent requests can collectively overshoot a limit; too
    # high and users are refused while under budget.
    default_max_output_tokens: int = 4096
    # Prompt tokens are counted locally before the call, so only the completion
    # needs assuming.

    # Valkey is a rebuildable cache, never the ledger. If it is unreachable we
    # fall back to exact PostgreSQL aggregates: slower, but correct, and the
    # gateway keeps serving. ADR 0006.
    fallback_to_database: bool = True
    # If both stores fail: refuse (safe for the budget) or allow (safe for
    # availability).
    fail_open: bool = False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GATEWAY_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["dev", "staging", "production"] = "dev"
    log_level: str = "INFO"
    log_json: bool = True

    database_url: str = "postgresql+asyncpg://gateway:gateway@localhost:5432/gateway"
    database_pool_size: int = 10
    database_max_overflow: int = 20
    database_echo: bool = False

    # Valkey (BSD-3, LF). Wire-compatible with Redis, so the redis-py client and
    # the redis:// scheme are still what you use. ADR 0006.
    valkey_url: str = "redis://localhost:6379/0"

    # Calendar boundaries for billing periods and calendar quotas are computed in
    # this timezone, not UTC. A foundation's January starts at midnight locally, and
    # a UTC boundary misallocates an hour of spend at each end of every month.
    # One implementation serves both, which is what makes a monthly budget and a
    # monthly report agree (ADR 0024, ADR 0025).
    billing_timezone: str = "Europe/Rome"

    # All money is denominated in this currency. Prices in any other currency are
    # rejected rather than silently converted, because a wrong exchange rate
    # produces plausible-looking wrong invoices. ADR 0008.
    billing_currency: str = "EUR"

    # Encrypts provider API keys at rest (ADR 0027). Comma-separated: the first
    # value encrypts, any of them decrypts, which is what makes rotation a
    # rolling restart rather than re-entering every provider credential.
    #
    # Deliberately separate from session_secret: rotating session signing must
    # not destroy stored provider keys.
    secret_key: SecretStr = SecretStr("")

    # Signs management session cookies.
    session_secret: SecretStr = SecretStr("")
    session_ttl_seconds: int = 8 * 3600
    session_cookie_name: str = "gw_session"
    session_cookie_secure: bool = True

    api_key_prefix: str = "gwk"

    # -- console (ADR 0023) ---------------------------------------------------
    #
    # Two independent gates, deliberately. The image may be built without the
    # console at all (`ARG INCLUDE_CONSOLE=false`), in which case there are no
    # assets to serve; and an operator may want it off on an image that has them,
    # without a rebuild. Neither gate can half-open: no assets means no route,
    # whatever the flag says.
    console_enabled: bool = True
    # Where the built SPA lives inside the image. Empty means "look next to the
    # package", which is where the Dockerfile puts it.
    console_dir: str = ""

    # How often the in-flight assistant text is flushed to the database while
    # streaming. Mobile clients get suspended mid-generation; a response that
    # only ever existed in the stream is lost. ADR 0007.
    transcript_flush_seconds: float = 2.0
    transcript_flush_chars: int = 2048
    persist_transcripts: bool = True

    request_id_header: str = "x-request-id"

    upstream: UpstreamSettings = Field(default_factory=UpstreamSettings)
    oidc: OIDCSettings = Field(default_factory=OIDCSettings)
    redaction: RedactionSettings = Field(default_factory=RedactionSettings)
    quota: QuotaSettings = Field(default_factory=QuotaSettings)

    @field_validator("billing_currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    def secret_key_list(self) -> list[str]:
        """Encryption keys, newest first. Empty when none is configured."""
        raw = self.secret_key.get_secret_value()
        return [part.strip() for part in raw.split(",") if part.strip()]

    @model_validator(mode="after")
    def _production_requires_secrets(self) -> Settings:
        if self.environment == "production":
            missing: list[str] = []
            if not self.session_secret.get_secret_value():
                missing.append("GATEWAY_SESSION_SECRET")
            # Provider credentials live in the database from ADR 0027 onwards,
            # and they cannot be stored without this.
            if not self.secret_key.get_secret_value():
                missing.append("GATEWAY_SECRET_KEY")
            if missing:
                raise ValueError(f"missing required settings in production: {', '.join(missing)}")
        return self


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so that importing modules can call it freely. Tests clear the cache
    via the ``settings`` fixture rather than mutating the object.
    """
    return Settings()
