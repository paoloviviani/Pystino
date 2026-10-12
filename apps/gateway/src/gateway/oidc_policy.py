"""The OIDC identity policy: what the environment says, what the console says.

See ADR 0048.

The split that shapes this module: **connection is environment, policy is
configuration.** The issuer, client secret and redirect URI are read once at
startup and are not hot-reloadable (discovery is fetched once by design, and
making the IdP connection hot would put its reachability on the request path).
The *policy* — may a stranger become a user, which claim names their groups,
what an IdP group means here — is what an operator changes, so it lives in the
append-only ``oidc_config`` table and is polled like the redaction
configuration it is modelled on (ADR 0033). Roles are not part of it: an
administrator is made in the console or by ``gateway passwd --admin``, never
by a claim (ADR 0069).

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
from gateway.models import Group, OIDCPolicyConfig

logger = logging.getLogger(__name__)

DEFAULT_REFRESH_SECONDS = 10.0

#: What a first-time sign-in does when provisioning is off. "refuse": the
#: stranger is told to ask an administrator. "create_inactive": the account is
#: created but disabled, waiting for an administrator to enable it.
#: Legacy (ADR 0095): the fold still reads these columns, but new decisions
#: are `join_mode` below.
UNKNOWN_USER_POLICIES = ("refuse", "create_inactive")

#: One answer to "who may join" (ADR 0095), for any issuer. `open` signs a
#: stranger up at once; `approval` creates their account disabled for an
#: administrator to enable; `invite` refuses anyone not pre-registered (or
#: directory-created); `group` admits only carriers of a required group, on
#: every sign-in and every `/v1` call.
JOIN_MODES = ("open", "approval", "invite", "group")

#: What a stranger without a required group gets in `group` mode.
GROUP_FALLBACKS = ("refuse", "approval")


def _legacy_from_join(join_mode: str, group_fallback: str) -> tuple[bool, str]:
    """The old two-knob view of a join mode, for readers not yet converted.

    One release only (ADR 0095): `oidc.py`'s gate still reads
    `auto_provision`, and the console still saves it. `group` reads as
    off-and-refusing, which fails closed until the gate step replaces it.
    """
    if join_mode == "open":
        return True, "refuse"
    if join_mode == "approval":
        return False, "create_inactive"
    return False, "refuse"


@dataclass(frozen=True)
class OIDCPolicy:
    """The policy in force on this worker."""

    join_mode: str = "open"
    required_groups: tuple[str, ...] = ()
    group_fallback: str = "refuse"
    groups_claim: str = "groups"
    #: IdP group name -> local group name. Unmapped groups keep their own name.
    group_mappings: dict[str, str] = field(default_factory=dict)
    #: Where each value came from — "environment" until a row carries one, and
    #: per field, because a row may answer one question and not the rest. The
    #: admin screen reports this, because "the console says X and the
    #: environment says Y" is otherwise invisible. Only fields a row actually
    #: decided carry a key; the legacy view below is derived, not decided.
    sources: dict[str, str] = field(default_factory=dict)
    config_id: object = None
    config_at: datetime | None = None
    config_reason: str = ""

    @property
    def auto_provision(self) -> bool:
        """Legacy view (ADR 0095): derived from `join_mode`, one release."""
        return _legacy_from_join(self.join_mode, self.group_fallback)[0]

    @property
    def unknown_user_policy(self) -> str:
        """Legacy view (ADR 0095): derived from `join_mode`, one release."""
        return _legacy_from_join(self.join_mode, self.group_fallback)[1]

    @property
    def source(self) -> str:
        """ "console" when any field is a row decision, else "environment"."""
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
        join_mode=settings.join_mode,
        required_groups=tuple(settings.required_groups),
        group_fallback=settings.group_fallback,
        groups_claim=settings.groups_claim,
        group_mappings={},
    )


def _legacy_join_mode(auto_provision: bool | None, unknown_user_policy: str | None) -> str:
    """The join mode a legacy row's columns mean. `refuse` and silence both
    read `invite`: refusing a stranger while nobody can be pre-registered was
    the old behaviour, and pre-registration is what makes the name honest."""
    if auto_provision:
        return "open"
    if unknown_user_policy == "create_inactive":
        return "approval"
    return "invite"


def effective_policy(settings: OIDCSettings, row: OIDCPolicyConfig | None) -> OIDCPolicy:
    """The newest row's decisions folded over the environment's baseline.

    Per-field: a null column defers. This is why the table can carry a row
    that changes one thing — the row never has to know the deployment's other
    answers to restate them.

    For the join answer the fold tries, in order: the row's `join_mode`, the
    row's legacy columns, the environment (ADR 0095 §1).
    """
    base = environment_policy(settings)
    if row is None:
        return base

    join_mode = row.join_mode
    if join_mode is not None and join_mode not in JOIN_MODES:
        # The database check constraint holds for anything written through the
        # API; a hand-edited row does not get to crash every login. Env stands.
        logger.warning("ignoring oidc_config row %s: join mode %r", row.id, join_mode)
        join_mode = None
    group_fallback = row.group_fallback
    if group_fallback is not None and group_fallback not in GROUP_FALLBACKS:
        logger.warning("ignoring oidc_config row %s: group fallback %r", row.id, group_fallback)
        group_fallback = None

    unknown = row.unknown_user_policy
    if unknown is not None and unknown not in UNKNOWN_USER_POLICIES:
        # The database check constraint holds for anything written through the
        # API; a hand-edited row does not get to crash every login. Env stands.
        logger.warning("ignoring oidc_config row %s: unknown policy %r", row.id, unknown)
        unknown = None

    sources: dict[str, str] = {}
    if join_mode is not None:
        mode = join_mode
        sources["join_mode"] = "console"
    elif row.auto_provision is not None or unknown is not None:
        mode = _legacy_join_mode(row.auto_provision, unknown)
        if row.auto_provision is not None:
            sources["auto_provision"] = "console"
        if unknown is not None:
            sources["unknown_user_policy"] = "console"
    else:
        mode = base.join_mode
    if group_fallback is not None:
        fallback = group_fallback
        sources["group_fallback"] = "console"
    else:
        fallback = base.group_fallback
    if row.required_groups is not None:
        required = tuple(name for name in (str(n).strip() for n in row.required_groups) if name)
        sources["required_groups"] = "console"
    else:
        required = base.required_groups
    groups_claim = base.groups_claim
    if row.groups_claim:
        groups_claim = row.groups_claim
        sources["groups_claim"] = "console"
    # `row.admin_groups` is read by nothing any more (ADR 0069): authorisation
    # is a gateway fact, so the column a deployment may still carry is dead
    # weight the fold declines to resurrect. An administrator is made in the
    # console or by `gateway passwd --admin`, never by a group claim.
    mappings = base.group_mappings
    if row.group_mappings is not None:
        mappings = {}
        for pair in row.group_mappings:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                idp, local = pair
                if isinstance(idp, str) and isinstance(local, str) and idp and local:
                    mappings[idp] = local

    return OIDCPolicy(
        join_mode=mode,
        required_groups=required,
        group_fallback=fallback,
        groups_claim=groups_claim,
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
        self._warned_missing_groups: frozenset[str] = frozenset()
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
                    select(OIDCPolicyConfig).order_by(OIDCPolicyConfig.created_at.desc()).limit(1)
                )
            ).scalar_one_or_none()
            local_names = set((await session.execute(select(Group.name))).scalars().all())
        self._policy = effective_policy(self._settings, row)
        # Console-saved names are validated at PUT; env names have no such
        # gate, so the load warns instead. Warned once per distinct set: the
        # poll runs every ten seconds and must not spam the log.
        missing = frozenset(n for n in self._settings.required_groups if n not in local_names)
        if missing and missing != self._warned_missing_groups:
            logger.warning(
                "GATEWAY_OIDC__REQUIRED_GROUPS names unknown groups: %s. The group "
                "gate admits nobody under those names — import the directory "
                "name or map it to a local group first.",
                ", ".join(sorted(missing)),
            )
        self._warned_missing_groups = missing

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
