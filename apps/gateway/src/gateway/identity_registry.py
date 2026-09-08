"""Identity providers as rows: seeded from the environment, owned by the console.

See ADR 0051.

Two jobs live here. The **registry** turns a provider row into an
``OIDCClient`` on demand and caches it by the row's identity and
``updated_at`` — so a configuration change (issuer, secret, mappings) takes
effect on the *next* login, discovery is fetched lazily per client, and
nothing IdP-shaped sits on any request path but the login itself. The
**seed** inserts the environment's provider as the first row when the table
is empty, which is what makes the upgrade invisible: the deployment that
configured OIDC through ``GATEWAY_OIDC__*`` keeps logging in, and from then
on the row is editable like any other.

Per-provider facts live on the row (groups claim, userinfo toggle, the
IdP→local mappings); the provisioning policy that answers "who may exist
here" stays global in ``oidc_config`` (ADR 0048).
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.config import OIDCSettings, Settings
from gateway.models import GroupSync, IdentityProvider
from gateway.oidc import OIDCClient
from gateway.secrets import SecretBox

logger = logging.getLogger(__name__)


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
    # May a login here adopt a local account with the same verified address
    # (ADR 0056)? A per-provider fact, like the group claim beside it.
    link_local_by_email: bool
    # How far this directory's answer about groups reaches (ADR 0057).
    group_sync: GroupSync
    is_enabled: bool
    source: str  # "console" | "environment"
    updated_at: object = None

    def as_oidc_settings(
        self, redirect_uri: str, access_token_audience: str = ""
    ) -> OIDCSettings:
        """The OIDCSettings one login against this provider needs.

        ``access_token_audience`` comes from the deployment, not the row: it is
        the deployment-wide `/v1` opt-in (ADR 0040), and an empty value here
        would silently switch bearer tokens off for every provider.
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
        link_local_by_email=row.link_local_by_email,
        group_sync=row.group_sync,
        is_enabled=row.is_enabled,
        source="console",
        updated_at=row.updated_at,
    )


def record_from_env(settings: Settings) -> ProviderRecord | None:
    """The environment's single provider, when it is enabled — the fallback."""
    oidc = settings.oidc
    if not oidc.enabled:
        return None
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
        # Never for the environment fallback. It exists so a deployment
        # configured through `GATEWAY_OIDC__*` keeps working across the
        # upgrade that made providers rows, and adopting existing accounts is
        # not a behaviour an upgrade may switch on by itself. The seed turns
        # that fallback into a row on first startup, so turning linking on is
        # one edit away in the console.
        link_local_by_email=False,
        # What the environment fallback has always done, and the only answer
        # that keeps an upgrade invisible.
        group_sync=GroupSync.EVERY_LOGIN,
        is_enabled=True,
        source="environment",
    )


async def list_providers(
    session: AsyncSession, settings: Settings, secrets: SecretBox, *, enabled_only: bool = False
) -> list[ProviderRecord]:
    """Every provider, rows first and the environment's as the fallback.

    The fallback applies only while the table has no rows at all: an operator
    who deletes every row wants no identity provider, not the env one back.
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
        # The `/v1` audience is deployment-wide and flows into every provider
        # client; the row carries nothing per-provider for it.
        self._audience = settings.oidc.access_token_audience
        self._clients: dict[tuple[uuid.UUID, object, str], OIDCClient] = {}

    def client_for(self, record: ProviderRecord, origin: str) -> OIDCClient:
        key = (record.id, record.updated_at, origin)
        client = self._clients.get(key)
        if client is None:
            client = OIDCClient(
                record.as_oidc_settings(
                    f"{origin}/auth/callback/{record.name}", self._audience
                ),
                self._control_http,
            )
            self._clients[key] = client
            # A stale key for the same row is dropped lazily; the dict is tiny.
            # A stale key for the same row is dropped lazily; the dict is tiny.
            self._clients = {
                k: v for k, v in self._clients.items() if k[0] != record.id or k == key
            }
        return client


async def seed_from_env(
    session: AsyncSession, settings: Settings, secrets: SecretBox
) -> None:
    """Insert the environment's provider as the first row, when there is none.

    Runs at startup beside the other idempotent seeds. The table being
    non-empty means the console owns this configuration; nothing is written.
    """
    any_row = (await session.execute(select(IdentityProvider.id).limit(1))).scalar_one_or_none()
    if any_row is not None:
        return
    env_record = record_from_env(settings)
    if env_record is None:
        return
    logger.info("seeding identity provider %r from the environment", env_record.name)
    session.add(
        IdentityProvider(
            id=env_record.id,
            name=env_record.name,
            issuer=env_record.issuer,
            client_id=env_record.client_id,
            client_secret_encrypted=secrets.encrypt(env_record.client_secret),
            scopes=env_record.scopes,
            groups_claim=env_record.groups_claim,
            fetch_userinfo=env_record.fetch_userinfo,
            group_mappings=[],
            is_enabled=True,
        )
    )
    await session.commit()
