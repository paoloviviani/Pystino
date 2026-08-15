"""Typed configuration, loaded from the environment.

Every knob the brief calls "configurable" is here, and nothing reads os.environ
directly anywhere else. Env vars are prefixed ``GATEWAY_`` and nested groups use
a double underscore, e.g. ``GATEWAY_OIDC__ISSUER``.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class UpstreamSettings(BaseModel):
    """One OpenAI-compatible upstream.

    Phase 1 ships a single configurable upstream. Multi-provider routing is
    Phase 2; the ``models`` table already carries a ``provider`` column so that
    routing can be added without a migration to the hot path.
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


class RedactionSettings(BaseModel):
    """Redaction/guardrail layer.

    Phase 1 ships ``noop``. ``http`` calls an out-of-process detection service
    (Presidio in Phase 2) — never in-process, because spaCy inference is
    CPU-bound and synchronous and would stall every concurrent stream on the
    worker. See docs/adr/0012-redaction-interface.md
    """

    engine: Literal["noop", "http"] = "noop"
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
    entity_types: list[str] | None = None

    # Swap placeholders back to real values in the response. See the asymmetry
    # note in llmp_shared.redaction.
    restore_in_response: bool = True

    @field_validator("endpoint")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

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

    # Signs management session cookies.
    session_secret: SecretStr = SecretStr("")
    session_ttl_seconds: int = 8 * 3600
    session_cookie_name: str = "gw_session"
    session_cookie_secure: bool = True

    api_key_prefix: str = "gwk"

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

    @model_validator(mode="after")
    def _production_requires_secrets(self) -> Settings:
        if self.environment == "production":
            missing: list[str] = []
            if not self.session_secret.get_secret_value():
                missing.append("GATEWAY_SESSION_SECRET")
            if not self.upstream.api_key.get_secret_value():
                missing.append("GATEWAY_UPSTREAM__API_KEY")
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
