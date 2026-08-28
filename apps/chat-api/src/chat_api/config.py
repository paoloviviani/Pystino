"""Settings.

Environment-driven, with the same ``__`` nesting convention the gateway uses so
an operator configuring both is not learning two schemes.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class OIDCSettings(BaseModel):
    """This service's own OIDC client.

    Its own, not the gateway's, and that is the boundary being kept: two
    services that authenticate people are two clients in the realm, each with
    its own secret and its own redirect URI. Sharing one would make revoking
    either an all-or-nothing act.
    """

    issuer: str = ""
    client_id: str = "llm-chat"
    client_secret: SecretStr = SecretStr("")
    redirect_uri: str = ""
    scopes: list[str] = Field(default_factory=lambda: ["openid", "profile", "email"])
    leeway_seconds: int = 60
    jwks_cache_seconds: int = 3600

    @field_validator("issuer")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")


class GatewaySettings(BaseModel):
    """Where the gateway is, and nothing about how it works.

    No API key here, deliberately. This service never calls ``/v1`` as itself;
    it calls as the person whose message it is, with their access token
    (ADR 0040). A configured key would be a way to make a request nobody can be
    billed for, which is exactly the failure the token path removes.
    """

    base_url: str = "http://gateway:8000"
    # Generous, because a slow model is not an error. The gateway applies its
    # own upstream timeouts; this one only stops a socket being held forever.
    timeout_seconds: float = 600.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CHAT_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    environment: str = "dev"
    log_level: str = "INFO"
    log_json: bool = True

    database_url: str = "postgresql+asyncpg://llm:llm@postgres:5432/chat"

    # Signs the session cookie. Distinct from secret_key below, which encrypts
    # what the session *holds* — a leaked cookie signer must not also decrypt
    # the tokens in the database.
    session_secret: SecretStr = SecretStr("")
    session_ttl_seconds: int = 60 * 60 * 12
    session_cookie_secure: bool = True

    # Encrypts stored refresh tokens. Comma-separated for rotation: the first
    # encrypts, any decrypts.
    secret_key: SecretStr = SecretStr("")

    oidc: OIDCSettings = Field(default_factory=OIDCSettings)
    gateway: GatewaySettings = Field(default_factory=GatewaySettings)

    # Where the SPA is served from, when it is built into the image.
    spa_enabled: bool = True
