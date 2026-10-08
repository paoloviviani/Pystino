"""Typed configuration, loaded from the environment.

Every knob the brief calls "configurable" is here, and nothing reads os.environ
directly anywhere else. Env vars are prefixed ``GATEWAY_`` and nested groups use
a double underscore, e.g. ``GATEWAY_OIDC__ISSUER``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

if TYPE_CHECKING:
    # Annotation-only: `from __future__ import annotations` means the dataclass
    # field below never evaluates it at runtime.
    import uuid


def _split_csv(value: str) -> list[str]:
    """A comma-separated `.env` value as a list (ADR 0093 §1).

    Not a `list[str]`-typed field with pydantic-settings' own env parsing:
    that decodes a list env var as JSON, so a plain `a,b,c` fails to parse
    rather than splitting — see `secret_key_list` below, which predates this
    and solves it the same way, string field plus a reader.
    """
    return [item.strip() for item in value.split(",") if item.strip()]


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
    # See ADR 0013
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
    """OIDC for the management API, and — when an audience is named — for ``/v1``.

    ``/v1`` authenticates with API keys by default. Setting
    ``access_token_audience`` additionally accepts OIDC **access** tokens there,
    which is what lets a first-party application (the chat app, the ``opencode``
    device flow) call the gateway as the human using it without minting and
    storing a key per user. See ADR 0040.
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
    # means "import every group the IdP reports". Comma-separated or a JSON
    # list: `NoDecode` because pydantic-settings would otherwise insist on
    # JSON, and refuse even an empty value, which a compose passthrough of an
    # unset variable produces.
    group_allowlist: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # What a group name the directory reports, and no group here carries, does
    # at a login. `manual` records it as *seen* (the console's Groups page
    # lists it, with Import and Dismiss) and creates nothing; `auto` creates
    # the group on first sight, which is what this gateway always did. Groups
    # that already exist resolve in both modes, so an upgrade that lands on
    # `manual` keeps every membership it was granting; only *new* names stop
    # becoming groups. Manual is the default because a real directory reports
    # every group a person is in: one GitLab sign-in created 67 groups.
    group_import: Literal["manual", "auto"] = "manual"
    # The older spelling of the same switch, still honoured: a deployment that
    # set it keeps its behaviour (`true` is `auto`, `false` is `manual`).
    # Unset (None) leaves `group_import` to decide.
    auto_create_groups: bool | None = None
    # The group every person joins at their first sign-in here (and every
    # existing person at their next one): what makes a brand-new account
    # usable, with a group to bill, when nothing its directory says has been
    # imported. It grants no model by itself — models are granted per group
    # or public. The bundled Authelia's `users` group is this one by default.
    # Empty turns it off.
    default_group: str = "users"

    @field_validator("group_allowlist", mode="before")
    @classmethod
    def _allowlist_from_text(cls, value: object) -> object:
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                return json.loads(text)
            return _split_csv(text)
        return value

    @model_validator(mode="after")
    def _group_import_from_legacy(self) -> OIDCSettings:
        if self.auto_create_groups is None:
            return self
        legacy: Literal["manual", "auto"] = "auto" if self.auto_create_groups else "manual"
        if "group_import" in self.model_fields_set and self.group_import != legacy:
            # Two settings saying opposite things is a mistake to surface, not
            # a precedence rule for the operator to guess.
            raise ValueError(
                f"GATEWAY_OIDC__AUTO_CREATE_GROUPS={str(self.auto_create_groups).lower()} "
                f"contradicts GATEWAY_OIDC__GROUP_IMPORT={self.group_import}: delete "
                "AUTO_CREATE_GROUPS, the older spelling, and keep GROUP_IMPORT."
            )
        self.group_import = legacy
        return self

    # Off by default, and turning it on is always an explicit operator act —
    # never a consequence of upgrading (ADR 0056): a deployment that had no
    # such variable keeps two accounts, exactly as before. Set, a directory
    # login adopts the local account sharing its *verified* email rather than
    # provisioning a second row. This is the shape a bundled-IdP install uses so
    # the operator's one local admin (`gateway passwd <email> --admin`) and
    # their SSO login are a single account, with the local password left as the
    # break-glass door. Mirrored to the seeded provider row and the pre-seed
    # fallback in identity_registry; the console's per-provider switch is the
    # same fact for a deployment that configures providers there instead.
    link_local_by_email: bool = False

    @field_validator("link_local_by_email")
    @classmethod
    def _link_local_by_email_was_removed(cls, value: bool) -> bool:
        # `false` is the untouched default and is ignored, not refused: an
        # upgrade that never used it must not start failing over a variable it
        # never set. `true` is the only value with anything to say, and what it
        # said was "adopt a local password account by email", a door that has
        # not existed since ADR 0088 D3 (ADR 0093 §1).
        if value:
            raise ValueError(
                "GATEWAY_OIDC__LINK_LOCAL_BY_EMAIL was removed (ADR 0093): linking is "
                "GATEWAY_OIDC__LINK_BY_EMAIL now, which links across issuers by verified "
                "email rather than adopting a local password account. Delete the "
                "variable and set GATEWAY_OIDC__LINK_BY_EMAIL instead."
            )
        return value

    # Where this server reaches the issuer, when that is not the issuer URL
    # itself — the bundled Authelia at http://authelia:9091/authelia. Discovery,
    # token, JWKS and userinfo go there, carrying X-Forwarded-Proto/Host for the
    # public issuer so the IdP still mints tokens whose `iss` is public; the
    # browser keeps the public authorization and logout endpoints. Without it,
    # server-to-server calls hairpin through the proxy's public TLS listener,
    # which is what the CA trust bundle existed to paper over. Empty means
    # "the issuer URL is reachable from here", the external-IdP case.
    internal_base_url: str = ""

    # What kind of directory the environment's provider is (ADR 0088):
    # `authelia` for the bundled one, which OIDC_KIND in .env writes (ADR
    # 0091: deploy/.env.example, or the deploy kit's ./configure). It only
    # matters when the first provider row is seeded from here — that row then
    # gets the users-file sync adapter and the console's user management.
    kind: str = "generic"

    # Removed as a setting by ADR 0069 and kept only as a tripwire: the field
    # exists so that a deployment still setting GATEWAY_OIDC__ADMIN_GROUPS gets
    # a startup error naming the removal instead of a silent no-op. An
    # administrator is made in the console or by `gateway passwd --admin`,
    # never by a group claim — the directory authenticates and never
    # administers.
    admin_groups: list[str] = Field(default_factory=list)

    @field_validator("admin_groups")
    @classmethod
    def _admin_groups_was_removed(cls, value: list[str]) -> list[str]:
        if value:
            raise ValueError(
                "GATEWAY_OIDC__ADMIN_GROUPS was removed (ADR 0069): authorisation is a "
                "gateway fact. Make an administrator in the console or with "
                "`pystino admin grant <email>`, and delete the variable."
            )
        return value

    # Naming an audience is what enables OIDC access tokens on `/v1`; empty means
    # API keys only, which is the behaviour every deployment had before this
    # existed. One knob rather than two, because an `enabled` flag without an
    # audience would accept any token the issuer ever issued to anybody — the
    # audience *is* the security property, so it is also the switch.
    #
    # Verified against a Keycloak deployment on 2026-08-28 and not guessed:
    # a Keycloak access token carries **no `aud` claim at all** unless an
    # audience mapper puts one there, only `azp` naming the client that asked
    # for it. So the value here must match a mapper configured on every client
    # permitted to call `/v1` — see docs/oidc-generic-provider.md for the
    # per-provider notes. Matching `azp` instead was rejected: `azp` says who
    # requested the token, not who it is *for*, so a token minted for any other
    # purpose by a permitted client would be accepted at the API.
    access_token_audience: str = ""

    # Clock skew tolerance when validating ID token exp/iat/nbf.
    leeway_seconds: int = 60
    # JWKS documents are cached for this long.
    jwks_cache_seconds: int = 3600

    @field_validator("issuer")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, value: str) -> str:
        # identity_policy.KINDS, spelled here: importing it would be a cycle.
        kinds = ("generic", "authelia", "keycloak", "entra", "okta", "authentik", "google")
        value = value.strip().lower() or "generic"
        if value not in kinds:
            raise ValueError(f"GATEWAY_OIDC__KIND must be one of {', '.join(kinds)}")
        return value

    # Overrides the provider's own end_session_endpoint for signing out (ADR
    # 0093 §1). This was a row-only field (`IdentityProvider.logout_url`,
    # migration 0046); `reseed_from_env` writes it onto the row from here, once
    # the re-seed lands. Authelia 4.39 publishes no end_session_endpoint at
    # all, which used to leave its SSO session alive after a sign-out.
    logout_url: str = ""

    # ADR 0093 §5.1: grants admin at every sign-in to whichever of these
    # addresses the IdP verifies (the literal boolean `true`, not a truthy
    # string). Comma-separated, because a `list[str]` field cannot be set from
    # a plain `.env` string — pydantic-settings decodes a list env var as JSON,
    # and a bare CSV string is not JSON. `admin_email_list()` is what reads it.
    admin_emails: str = ""

    # ADR 0093 §5.2: the existing claim-rule shape (`AdminRule`), fed from here
    # instead of a provider row once `apply_env_admin_rules` lands. A dotted
    # path, as for `groups_claim` (e.g. `groups`, `realm_access.roles`).
    admin_claim: str = ""
    #: Comma-separated; any one value matches. See `admin_emails` for why this
    #: is a string and not a `list[str]`.
    admin_claim_values: str = ""

    # ADR 0093 §6: off by default, and turning it on is always an explicit
    # operator act, the same reasoning `link_local_by_email` was given — a
    # first sign-in from an unknown identity attaches to the existing account
    # with the same *verified* email instead of provisioning a second one.
    # Read and warned about only in this stage; the linking rule itself, at
    # the two sign-in doors, is stage (c).
    link_by_email: bool = False

    # ADR 0093 §1: how far the directory's answer about groups reaches, fed
    # from here once the re-seed lands (`GroupSync` in `gateway.models` is the
    # same three values, spelled out again here for the reason `_known_kind`
    # gives for `kind`: importing it would be a cycle).
    group_sync: Literal["every_login", "first_login", "never"] = "every_login"

    # ADR 0093 §2: a bearer token carrying `azp` (or `client_id`) must name one
    # of these to be accepted — the console, the chat and galopin's machine
    # client. Compose fills this from `OIDC_CONSOLE_CLIENT_ID`,
    # `OIDC_CHAT_CLIENT_ID` and `OIDC_MACHINE_CLIENT_ID`; see `accepted_client_list`
    # for why this is a string. Enforced on `/v1` once the re-seed lands.
    accepted_clients: str = ""

    # ADR 0093 §4.1: which of `accepted_clients` is the chat, specifically.
    # `POST /v1/session/announce` is the chat's own sign-in door and refuses a
    # token whose `azp` (or `client_id`) names anything else — the console or
    # galopin's machine client are accepted *callers of `/v1`*, but neither is
    # entitled to run the sign-in sequence a login triggers. Compose fills
    # this from `OIDC_CHAT_CLIENT_ID`, the same variable that feeds this
    # gateway's slot in `accepted_clients`.
    chat_client_id: str = ""
    # The chat client's secret, for one purpose only: introspecting (RFC 7662)
    # the chat's access tokens at a provider whose tokens are opaque rather
    # than JWTs (GitLab). Doorkeeper answers `active: true` only to the client a
    # token was issued to, so the console's credentials cannot vouch for a
    # chat token. Empty everywhere the chat's tokens are JWTs. Compose fills
    # it from `OIDC_CHAT_CLIENT_SECRET`, the secret the chat itself signs in
    # with.
    chat_client_secret: SecretStr = SecretStr("")

    def admin_email_list(self) -> list[str]:
        return _split_csv(self.admin_emails)

    def admin_claim_value_list(self) -> list[str]:
        return _split_csv(self.admin_claim_values)

    def accepted_client_list(self) -> list[str]:
        return _split_csv(self.accepted_clients)


class SmtpSettings(BaseModel):
    """The stack's one mail setting (ADR 0093 §1, review correction 8).

    Was ``local_auth.password_reset`` (ADR 0049), nested there because mail
    arrived with the local door's password reset. That door is long gone
    (ADR 0088 D3), but mail outlived it: quota notifications and the
    console's "send test" both read this, and (through the deploy kit, a
    different repo) Authelia's own notifier feeds its self-service reset
    from the same values. None of that is "password reset" any more, so it
    moved to the top level and took a name that says what it actually is.

    ``security`` is new: ``starttls`` (the only thing ``mail.py`` used to
    do — connect plain, then upgrade if the server offers it), ``tls``
    (connect already encrypted, port 465 by convention), or ``none`` (never
    encrypt, for a mail sink with no certificate on a private network).
    """

    enabled: bool = False
    host: str = ""
    port: int = Field(default=587, gt=0)
    username: str = ""
    password: SecretStr = SecretStr("")
    from_address: str = ""
    security: Literal["starttls", "tls", "none"] = "starttls"


class ChatSettings(BaseModel):
    """The gateway's own call to the chat, for erasure only (ADR 0093 §9.3).

    ``erasure_token`` is a service credential minted once by
    the deploy kit's ``./configure`` (Cerea's ``kit/``), used for nothing else, and never
    reaches the edge (the Caddyfile answers 404 for the path first; the chat
    refuses it again itself if a proxy header is present regardless). Empty
    ``erasure_url`` means this deployment has no chat the gateway can reach —
    a Pystino-only install, or a satellite without its own — and delete
    refuses outright rather than queuing a call that can never land.
    """

    erasure_url: str = ""
    erasure_token: SecretStr = SecretStr("")


class _RemovedPasswordResetSettings(BaseModel):
    """``GATEWAY_LOCAL_AUTH__PASSWORD_RESET__*`` — removed (ADR 0093 §1).

    A tripwire, the same shape as `LocalAuthSettings.enabled`: the fields
    exist only so that a deployment still setting one of these old variables
    gets a startup error naming `SmtpSettings` instead of configuring mail
    that nothing reads any more.
    """

    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = Field(default=587, gt=0)
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from: str = ""

    @model_validator(mode="after")
    def _was_removed(self) -> _RemovedPasswordResetSettings:
        if (
            self.enabled
            or self.smtp_host
            or self.smtp_username
            or self.smtp_password.get_secret_value()
            or self.smtp_from
            or self.smtp_port != 587
        ):
            raise ValueError(
                "GATEWAY_LOCAL_AUTH__PASSWORD_RESET__* was removed (ADR 0093): mail "
                "settings are GATEWAY_SMTP__* now, and feed quota notifications, the "
                "console's \"send test\" and (through the deploy kit) Authelia's own "
                "reset flow. Delete the variable and set GATEWAY_SMTP__HOST etc. "
                "instead (the deploy kit, Cerea's kit/: ./configure --smtp-host ...)."
            )
        return self


class LocalAuthSettings(BaseModel):
    """Local email + password sign-in — removed (ADR 0088, decision D3).

    Every person signs in through an OpenID Connect provider; the break-glass
    for a deployment with no administrator is `pystino admin grant <email>`
    inside the gateway container. Kept only as a tripwire, per ADR 0065's
    rule: a deployment still asking for the password door gets a startup error
    naming the change, not a silent absence.
    """

    enabled: bool = False
    password_reset: _RemovedPasswordResetSettings = Field(
        default_factory=_RemovedPasswordResetSettings
    )

    @field_validator("enabled")
    @classmethod
    def _local_auth_was_removed(cls, value: bool) -> bool:
        if value:
            raise ValueError(
                "GATEWAY_LOCAL_AUTH__ENABLED was removed (ADR 0088): people sign in through "
                "an OIDC provider, and the break-glass is `pystino admin grant <email>`. "
                "Delete the variable."
            )
        return value


class IdPSettings(BaseModel):
    """The house identity provider — removed (ADR 0088; it superseded ADR 0068).

    The bundled Authelia (COMPOSE_PROFILES=authelia in deploy/.env.example, or
    the deploy kit's `./configure --idp authelia`) or any OIDC provider replaces
    it. A tripwire, like LocalAuthSettings: GATEWAY_IDP__ENABLED=true refuses
    to start with a message saying what to do instead.
    """

    enabled: bool = False

    @field_validator("enabled")
    @classmethod
    def _idp_was_removed(cls, value: bool) -> bool:
        if value:
            raise ValueError(
                "GATEWAY_IDP__ENABLED was removed (ADR 0088): the house identity provider is "
                "gone. Use the bundled Authelia (deploy/.env.example: COMPOSE_PROFILES=authelia) "
                "or any OIDC provider, and delete the GATEWAY_IDP__* variables."
            )
        return value


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
    #: ``<PERSON>``, with no derived token. Lossiest of the substitutions: two
    #: people in one prompt become the same label, so the model cannot tell them
    #: apart.
    REDACT = "redact"
    #: The request is refused before it reaches a provider. Last in the order, so
    #: it composes with the fold in :meth:`RedactionPolicy.combine` for free.
    #: For values whose presence is itself the incident — a pasted API key —
    #: where silently replacing it tells nobody it happened.
    BLOCK = "block"


class EntityPolicy(BaseModel):
    """How one entity type is treated, and how sure the detector must be."""

    mode: EntityMode = EntityMode.ANONYMISE_RESTORE
    #: Overrides ``score_threshold`` for this type alone. ``PERSON`` at 0.85 and
    #: ``URL`` at 0.5 are not the same judgement, and one global number forces
    #: them to be. None means the global value applies.
    threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class CustomPattern(BaseModel):
    """A regex an operator wrote, treated as one more entity type.

    ``name`` becomes the entity label, so a match is substituted, restored and
    counted by exactly the machinery every other type uses — and shows up in the
    ledger's entity count without a second concept.

    Evaluated in the gateway rather than sent to the detection service: no
    contract change, works for any engine, and it keeps one deployment's regexes
    out of a service every deployment shares. Also outside the detection cache,
    since a regex is cheap and deterministic and would otherwise fragment the key.
    """

    name: str = Field(min_length=1, max_length=64)
    regex: str = Field(min_length=1, max_length=512)
    #: Defaults to the strongest substitution rather than to the gentlest: a
    #: pattern somebody wrote by hand is a value they went out of their way to
    #: name, and the gateway should not guess that they wanted it back.
    mode: EntityMode = EntityMode.REDACT

    @field_validator("regex")
    @classmethod
    def _compilable(cls, value: str) -> str:
        """Refuse at save time what would otherwise fail per request.

        RE2 rather than :mod:`re`, and the reason is measured:
        ``re.search(r"(a+)+$", "a"*26 + "!")`` takes **10.8 seconds** on Python's
        engine, which has no timeout, so one pattern of that shape written by
        accident hangs a worker. RE2 cannot backtrack — the same call returns in
        microseconds — so the hang is structurally impossible rather than
        merely unlikely (ADR 0038).

        What that costs, and it is worth knowing before writing a pattern:
        RE2 has no backreferences and no lookaround. Its own parser message is
        passed through, because "invalid escape sequence: \\1" tells an author
        exactly which construct it refused.
        """
        import re2

        try:
            compiled = re2.compile(value)
        except Exception as exc:  # re2 raises its own error type
            raise ValueError(f"this pattern cannot be used: {exc}") from exc
        # A pattern matching the empty string matches at every position, so it
        # would replace the whole text with placeholders. Refused here rather
        # than discovered on the first prompt.
        if compiled.search("") is not None:
            raise ValueError("this pattern matches the empty string, so it would match everywhere")
        return value

    @field_validator("name")
    @classmethod
    def _label(cls, value: str) -> str:
        # Same alphabet the placeholder scheme coerces to, checked here so the
        # console refuses it rather than the substitution silently renaming it.
        cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_").upper()
        if not cleaned:
            raise ValueError("a pattern name needs at least one letter or digit")
        return cleaned


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

    #: What happens to a type this policy does not name. **Off**, so a policy
    #: is a statement about the types it lists and silent about the rest. Any
    #: other default would mean a rule about `IBAN_CODE` quietly deciding for
    #: `PERSON` too, and — folded across scopes by `max` — a narrow rule could
    #: raise the floor for everything.
    default_mode: EntityMode = EntityMode.OFF
    entities: dict[str, EntityPolicy] = Field(default_factory=dict)
    #: Operator-written regexes, unioned across scopes when policies combine:
    #: adding a pattern can only find more, so union is the tightening direction.
    patterns: list[CustomPattern] = Field(default_factory=list)
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
        name = entity_type.upper()
        entry = self.entities.get(name)
        if entry is not None:
            return entry.mode
        # A custom pattern is one more entity type, and its own mode is where it
        # is written. Checked after `entities` so an admin can still override a
        # pattern's mode by naming it there — the narrower statement wins, which
        # is the same rule the rest of this file follows.
        for pattern in self.patterns:
            if pattern.name == name:
                return pattern.mode
        return self.default_mode

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

    @classmethod
    def combine(cls, policies: Sequence[RedactionPolicy]) -> RedactionPolicy:
        """Fold an applicable set into the one policy that governs a request.

        The whole safety property of scoping is in the choice of combiners, and
        it is deliberately not a validation rule: **adding a scope can only
        tighten, by construction**. A rule saved wrong is inert rather than
        dangerous, which is the difference between a bug and an incident.

        * **mode** is ``max`` by rank **over the policies that name the type**.
          A policy contributes where it speaks; its ``default_mode`` applies only
          to types *nobody* names. That distinction was found by a test: with the
          default falling through, a group rule about `IBAN_CODE` alone silently
          switched `URL` back on, because the rule's own default outranked the
          deployment's deliberate "off" — reintroducing the exact bug ADR 0037
          exists to fix. An operator who writes a rule about one type has said
          nothing about the others, and the fold now reads it that way.
        * **threshold** is ``min`` over the policies that act on that type. A
          lower threshold catches more spans, so ``min`` is the strict
          direction — and it stops a group rule setting ``PERSON`` to 0.95,
          hiding names the deployment wanted caught at 0.5 while looking like
          it was tightening.
        * **patterns** are the union. One more regex can only find more.
        * **allow_list** is taken from the *first* policy alone, which is always
          the deployment's. It is the one field that weakens, and neither
          combiner is safe: union lets a user exempt what an admin redacts, and
          intersection silently voids the deployment's own exemptions the moment
          any scoped rule carries an empty list. So it is not a per-scope field
          (ADR 0038).
        """
        if not policies:
            return cls()
        first, *rest = policies
        if not rest:
            return first

        order = list(EntityMode)
        named = {name for policy in policies for name in policy.entities}
        entities: dict[str, EntityPolicy] = {}
        for name in named:
            # Only the policies that name it. A policy that is silent about a
            # type is silent, not voting for its own default.
            speaking = [p for p in policies if name in p.entities]
            mode = max((p.entities[name].mode for p in speaking), key=order.index)
            thresholds = [
                entry.threshold
                for policy in policies
                if (entry := policy.entities.get(name)) is not None
                and entry.threshold is not None
                and entry.mode is not EntityMode.OFF
            ]
            entities[name] = EntityPolicy(
                mode=mode, threshold=min(thresholds) if thresholds else None
            )

        patterns: list[CustomPattern] = []
        seen: set[tuple[str, str]] = set()
        for policy in policies:
            for pattern in policy.patterns:
                if (key := (pattern.name, pattern.regex)) not in seen:
                    seen.add(key)
                    patterns.append(pattern)

        return cls(
            default_mode=max((p.default_mode for p in policies), key=order.index),
            entities=entities,
            patterns=patterns,
            allow_list=list(first.allow_list),
        )

    def weakens(self, previous: RedactionPolicy, known: Collection[str] | None = None) -> bool:
        """Whether moving from *previous* to this one protects strictly less.

        The direction that has to be explained. Turning protection *on* needs no
        justification — a prompt with no reader trains people to type "x"
        (ADR 0033) — but turning it off, or downgrading a mode, is a decision a
        later review asks about.
        """
        order = list(EntityMode)
        if order.index(self.default_mode) < order.index(previous.default_mode):
            return True
        # `known` matters as much as the named ones: a default downgrade hides
        # exactly where nobody has written a rule, so a caller passes the
        # detector's own list and the comparison covers types neither policy
        # mentions.
        names = set(self.entities) | set(previous.entities) | set(known or ())
        for name in names:
            if order.index(self.mode_for(name)) < order.index(previous.mode_for(name)):
                return True
            # A raised threshold catches fewer spans, so it weakens even though
            # the mode is unchanged — the failure this whole check exists for,
            # dressed as a tightening.
            if self.threshold_for(name, 0.0) > previous.threshold_for(name, 0.0):
                return True
        # Exempting more values is the same decision by another route: an
        # allow-list entry means "never redact this", whatever the detector says.
        return not set(self.allow_list).issubset(previous.allow_list)


@dataclass(frozen=True, slots=True)
class EffectivePolicy:
    """The policy governing one request, and the rule to record against it.

    Lives here rather than beside the resolver that produces it, because the
    redactor that consumes it must not import the resolver — and a shared type in
    the module both already depend on is cheaper than a protocol.
    """

    policy: RedactionPolicy
    #: The narrowest scope whose rule contributed, or None when only the
    #: deployment policy applied.
    scope: str | None = None
    rule_id: uuid.UUID | None = None


#: What a deployment redacts before anybody has written a rule: **nothing**.
#:
#: This reverses ADR 0037, deliberately, and the reasoning is in ADR 0039. That
#: version shipped a policy that protected everything the engine found except
#: four noisy types, on the argument that the failure direction must be
#: over-protection. Two things were wrong with it. It made the deployment policy
#: a *special* object — not a rule, edited on its own screen, folded in ahead of
#: the rules — so "what applies here" had two answers with different shapes. And
#: a filter nobody chose is a filter nobody has thought about: the measured
#: outcome was ordinary Italian requests arriving at the provider with the verb
#: replaced by a placeholder.
#:
#: Redaction is now off until somebody says otherwise, and *saying otherwise* is
#: one thing — writing a rule. A deployment that wants everything protected
#: writes one rule, scoped to `all`, and can see it on the screen next to every
#: other rule.
DEFAULT_REDACTION_POLICY = RedactionPolicy()


class ExtractorSettings(BaseModel):
    """The local document extraction service (`/v1/ocr`'s local backend).

    Its own section rather than a field on the redaction settings, even though
    both are served by one image today: they are two services that happen to
    share a build, and a deployment scaling one without the other should not
    have to edit the other's address.
    """

    #: Where the service is. A provider row's own Base URL wins over this, so a
    #: deployment can run more than one extractor; this is the default the
    #: plugin's rows fall back to.
    endpoint: str = "http://extractor:8080"


class RedactionSettings(BaseModel):
    """Redaction/guardrail layer.

    Phase 1 ships ``noop``. ``http`` calls an out-of-process detection service
    (Presidio in Phase 2) — never in-process, because spaCy inference is
    CPU-bound and synchronous and would stall every concurrent stream on the
    worker. See ADR 0012
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
    # Nullable because the detector's deployment default remains authoritative
    # until an administrator makes an explicit console decision.
    presidio_pattern_matching: bool | None = None
    presidio_ner: bool | None = None
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


class AccountingSettings(BaseModel):
    """Whether this deployment keeps a ledger at all.

    Off, the gateway is a plain proxy with model discovery, access control and
    redaction, and it writes no ``usage_records``. That is a real deployment
    shape — somebody who wants routing and key management and has no interest
    in what anything cost — and pretending otherwise meant they paid for two
    writes per request to fill a table nobody would read.

    **Off is not "record zero".** No row is written, because a row saying a
    request cost nothing is indistinguishable from one where the arithmetic
    failed, and this project's whole posture on money is that a gap must be
    visible as a gap. Reports say metering is off rather than showing an empty
    table that looks like an idle week.

    The one thing it cannot coexist with is quotas — see
    :meth:`Settings._check_accounting`.
    """

    enabled: bool = True


class QuotaSettings(BaseModel):
    enabled: bool = True

    # Sliding windows are approximated with fixed buckets. The window edge is
    # accurate to one bucket, and a window is covered by at most
    # window/granularity keys, read in a single MGET. See
    # ADR 0009
    min_bucket_seconds: int = 1
    max_buckets_per_window: int = 60

    # Reserve-then-settle: what we assume a request may cost before we know.
    # Too low and concurrent requests can collectively overshoot a limit; too
    # high and users are refused while under budget.
    default_max_output_tokens: int = 4096
    # Prompt tokens are counted locally before the call, so only the completion
    # needs assuming.
    # The same question for server-side web search, which is charged per search
    # on top of tokens (ADR 0058). Used when a request asks for the tool
    # without capping it — and, on the surfaces whose tool definition has a cap
    # field, *written into* the outgoing request, so the number reserved is a
    # bound that actually holds rather than a guess that looks like one.
    default_max_web_searches: int = 5

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

    # The first administrator of an OIDC-only deployment (ADR 0088 draft).
    # The first browser sign-in whose *verified* email is this address, while
    # no active administrator exists, is made one. Inert once any administrator
    # exists, so leaving it set is harmless; PYSTINO_BOOTSTRAP_ADMIN_EMAIL in
    # .env writes it. It replaces `gateway passwd --admin`, which needed the
    # local-password door.
    bootstrap_admin_email: str = ""

    database_url: str = "postgresql+asyncpg://gateway:gateway@localhost:5432/gateway"
    database_pool_size: int = 10
    database_max_overflow: int = 20
    database_echo: bool = False

    # Valkey (BSD-3, LF). Wire-compatible with Redis, so the redis-py client and
    # the redis:// scheme are still what you use. ADR 0006.
    valkey_url: str = "redis://localhost:6379/0"

    # Calendar boundaries for billing periods and calendar quotas are computed in
    # this timezone, not UTC. An organisation's January starts at midnight locally, and
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
    # Other applications' session cookies on this same origin that signing out
    # here also expires — the chat's, on the Pystino stack — so that signing
    # out of one never leaves the other signed in (`name` or `name:path`).
    logout_also_clear_cookies: list[str] = []

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
    # How long a finished reply's text is kept on its usage row. The text is
    # there so a suspended client can recover an answer, which is a matter of
    # minutes; the row itself (tokens, cost) is kept regardless. 0 keeps the
    # text forever, which is the behaviour before this setting existed.
    transcript_retention_hours: float = Field(default=24.0, ge=0)
    transcript_sweep_seconds: float = Field(default=600.0, gt=0)

    request_id_header: str = "x-request-id"

    upstream: UpstreamSettings = Field(default_factory=UpstreamSettings)
    oidc: OIDCSettings = Field(default_factory=OIDCSettings)
    smtp: SmtpSettings = Field(default_factory=SmtpSettings)
    chat: ChatSettings = Field(default_factory=ChatSettings)
    local_auth: LocalAuthSettings = Field(default_factory=LocalAuthSettings)
    idp: IdPSettings = Field(default_factory=IdPSettings)
    redaction: RedactionSettings = Field(default_factory=RedactionSettings)
    extractor: ExtractorSettings = Field(default_factory=ExtractorSettings)
    quota: QuotaSettings = Field(default_factory=QuotaSettings)
    accounting: AccountingSettings = Field(default_factory=AccountingSettings)

    @model_validator(mode="after")
    def _check_accounting(self) -> Settings:
        """Quotas without a ledger are limits that can never fire.

        The counter store is a cache, not the record: it is rebuilt from
        ``usage_records`` (which is why flushing Valkey alone does not reset a
        limit). With accounting off there is nothing to rebuild from, so every
        counter would come back at zero and every ceiling would silently pass —
        a quota that refuses nobody, which is worse than no quota at all
        because somebody configured it and believes it works.

        Refused at startup rather than warned about, for the reason ground rule
        3 exists: this is the accounting path, and a wrong answer here is a
        wrong invoice or an unenforced limit rather than a stack trace.
        """
        if self.quota.enabled and not self.accounting.enabled:
            raise ValueError(
                "quota.enabled requires accounting.enabled: quota counters are rebuilt "
                "from usage_records, so with no ledger every limit would silently "
                "never fire. Set GATEWAY_QUOTA__ENABLED=false as well to run "
                "unmetered."
            )
        return self

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

    @model_validator(mode="after")
    def _admin_claim_pair(self) -> Settings:
        """ADR 0093 §1.1, refusal 2: half a claim pair is certainly a typo."""
        has_claim = bool(self.oidc.admin_claim)
        has_values = bool(self.oidc.admin_claim_values)
        if has_claim != has_values:
            raise ValueError(
                "GATEWAY_OIDC__ADMIN_CLAIM and GATEWAY_OIDC__ADMIN_CLAIM_VALUES must be "
                "set together, or not at all: set both, or delete both."
            )
        return self

    @model_validator(mode="after")
    def _production_requires_an_admin_rule(self) -> Settings:
        """ADR 0093 §1.1, refusal 1: an external IdP with no admin rule locks
        everyone out at the first sign-in — refused here, not found at 2am.

        Gated to production for the reason `_production_requires_secrets` is:
        `OIDCSettings()`'s own defaults (`kind="generic"`, no rule set) are
        exactly what hundreds of unrelated unit tests construct, and none of
        them are a deployment.
        """
        if (
            self.environment == "production"
            and self.oidc.kind != "authelia"
            and not self.oidc.admin_email_list()
            and not (self.oidc.admin_claim and self.oidc.admin_claim_values)
        ):
            raise ValueError(
                "an external identity provider needs an admin rule: set "
                "OIDC_ADMIN_EMAIL, or OIDC_ADMIN_CLAIM with OIDC_ADMIN_CLAIM_VALUE, "
                "in .env (./configure --admin-email / --admin-claim)"
            )
        return self

    @model_validator(mode="after")
    def _production_requires_accepted_clients(self) -> Settings:
        """ADR 0093 §1.1, refusal 6: an audience with no client allowlist would
        accept a token minted for any purpose by any client that can reach the
        issuer — the `azp`/`client_id` check this backs (ADR 0093 §2) has
        nothing to check against otherwise."""
        if (
            self.environment == "production"
            and self.oidc.access_token_audience
            and not self.oidc.accepted_client_list()
        ):
            raise ValueError(
                "GATEWAY_OIDC__ACCEPTED_CLIENTS must name the clients allowed to use "
                "GATEWAY_OIDC__ACCESS_TOKEN_AUDIENCE (the console, chat and machine "
                "client ids) when an audience is set. Compose fills this from "
                "OIDC_CONSOLE_CLIENT_ID, OIDC_CHAT_CLIENT_ID and "
                "OIDC_MACHINE_CLIENT_ID; a hand-run gateway sets "
                "GATEWAY_OIDC__ACCEPTED_CLIENTS itself."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so that importing modules can call it freely. Tests clear the cache
    via the ``settings`` fixture rather than mutating the object.
    """
    return Settings()


def startup_warnings(settings: Settings) -> list[str]:
    """Advisory only (ADR 0093 §1.1, refusal 7) — logged by the caller, at
    every start; nothing here refuses to start. Kept as a pure function of
    ``settings`` rather than a model validator, so the messages are testable
    without capturing a logger, and so a `Settings()` built for a unit test
    never has to see them.
    """
    warnings: list[str] = []
    if settings.oidc.link_by_email:
        warnings.append(
            "OIDC_LINK_BY_EMAIL is on: a first sign-in with a verified email will "
            "attach to the existing non-admin account with that address. Turn it "
            "off when the transition is done."
        )
    if settings.bootstrap_admin_email and settings.oidc.kind != "authelia":
        warnings.append(
            "PYSTINO_BOOTSTRAP_ADMIN_EMAIL is set but OIDC_KIND is not authelia: the "
            "bootstrap admin is honoured only with the bundled Authelia, so this "
            "variable is ignored. Grant admin with OIDC_ADMIN_EMAIL, OIDC_ADMIN_CLAIM, "
            "the console, or `pystino admin grant`."
        )
    if settings.oidc.access_token_audience and not settings.oidc.chat_client_id:
        warnings.append(
            "GATEWAY_OIDC__ACCESS_TOKEN_AUDIENCE is set but GATEWAY_OIDC__CHAT_CLIENT_ID "
            "is not: POST /v1/session/announce refuses every token until it is set, "
            "which means every chat login fails closed."
        )
    return warnings
