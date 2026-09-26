"""Identity providers as rows: a projection of the environment (ADR 0093 §2).

See ADR 0051; superseded in the load-bearing way by ADR 0093.

Two jobs live here. The **registry** turns a provider row into an
``OIDCClient`` on demand and caches it by the row's identity and
``updated_at`` — so a configuration change (the console's directory-sync
settings, or a re-seed) takes effect on the *next* login, discovery is
fetched lazily per client, and nothing IdP-shaped sits on any request path
but the login itself. **`reseed_from_env`** makes the table agree with the
environment on every start: exactly one row is ever enabled, it is always
named ``default``, and every environment-owned field on it is the
environment's, not whatever a console once set — the split the row-owns-it
world (ADR 0051) allowed is what this closes. What is left to the console is
directory sync configuration on that one active row (§14).

Per-provider facts live on the row (groups claim, userinfo toggle, the
IdP→local mappings); the provisioning policy that answers "who may exist
here" stays global in ``oidc_config`` (ADR 0048).
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.config import OIDCSettings, Settings
from gateway.identity_events import record_event
from gateway.models import (
    DirectoryEntry,
    GroupSync,
    IdentityEventAction,
    IdentityEventActor,
    IdentityProvider,
    User,
)
from gateway.oidc import OIDCClient

if TYPE_CHECKING:
    from gateway.identity_policy import AdminRule
from gateway.secrets import SecretBox

logger = logging.getLogger(__name__)

#: One worker re-seeds at a time; the rest block here and then see the
#: committed result rather than racing to insert the same row. Arbitrary but
#: fixed, so every worker names the same lock (ADR 0093 §2). Postgres only —
#: SQLite has no advisory locks, and a single-process test needs none.
_RESEED_ADVISORY_LOCK_KEY = 93_002_001


@dataclass(frozen=True)
class ProviderRecord:
    """One provider, whatever table or fallback produced it."""

    id: uuid.UUID
    name: str
    issuer: str
    client_id: str
    client_secret: str
    scopes: list[str]
    groups_claim: str
    fetch_userinfo: bool
    group_mappings: dict[str, str]
    # May an unknown identity here attach to an existing account by verified
    # email (ADR 0056, repurposed by ADR 0093 §6 into a cross-issuer rule). A
    # display copy of `GATEWAY_OIDC__LINK_BY_EMAIL`; the linking rule itself
    # reads the setting, not this field (stage c).
    link_by_email: bool
    # How far this directory's answer about groups reaches (ADR 0057).
    group_sync: GroupSync
    is_enabled: bool
    source: str  # "console" | "environment"
    updated_at: object = None
    # Back-channel base (OIDCSettings.internal_base_url); empty for an IdP
    # this server reaches at its public issuer.
    internal_base_url: str = ""
    # Signing-out override (`{redirect}` = the page to come back to); empty
    # means discovery's end_session_endpoint, else the kind's default.
    logout_url: str = ""
    # Identity policy (ADR 0088); defaults reproduce the behaviour before it.
    kind: str = "generic"
    group_source: str = "claim"
    # Provenance display only (ADR 0093 §2): "claim" when
    # `GATEWAY_OIDC__ADMIN_CLAIM`/`_VALUES` are both set, else "console". The
    # env email rule (`OIDC_ADMIN_EMAIL`) has no row-level display — it grants
    # per person, on `users.admin_rule`, not per provider.
    admin_source: str = "console"
    admin_claim: str = "groups"
    admin_values: tuple[str, ...] = ()
    subject_claim: str = "sub"
    sync_adapter: str = "none"
    sync_interval_minutes: int = 60
    sync_deprovision: str = "disable"
    sync_create_users: bool = True
    sync_confirmed: bool = False

    def admin_rule(self) -> "AdminRule | None":
        from gateway.identity_policy import admin_rule

        return admin_rule(self.admin_source, self.admin_claim, self.admin_values)

    def as_oidc_settings(
        self,
        redirect_uri: str,
        access_token_audience: str = "",
        accepted_clients: str = "",
    ) -> OIDCSettings:
        """The OIDCSettings one login against this provider needs.

        ``access_token_audience`` and ``accepted_clients`` come from the
        deployment, not the row: both are deployment-wide facts (ADR 0040,
        ADR 0093 §2), and an empty value here would silently switch off the
        checks they gate for every provider.
        """
        return OIDCSettings(
            enabled=True,
            issuer=self.issuer,
            client_id=self.client_id,
            client_secret=self.client_secret,
            redirect_uri=redirect_uri,
            scopes=list(self.scopes),
            groups_claim=self.groups_claim,
            fetch_userinfo=self.fetch_userinfo,
            access_token_audience=access_token_audience,
            accepted_clients=accepted_clients,
            internal_base_url=self.internal_base_url,
        )

    def mappings_dict(self) -> dict[str, str]:
        return dict(self.group_mappings)


def record_from_row(row: IdentityProvider, secrets: SecretBox) -> ProviderRecord:
    secret = secrets.decrypt(row.client_secret_encrypted)
    return ProviderRecord(
        id=row.id,
        name=row.name,
        issuer=row.issuer,
        client_id=row.client_id,
        client_secret=secret,
        scopes=list(row.scopes or []),
        groups_claim=row.groups_claim,
        fetch_userinfo=row.fetch_userinfo,
        group_mappings=dict(row.group_mappings or []),
        link_by_email=row.link_by_email,
        group_sync=row.group_sync,
        is_enabled=row.is_enabled,
        source="console",
        updated_at=row.updated_at,
        internal_base_url=row.internal_base_url or "",
        logout_url=row.logout_url or "",
        kind=row.kind or "generic",
        group_source=row.group_source or "claim",
        admin_source=row.admin_source or "console",
        admin_claim=row.admin_claim or "groups",
        admin_values=tuple(row.admin_values or ()),
        subject_claim=row.subject_claim or "sub",
        sync_adapter=row.sync_adapter or "none",
        sync_interval_minutes=row.sync_interval_minutes or 60,
        sync_deprovision=row.sync_deprovision or "disable",
        sync_create_users=bool(row.sync_create_users),
        sync_confirmed=bool(row.sync_confirmed),
    )


def record_from_env(settings: Settings) -> ProviderRecord | None:
    """The environment's single provider, when it is enabled.

    Both the fallback `list_providers` uses while the table is empty, and the
    shape `reseed_from_env` writes — one function, so the two can never
    silently disagree about what the environment says.
    """
    oidc = settings.oidc
    if not oidc.enabled:
        return None
    # ADR 0093 §5.2: displayed only. The env claim rule's *evaluation* reads
    # settings directly (`apply_env_admin_rules`, stage 5), never this row.
    has_claim_rule = bool(oidc.admin_claim and oidc.admin_claim_values)
    is_bundled = oidc.kind == "authelia"
    return ProviderRecord(
        id=uuid.uuid5(uuid.NAMESPACE_URL, f"env-oidc:{oidc.issuer}"),
        name="default",
        issuer=oidc.issuer,
        client_id=oidc.client_id,
        client_secret=oidc.client_secret.get_secret_value(),
        scopes=list(oidc.scopes),
        groups_claim=oidc.groups_claim,
        fetch_userinfo=oidc.fetch_userinfo,
        group_mappings={},
        link_by_email=oidc.link_by_email,
        # Groups mean nothing for the bundled Authelia (§8.3): its users file
        # carries no group but "users", so the claim is never consulted.
        group_sync=GroupSync.NEVER if is_bundled else GroupSync(oidc.group_sync),
        is_enabled=True,
        source="environment",
        internal_base_url=oidc.internal_base_url,
        logout_url=oidc.logout_url,
        kind=oidc.kind,
        group_source="none" if is_bundled else "claim",
        admin_source="claim" if has_claim_rule else "console",
        admin_claim=oidc.admin_claim if has_claim_rule else "groups",
        admin_values=tuple(oidc.admin_claim_value_list()) if has_claim_rule else (),
        sync_adapter=_seed_adapter(oidc.kind),
        sync_confirmed=False,
    )


def _seed_adapter(kind: str) -> str:
    """The pull adapter a freshly created row of this kind starts with."""
    return "authelia_file" if kind == "authelia" else "none"


async def list_providers(
    session: AsyncSession, settings: Settings, secrets: SecretBox, *, enabled_only: bool = False
) -> list[ProviderRecord]:
    """Every provider, rows first and the environment's as the fallback.

    The fallback applies only while the table has no rows at all — before
    this process's first `reseed_from_env` has committed. Once it has run,
    the table always has at least the environment's row.
    """
    rows = list(
        (await session.execute(select(IdentityProvider).order_by(IdentityProvider.name)))
        .scalars()
        .all()
    )
    if rows:
        records = [record_from_row(row, secrets) for row in rows]
    else:
        env_record = record_from_env(settings)
        records = [env_record] if env_record is not None else []
    if enabled_only:
        records = [record for record in records if record.is_enabled]
    return records


async def provider_by_name(
    session: AsyncSession, settings: Settings, secrets: SecretBox, name: str
) -> ProviderRecord | None:
    for record in await list_providers(session, settings, secrets):
        if record.name == name:
            return record
    return None


class OIDCProviderRegistry:
    """Builds and caches one ``OIDCClient`` per provider row and origin.

    The cache key includes the row's ``updated_at`` and the origin the login
    arrived on (the redirect URI is derived from it, and the IdP must have
    that exact URI registered), so a configuration change and a different
    origin each get a fresh client with their own discovery — and everything
    else reuses the cached one, discovery included.
    """

    def __init__(self, control_http: Any, secrets: SecretBox, settings: Settings) -> None:
        self._control_http = control_http
        self._secrets = secrets
        # Deployment-wide facts that flow into every provider client; the row
        # carries nothing per-provider for either (ADR 0040, ADR 0093 §2).
        self._audience = settings.oidc.access_token_audience
        self._accepted_clients = settings.oidc.accepted_clients
        self._clients: dict[tuple[uuid.UUID, object, str], OIDCClient] = {}

    def client_for(self, record: ProviderRecord, origin: str) -> OIDCClient:
        key = (record.id, record.updated_at, origin)
        client = self._clients.get(key)
        if client is None:
            client = OIDCClient(
                record.as_oidc_settings(
                    f"{origin}/auth/callback/{record.name}", self._audience, self._accepted_clients
                ),
                self._control_http,
            )
            self._clients[key] = client
            # A stale key for the same row is dropped lazily; the dict is tiny.
            self._clients = {
                k: v for k, v in self._clients.items() if k[0] != record.id or k == key
            }
        return client


async def reseed_from_env(session: AsyncSession, settings: Settings, secrets: SecretBox) -> None:
    """Make the provider table agree with the environment. Runs on every start.

    Replaces `seed_from_env`, whose seed only ever filled an *empty* table and
    otherwise backfilled two columns on a matching row (the old
    `_fill_internal_base_url` / `_fill_kind`, both gone now, subsumed by this).
    ADR 0093 §2 makes the environment win outright, in one transaction:

    1. The row whose issuer matches `.env` (if any) gets every environment-
       owned field overwritten; the row already named ``default`` (if it is a
       *different* row — the issuer changed, or this is a switch back) is
       renamed aside and disabled, never deleted, so its identities stay
       resolvable.
    2. Every row whose issuer does not match is disabled.
    3. Whichever row is the environment's is left as ``is_enabled=True``,
       named ``default``, so the callback path `/auth/callback/default` never
       changes.

    Callers decide whether a failure here is fatal (`main.py`, gated to
    `environment == "production"`) — this function only ever raises or
    completes.
    """
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": _RESEED_ADVISORY_LOCK_KEY}
        )

    env_record = record_from_env(settings)
    if env_record is None:
        return

    current = (
        await session.execute(
            select(IdentityProvider).where(IdentityProvider.issuer == env_record.issuer)
        )
    ).scalar_one_or_none()

    # The row named "default" is stale exactly when it is not `current` — a
    # switch (the issuer changed) or a switch back (this issuer's row exists
    # but was the disabled one). Renamed and flushed *before* anything else
    # claims the name: both dialects check the unique index immediately, so
    # writing the new name first would collide with the still-current one.
    stale_default = (
        await session.execute(
            select(IdentityProvider).where(
                IdentityProvider.name == "default",
                IdentityProvider.issuer != env_record.issuer,
            )
        )
    ).scalar_one_or_none()
    old_issuer = stale_default.issuer if stale_default is not None else ""
    was_bundled = stale_default is not None and stale_default.kind == "authelia"
    if stale_default is not None:
        stamp = datetime.now(UTC).strftime("%Y%m%d")
        stale_default.name = f"previous-{stamp}-{uuid.uuid4().hex[:4]}"
        stale_default.is_enabled = False
        await session.flush()

    if current is not None:
        _apply_env_owned_fields(current, env_record, secrets)
        target = current
    else:
        target = IdentityProvider(
            id=env_record.id,
            name="default",
            issuer=env_record.issuer,
            client_id=env_record.client_id,
            client_secret_encrypted=secrets.encrypt(env_record.client_secret),
            scopes=env_record.scopes,
            groups_claim=env_record.groups_claim,
            fetch_userinfo=env_record.fetch_userinfo,
            group_mappings=[],
            link_by_email=env_record.link_by_email,
            group_sync=env_record.group_sync,
            internal_base_url=env_record.internal_base_url,
            logout_url=env_record.logout_url,
            kind=env_record.kind,
            group_source=env_record.group_source,
            admin_source=env_record.admin_source,
            admin_claim=env_record.admin_claim,
            admin_values=list(env_record.admin_values),
            sync_adapter=env_record.sync_adapter,
            sync_confirmed=False,
            is_enabled=True,
        )
        session.add(target)
        await session.flush()

    if current is None or stale_default is not None:
        if was_bundled and stale_default is not None:
            # Login-name bindings survive an origin change: the entries stay
            # findable under the row that is now active.
            await session.execute(
                update(DirectoryEntry)
                .where(DirectoryEntry.provider_id == stale_default.id)
                .values(provider_id=target.id)
            )
        logger.warning(
            "identity provider re-seeded: issuer changed from %r to %r",
            old_issuer or "(none)",
            env_record.issuer,
        )
        await record_event(
            session,
            actor_type=IdentityEventActor.SYSTEM,
            actor_label="system",
            action=IdentityEventAction.IDP_RESEED,
            detail={"old_issuer": old_issuer, "new_issuer": env_record.issuer},
        )

    others = (
        await session.execute(
            select(IdentityProvider).where(IdentityProvider.issuer != env_record.issuer)
        )
    ).scalars()
    for row in others:
        row.is_enabled = False

    disabled = (
        (
            await session.execute(
                select(IdentityProvider).where(IdentityProvider.is_enabled.is_(False))
            )
        )
        .scalars()
        .all()
    )
    for row in disabled:
        count = (
            await session.execute(
                select(func.count(User.id)).where(User.issuer == row.issuer)
            )
        ).scalar_one()
        logger.warning(
            "identity provider %r (%s, %d user%s) disabled: set it in .env to use it",
            row.name,
            row.issuer,
            count,
            "" if count == 1 else "s",
        )

    await session.commit()


def _apply_env_owned_fields(row: IdentityProvider, env: ProviderRecord, secrets: SecretBox) -> None:
    """Overwrite exactly the fields ADR 0093 §2 calls environment-owned.

    Never touches `sync_adapter`, `sync_config_encrypted`,
    `sync_interval_minutes`, `sync_deprovision`, `sync_create_users` or
    `sync_confirmed` — those are console-owned, set through the directory
    sync endpoints once an administrator configures external sync (§14), and
    `fetch_userinfo`, set once at creation and left alone after.
    """
    row.issuer = env.issuer
    row.client_id = env.client_id
    row.client_secret_encrypted = secrets.encrypt(env.client_secret)
    row.scopes = env.scopes
    row.groups_claim = env.groups_claim
    row.internal_base_url = env.internal_base_url
    row.logout_url = env.logout_url
    row.kind = env.kind
    row.group_sync = env.group_sync
    if env.kind == "authelia":
        row.group_source = "none"
    row.link_by_email = env.link_by_email
    row.admin_source = env.admin_source
    row.admin_claim = env.admin_claim
    row.admin_values = list(env.admin_values)
    row.is_enabled = True
    row.name = "default"
