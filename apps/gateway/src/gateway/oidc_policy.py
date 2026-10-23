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

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gateway.config import AccessSettings, OIDCSettings
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

#: One answer to "what flag do new groups get" (ADR 0096, amended): `on`
#: or `off`. The per-group booleans decide who holds a capability; this
#: setting only defaults them at creation.
CAPABILITY_MODES = ("on", "off")

#: The 0.7.x vocabulary, still read for one release — a stored row or an old
#: environment spelling keeps its behaviour (`everyone` allowed by default,
#: `groups` denied by default) while the new one is adopted.
LEGACY_CAPABILITY_MODES = {"everyone": "on", "groups": "off"}


def _normalize_capability_mode(value: str | None) -> str | None:
    """A row or environment spelling as the canonical `on`/`off`, else None
    for the caller to log-and-defer on."""
    if value is None:
        return None
    if value in CAPABILITY_MODES:
        return value
    return LEGACY_CAPABILITY_MODES.get(value)


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
    #: ADR 0096 (amended): the default flag new groups are created with —
    #: `on` or `off`. The per-group booleans decide who holds a capability.
    code_agents_mode: str = "on"
    api_keys_mode: str = "on"
    local_connectors_mode: str = "on"
    #: IdP group name -> local group name. A claimed name resolves to a local
    #: group only through this table: an unmapped name resolves to nothing,
    #: even when a local group carries the same name.
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

    def map_claim_group_names(
        self,
        idp_names: list[str],
        *,
        claim_source: bool = True,
    ) -> list[str]:
        """IdP group names from a token, to local names — the one mapper both
        claim-bearing doors run (`sign_in` and the bearer sync), so the gate's
        stranger check, the membership reconciliation and the bearer path's
        divergence check all agree on what a claim named.

        The rule: a claimed name reaches a local group only through a
        mapping. Keep the mapped names, drop the rest — even when a local
        group carries the same name, because the IdP's group namespace is
        user-writable and a same-name claim is not a vouch. Under ``auto``
        import the dropped names are self-mapped at provisioning
        (:mod:`gateway.group_import`), which is how that mode's "every name
        becomes a group" convenience survives inside the rule.

        ``OIDC_GROUP_ALLOWLIST`` is a pre-filter applied before this
        (``extract_groups``): it narrows which claimed names are considered
        at all and confers nothing by itself. A directory-source provider
        (``claim_source=False``) is unchanged: its claims never reconcile
        into memberships and its names arrive vouched by the directory
        connection — mapped names still map, the rest keep their own name.
        """
        seen: list[str] = []
        for name in idp_names:
            if claim_source:
                mapped = self.group_mappings.get(name)
                if mapped is None:
                    continue
            else:
                mapped = self.group_mappings.get(name, name)
            if mapped not in seen:
                seen.append(mapped)
        return seen


def environment_policy(settings: OIDCSettings, access: AccessSettings | None = None) -> OIDCPolicy:
    """The policy as the environment states it — the baseline every row folds onto."""
    return OIDCPolicy(
        join_mode=settings.join_mode,
        required_groups=tuple(settings.required_groups),
        group_fallback=settings.group_fallback,
        groups_claim=settings.groups_claim,
        group_mappings={},
        code_agents_mode=access.code_agents if access is not None else "everyone",
        api_keys_mode=access.api_keys if access is not None else "everyone",
        local_connectors_mode=access.local_connectors if access is not None else "on",
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


def effective_policy(
    settings: OIDCSettings,
    row: OIDCPolicyConfig | None,
    access: AccessSettings | None = None,
) -> OIDCPolicy:
    """The newest row's decisions folded over the environment's baseline.

    Per-field: a null column defers. This is why the table can carry a row
    that changes one thing — the row never has to know the deployment's other
    answers to restate them.

    For the join answer the fold tries, in order: the row's `join_mode`, the
    row's legacy columns, the environment (ADR 0095 §1). For the capability
    defaults (ADR 0096, amended) it tries the row's column — normalising the
    0.7.x `everyone`/`groups` spellings — then the environment
    (``GATEWAY_ACCESS__*``, via ``access``, same normalisation).
    """
    base = environment_policy(settings, access)
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

    code_agents_mode = base.code_agents_mode
    normalized = _normalize_capability_mode(row.code_agents_mode)
    if row.code_agents_mode is not None and normalized is None:
        logger.warning(
            "ignoring oidc_config row %s: code_agents_mode %r", row.id, row.code_agents_mode
        )
    elif normalized is not None:
        code_agents_mode = normalized
        sources["code_agents_mode"] = "console"
    api_keys_mode = base.api_keys_mode
    normalized = _normalize_capability_mode(row.api_keys_mode)
    if row.api_keys_mode is not None and normalized is None:
        logger.warning("ignoring oidc_config row %s: api_keys_mode %r", row.id, row.api_keys_mode)
    elif normalized is not None:
        api_keys_mode = normalized
        sources["api_keys_mode"] = "console"
    local_connectors_mode = base.local_connectors_mode
    normalized = _normalize_capability_mode(row.local_connectors_mode)
    if row.local_connectors_mode is not None and normalized is None:
        logger.warning(
            "ignoring oidc_config row %s: local_connectors_mode %r",
            row.id,
            row.local_connectors_mode,
        )
    elif normalized is not None:
        local_connectors_mode = normalized
        sources["local_connectors_mode"] = "console"

    return OIDCPolicy(
        join_mode=mode,
        required_groups=required,
        group_fallback=fallback,
        groups_claim=groups_claim,
        group_mappings=mappings,
        code_agents_mode=code_agents_mode,
        api_keys_mode=api_keys_mode,
        local_connectors_mode=local_connectors_mode,
        sources=sources,
        config_id=row.id,
        config_at=row.created_at,
        config_reason=row.reason,
    )


def pending_policy_row(
    stored: OIDCPolicyConfig | None,
    fields: dict[str, object],
    *,
    required_groups: list[str] | None,
    mappings: list[list[str]] | None,
) -> OIDCPolicyConfig:
    """The row a policy save would store: the newest stored row's answers
    carried forward for every field the payload does not send.

    Only the newest `oidc_config` row is ever in force, and the console saves
    one field at a time — so a save that stored null for every unsent field
    would reset the other saves' answers to the environment (a group-mode
    save followed by a fallback-only save would fail open). Unsent fields
    carry the newest row's values instead; an explicitly sent null clears the
    field back to the environment. A payload that names a join mode clears
    the carried legacy columns (and a payload naming legacy columns clears
    the carried join ones), so a stored row never mixes the two spellings —
    the fold would let one silently win. The map endpoint and the auto
    import's self-mapping both build their rows through this same helper, so
    an append made outside the console cannot reset what the console saved.
    """
    row = OIDCPolicyConfig(
        auto_provision=stored.auto_provision if stored is not None else None,
        unknown_user_policy=stored.unknown_user_policy if stored is not None else None,
        join_mode=stored.join_mode if stored is not None else None,
        required_groups=(
            list(stored.required_groups)
            if stored is not None and stored.required_groups is not None
            else None
        ),
        group_fallback=stored.group_fallback if stored is not None else None,
        groups_claim=stored.groups_claim if stored is not None else None,
        group_mappings=(
            [list(pair) for pair in stored.group_mappings]
            if stored is not None and stored.group_mappings is not None
            else None
        ),
        code_agents_mode=stored.code_agents_mode if stored is not None else None,
        api_keys_mode=stored.api_keys_mode if stored is not None else None,
        local_connectors_mode=stored.local_connectors_mode if stored is not None else None,
    )
    if "auto_provision" in fields:
        row.auto_provision = fields["auto_provision"]  # type: ignore[assignment]
    if "unknown_user_policy" in fields:
        row.unknown_user_policy = fields["unknown_user_policy"]  # type: ignore[assignment]
    if "join_mode" in fields:
        row.join_mode = fields["join_mode"]  # type: ignore[assignment]
    if "required_groups" in fields:
        row.required_groups = required_groups
    if "group_fallback" in fields:
        row.group_fallback = fields["group_fallback"]  # type: ignore[assignment]
    if "groups_claim" in fields:
        row.groups_claim = fields["groups_claim"]  # type: ignore[assignment]
    if "group_mappings" in fields:
        row.group_mappings = mappings
    if "code_agents_mode" in fields:
        row.code_agents_mode = fields["code_agents_mode"]  # type: ignore[assignment]
    if "api_keys_mode" in fields:
        row.api_keys_mode = fields["api_keys_mode"]  # type: ignore[assignment]
    if "local_connectors_mode" in fields:
        row.local_connectors_mode = fields["local_connectors_mode"]  # type: ignore[assignment]
    if fields.get("join_mode") is not None:
        row.auto_provision = None
        row.unknown_user_policy = None
    if fields.get("auto_provision") is not None or fields.get("unknown_user_policy") is not None:
        row.join_mode = None
    return row


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
        access: AccessSettings | None = None,
        refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
    ) -> None:
        self._settings = settings
        self._access = access
        self._session_factory = session_factory
        self._refresh_seconds = refresh_seconds
        self._policy = environment_policy(settings, access)
        self._warned_missing_groups: frozenset[str] = frozenset()
        self._warned_machine_mismatch: tuple[str, tuple[str, ...], bool] | None = None
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
            policy = effective_policy(self._settings, row, self._access)
            # Console-saved names are validated at PUT, but the group can be
            # deleted afterwards — and env names have no PUT gate at all — so
            # the load warns for whatever the folded policy names that does
            # not exist. Warned once per distinct set: the poll runs every ten
            # seconds and must not spam the log. No required names, no query.
            required = policy.required_groups
            local_names: set[str] = set()
            if required:
                local_names = set((await session.execute(select(Group.name))).scalars().all())
        self._policy = policy
        missing = frozenset(n for n in required if n not in local_names)
        if missing and missing != self._warned_missing_groups:
            logger.warning(
                "The identity policy's required groups name unknown groups: %s. The "
                "group gate admits nobody under those names — map the identity "
                "provider's name to a local group first (Console → Groups).",
                ", ".join(sorted(missing)),
            )
        self._warned_missing_groups = missing
        # ADR 0096 (amended): a code_agents flag switched off somewhere while
        # the accepted machine client cannot match it is a restriction whose
        # machine-link half can never fire — the bulk and patch paths refuse
        # the change, but the flags may predate the refusal or the
        # environment may have changed under them, so the poll logs it too.
        # ERROR, not warning: unlike an unknown group name (which fails
        # closed) this fails silently open. Once per distinct state, like
        # the warning above: the poll runs every ten seconds. One cheap
        # EXISTS, and only when the machine client mismatches at all — the
        # common case (aligned, or no allowlist) costs nothing.
        machine = self._settings.machine_client_id
        accepted = tuple(self._settings.accepted_client_list())
        any_off: bool | None = None
        if machine and accepted and machine not in accepted:
            async with self._session_factory() as flag_session:
                any_off = (
                    await flag_session.execute(
                        select(Group.id)
                        .where(
                            Group.is_active.is_(True),
                            or_(
                                Group.code_agents_allowed.is_(False),
                                Group.local_connectors_allowed.is_(False),
                            ),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none() is not None
        mismatch = (machine, accepted, any_off) if any_off else None
        if mismatch is not None and mismatch != self._warned_machine_mismatch:
            logger.error(
                "An active group has code_agents or local_connectors off while "
                "GATEWAY_OIDC__MACHINE_CLIENT_ID=%r names a client not in "
                "GATEWAY_OIDC__ACCEPTED_CLIENTS (%s): the "
                "restriction cannot reach machine tokens, and a chat-only "
                "person's machine keeps working. Name the machine client the "
                "deployment accepts.",
                machine,
                ", ".join(accepted),
            )
        self._warned_machine_mismatch = mismatch

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
