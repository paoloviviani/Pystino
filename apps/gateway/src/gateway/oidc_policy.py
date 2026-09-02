"""The OIDC identity policy: what the environment says, what the console says.

See [ADR 0048](../../../../docs/adr/0048-oidc-policy-configuration.md).

The split that shapes this module: **connection is environment, policy is
configuration.** The issuer, client secret and redirect URI are read once at
startup and are not hot-reloadable (discovery is fetched once by design, and
making the IdP connection hot would put its reachability on the request path).
The *policy* — may a stranger become a user, which claim names their groups,
what an IdP group means here, which local group confers admin — is what an
operator changes, so it lives in the append-only ``oidc_config`` table and is
polled like the redaction configuration it is modelled on (ADR 0033).

A row never has to answer everything: each column is nullable, and a null
defers to the environment for that one field. Effective policy is therefore a
per-field fold of the newest row over the environment's values.
"""

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gateway.config import OIDCSettings
from gateway.models import OIDCPolicyConfig

logger = logging.getLogger(__name__)

DEFAULT_REFRESH_SECONDS = 10.0

#: What a first-time sign-in does when provisioning is off. "refuse": the
#: stranger is told to ask an administrator. "create_inactive": the account is
#: created but disabled, waiting for an administrator to enable it.
UNKNOWN_USER_POLICIES = ("refuse", "create_inactive")


@dataclass(frozen=True)
class OIDCPolicy:
    """The policy in force on this worker."""

    auto_provision: bool
    unknown_user_policy: str
    groups_claim: str
    #: Local group names that confer ``is_admin``. Compared against *mapped*
    #: names — what a group is called here, not what the IdP calls it.
    admin_groups: list[str] = field(default_factory=list)
    #: IdP group name -> local group name. Unmapped groups keep their own name.
    group_mappings: dict[str, str] = field(default_factory=dict)
    #: Where each value came from — "environment" until a row carries one, and
    #: per field, because a row may answer one question and not the rest. The
    #: admin screen reports this, because "the console says X and the
    #: environment says Y" is otherwise invisible.
    sources: dict[str, str] = field(default_factory=dict)
    config_id: object = None
    config_at: datetime | None = None
    config_reason: str = ""

    @property
    def source(self) -> str:
        """"console" when any field is a row decision, else "environment"."""
        return "console" if self.sources else "environment"

    def map_group_names(self, idp_names: list[str]) -> list[str]:
        """IdP group names to local names, order preserved, duplicates dropped.

        A many-to-one mapping can hand back the same local name twice (two IdP
        groups folded into one platform group); the caller wants a set with
        stable order, not a repetition.
        """
        seen: list[str] = []
        for name in idp_names:
            mapped = self.group_mappings.get(name, name)
            if mapped not in seen:
                seen.append(mapped)
        return seen


def environment_policy(settings: OIDCSettings) -> OIDCPolicy:
    """The policy as the environment states it — the baseline every row folds onto."""
    return OIDCPolicy(
        auto_provision=True,
        unknown_user_policy="refuse",
        groups_claim=settings.groups_claim,
        admin_groups=list(settings.admin_groups),
        group_mappings={},
    )


def effective_policy(settings: OIDCSettings, row: OIDCPolicyConfig | None) -> OIDCPolicy:
    """The newest row's decisions folded over the environment's baseline.

    Per-field: a null column defers. This is why the table can carry a row
    that changes one thing — the row never has to know the deployment's other
    answers to restate them.
    """
    base = environment_policy(settings)
    if row is None:
        return base

    unknown = row.unknown_user_policy
    if unknown is not None and unknown not in UNKNOWN_USER_POLICIES:
        # The database check constraint holds for anything written through the
        # API; a hand-edited row does not get to crash every login. Env stands.
        logger.warning("ignoring oidc_config row %s: unknown policy %r", row.id, unknown)
        unknown = None

    sources: dict[str, str] = {}
    auto_provision = base.auto_provision
    if row.auto_provision is not None:
        auto_provision = row.auto_provision
        sources["auto_provision"] = "console"
    if unknown is not None:
        base_unknown = unknown
        sources["unknown_user_policy"] = "console"
    else:
        base_unknown = base.unknown_user_policy
    groups_claim = base.groups_claim
    if row.groups_claim:
        groups_claim = row.groups_claim
        sources["groups_claim"] = "console"
    admin_groups = base.admin_groups
    if row.admin_groups is not None:
        admin_groups = [str(name) for name in row.admin_groups]
        sources["admin_groups"] = "console"
    mappings = base.group_mappings
    if row.group_mappings is not None:
        mappings = {}
        for pair in row.group_mappings:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                idp, local = pair
                if isinstance(idp, str) and isinstance(local, str) and idp and local:
                    mappings[idp] = local

    return OIDCPolicy(
        auto_provision=auto_provision,
        unknown_user_policy=base_unknown,
        groups_claim=groups_claim,
        admin_groups=admin_groups,
        group_mappings=mappings,
        sources=sources,
        config_id=row.id,
        config_at=row.created_at,
        config_reason=row.reason,
    )


class OIDCPolicyResolver:
    """Holds the policy in force for this worker, and polls for changes.

    Deliberately the shape of ``RedactionResolver`` (ADR 0033) with the parts
    that made it large removed: no per-request lookups, no engine objects, no
    folded caches — one policy object swapped every poll, and the login paths
    read ``app.state.oidc_policy``.
    """

    def __init__(
        self,
        settings: OIDCSettings,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._refresh_seconds = refresh_seconds
        self._policy = environment_policy(settings)
        self._task: asyncio.Task[None] | None = None

    @property
    def policy(self) -> OIDCPolicy:
        return self._policy

    @property
    def refresh_seconds(self) -> float:
        """How far behind the console's save a worker may lag."""
        return self._refresh_seconds

    async def refresh_once(self) -> None:
        """Poll once. Exposed for tests and for the startup read."""
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(OIDCPolicyConfig)
                    .order_by(OIDCPolicyConfig.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        self._policy = effective_policy(self._settings, row)

    def start(self) -> None:
        self._task = asyncio.create_task(self._poll_forever())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _poll_forever(self) -> None:
        while True:
            await asyncio.sleep(self._refresh_seconds)
            try:
                await self.refresh_once()
            except Exception:
                # The poll is a convenience, not a correctness mechanism: the
                # change lands within ten seconds of being made, and a failed
                # poll means the previous policy stays in force until the next
                # one. Crash-looping the worker over it would be worse.
                logger.exception("oidc policy poll failed; keeping current policy")
