"""Typed configuration, loaded from the environment.

Every knob the brief calls "configurable" is here, and nothing reads os.environ
directly anywhere else. Env vars are prefixed ``GATEWAY_`` and nested groups use
a double underscore, e.g. ``GATEWAY_OIDC__ISSUER``.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    # Annotation-only: `from __future__ import annotations` means the dataclass
    # field below never evaluates it at runtime.
    import uuid


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
    # means "import every group the IdP reports".
    group_allowlist: list[str] = Field(default_factory=list)
    # Groups are created on first sight when true. Turn off to make group
    # membership purely an admin decision.
    auto_create_groups: bool = True

    # Membership of any of these groups grants `is_admin`, and it defaults to
    # `platform-admins` rather than to nothing.
    #
    # It used to default to empty, meaning "login never touches the flag". That
    # made admin a database fact with no visible cause, and it made the one
    # knob that decides who administers a deployment something an operator had
    # to know to set. One well-known name, and a directory's own group names
    # reach it through the provider's `group_mappings` (ADR 0048) — mapping is
    # what a directory-specific name is *for*, so nothing here has to know it.
    #
    # This is still overridable, because a deployment whose directory cannot be
    # made to produce this name needs somewhere to say so. What changed is the
    # default.
    #
    # Two consequences to know. Admin follows membership **in both
    # directions**, so a directory account that is not in the group loses the
    # flag at its next login — an upgrade from an empty setting will revoke
    # admin from any directory user who was granted it by hand *and is not in
    # an admin group*, which is why the console now grants the group rather
    # than the flag (`_set_admin_by_membership`). And local accounts are
    # untouched: this derivation runs only on the OIDC provisioning path, so
    # `gateway seed`'s admin keeps working.
    admin_groups: list[str] = Field(default_factory=lambda: ["platform-admins"])

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



class PasswordResetSettings(BaseModel):
    """Self-service password reset by email (ADR 0049).

    Amends the part of ADR 0043 that declined email reset as "a bigger surface
    than the feature is worth". The surface is bounded three ways, all here in
    the environment rather than in the database: the feature is off until this
    section names a mail server (`enabled` plus `smtp_host`), only ``local``
    accounts may reset (a directory user's password belongs to the IdP), and
    the link is a single-use, high-entropy, short-lived token stored only as a
    SHA-256 hash — the same shape as an API key (ADR 0010), for the same
    reason.

    Delivery is plain ``smtplib`` run in a worker thread: the dependency that
    would be adopted for one email is not worth its supply chain. STARTTLS is
    always attempted; a server that refuses TLS refuses the mail.
    """

    enabled: bool = False

    # Short on purpose: the token's only job is to survive "check my mail".
    token_ttl_seconds: int = Field(default=3600, gt=0)
    # Per email address, per worker process — the same sharing trade the login
    # throttle makes (see gateway/login_throttle.py).
    request_cooldown_seconds: float = Field(default=60, gt=0)

    smtp_host: str = ""
    smtp_port: int = Field(default=587, gt=0)
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    # RFC 5322 From, address required: "Pystino <no-reply@example.org>".
    smtp_from: str = ""


class LocalAuthSettings(BaseModel):
    """Local email + password sign-in for the management surface (ADR 0043).

    Off by default: a deployment with an identity provider should not grow a
    second way in just because the feature shipped. Enabling it adds
    ``POST /auth/login``; disabling it makes that endpoint answer 503 and the
    console hides the form. Nothing else changes — the session cookie, its
    lifetime, and everything downstream of it are shared with the OIDC flow.

    There is no self-service registration: an account exists because an
    operator created it, via ``gateway passwd`` or the admin API. Password
    reset by email arrived later, opt-in and environment-configured — see
    ``PasswordResetSettings`` and ADR 0049, which amend the refusal recorded
    here originally.
    """

    enabled: bool = False

    # Argon2id is what protects a weak password from a GPU; no KDF protects it
    # from a dictionary. The floor is low enough to accept generated secrets and
    # long passphrases without friction, and exists so "password1" is refused at
    # every entry point rather than hashed and stored.
    min_password_length: int = Field(default=10, ge=1)

    # Brute-force throttle, per worker process (see gateway/login_throttle.py
    # for why it is not shared across workers).
    max_failed_attempts: int = Field(default=10, ge=1)
    throttle_window_seconds: float = Field(default=900, gt=0)

    # Self-service password reset (ADR 0049): disabled until the deployment
    # names a mail server and says so. Nested because a reset is a local-auth
    # concern — it exists to recover exactly the credential local auth mints.
    password_reset: PasswordResetSettings = Field(
        default_factory=PasswordResetSettings
    )


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


class KnowledgeSettings(BaseModel):
    """Defaults for knowledge-base ingestion and retrieval.

    Every value here is what the deployment does **when the console has said
    nothing** — ``knowledge_config`` is an append-only row that overrides these,
    exactly as ``redaction_config`` overrides ``RedactionSettings``
    (ADR 0033). A deployment that never opens the
    new screen behaves as though the table did not exist, which is what keeps
    the upgrade silent.

    Note which things are *not* here, and why they cannot be. The embedding and
    extraction **models** are rows in this database, chosen by an administrator
    from what the deployment actually has, so naming them in the environment
    would mean an operator maintaining a name in two places and a typo becoming
    a runtime failure rather than a validation error. They live only in the
    config row.
    """

    #: Off by default, and this is the whole feature flag. A deployment that
    #: does not want durable storage of user documents does not get tables
    #: filling up because somebody found the endpoint: `/v1/files` and
    #: `/v1/vector_stores` answer 404 until this is on.
    enabled: bool = False

    #: Characters per chunk, and the overlap between neighbours. Snapshotted
    #: onto each base at creation, so changing these never silently changes
    #: what an existing index means (see `KnowledgeBase`).
    chunk_chars: int = 1200
    chunk_overlap: int = 150

    #: How many passages a search returns by default, and the floor a passage
    #: must clear to be returned at all. The floor is a cosine *similarity*, so
    #: higher is stricter; 0 returns the nearest whatever they are, which is
    #: the right default because a threshold tuned for one embedding model is
    #: wrong for the next.
    search_limit: int = 8
    min_score: float = 0.0

    #: The ceiling on one upload, in bytes. Below the extractor's own 25 MiB
    #: refusal on purpose: the gateway should decline a file it knows the
    #: extractor will reject, rather than buffering it first (see `ocr.py`,
    #: which records that the gateway imposes no body limit of its own).
    max_upload_bytes: int = 20 * 1024 * 1024

    #: How many chunks go to the embedding surface in one call. Batched
    #: because an embedding request has real per-call latency and a document
    #: is hundreds of chunks; bounded because a provider will refuse a payload
    #: that is too large, and a refusal mid-document leaves half an index.
    embed_batch: int = 32


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
    local_auth: LocalAuthSettings = Field(default_factory=LocalAuthSettings)
    redaction: RedactionSettings = Field(default_factory=RedactionSettings)
    extractor: ExtractorSettings = Field(default_factory=ExtractorSettings)
    knowledge: KnowledgeSettings = Field(default_factory=KnowledgeSettings)
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


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so that importing modules can call it freely. Tests clear the cache
    via the ``settings`` fixture rather than mutating the object.
    """
    return Settings()
