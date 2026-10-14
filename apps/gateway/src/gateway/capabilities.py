"""Who holds which capability (ADR 0096, amended).

A capability is only a per-group flag: a person holds ``code_agents`` (the
/code panel and the machine links that serve it) or ``api_keys`` (minting
and using ``gwk_`` keys) as an administrator, or through an active
membership in an active group with the flag on. There is no mode that gates
access anymore — the deployment setting is the default new groups are
created with (see :func:`default_flags`).

Pure, no I/O: these functions read the user and its memberships the caller
already loaded, so the bearer path's steady state costs no query —
``test_query_counts.py`` pins that. A membership's *source* does not matter
— a directory grant counts as much as a hand grant — because a capability
flag is a property of the group, not of who vouched for the membership. A
person in no group holds neither (in practice the default group covers
everyone at first sign-in).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.models import Group, OIDCPolicyConfig, User
from gateway.oidc_policy import OIDCPolicy, effective_policy

if TYPE_CHECKING:
    from gateway.config import Settings


@dataclass(frozen=True, slots=True)
class Capabilities:
    """The two ADR 0096 capabilities a person effectively holds."""

    code_agents: bool
    api_keys: bool


def effective_capabilities(user: User) -> Capabilities:
    """The capabilities in force for one person, from their memberships alone."""
    return Capabilities(
        code_agents=_holds(user, "code_agents"),
        api_keys=_holds(user, "api_keys"),
    )


def default_flags(policy: OIDCPolicy | None) -> Capabilities:
    """The flags a newly created group carries: the folded default.

    The row's answer when it names one, else the environment baseline, else
    on — which is what "everyone held everything" meant, so a deployment
    that never decides creates groups exactly as usable as before. Without a
    policy at all (a path that cannot fold one) the answer is on for the
    same reason. Both the canonical `on`/`off` and the legacy
    `everyone`/`groups` spellings read, so a 0.7.x row or environment keeps
    its meaning.
    """
    if policy is None:
        return Capabilities(code_agents=True, api_keys=True)
    return Capabilities(
        code_agents=_default_on(policy.code_agents_mode),
        api_keys=_default_on(policy.api_keys_mode),
    )


def _default_on(mode: str) -> bool:
    return mode in ("on", "everyone")


def new_group_flags(defaults: Capabilities) -> dict[str, bool]:
    """The capability flag columns a newly created group carries (ADR 0096, amended).

    Every path that INSERTs a group — the console's create, map-to-new, auto
    import and the self-mapping it mints, the directory engine, the default
    group, ``gateway seed`` — takes its flag values from here, so a new group
    is as usable as the deployment's default says and no path can silently
    fall back to the column default (off) again: `_group_named` did exactly
    that, and the fresh-kit check caught every fresh ``users`` chat-only. The
    flags apply only when the row is CREATED; an existing group's flags are
    never touched (the conflict-safe inserts keep ``on conflict do nothing``).
    """
    return {"code_agents_allowed": defaults.code_agents, "api_keys_allowed": defaults.api_keys}


async def load_defaults(session: AsyncSession, settings: Settings) -> Capabilities:
    """The folded capability default, reading the newest row (ADR 0096 amended).

    One query, for background paths only — directory sync runs, the deploy
    CLI: every request path and console route carries the folded policy
    already and must not pay this. The row's answer wins when it names one,
    else the environment baseline, else on.
    """
    row = (
        await session.execute(
            select(OIDCPolicyConfig).order_by(OIDCPolicyConfig.created_at.desc()).limit(1)
        )
    ).scalar_one_or_none()
    return default_flags(effective_policy(settings.oidc, row, settings.access))


def _holds(user: User, flag_name: str) -> bool:
    if not user.is_active:
        return False
    if user.is_admin:
        return True
    return any(
        membership.group is not None
        and membership.group.is_active
        and _group_flag(membership.group, flag_name)
        for membership in user.memberships
    )


def _group_flag(group: Group, flag_name: str) -> bool:
    if flag_name == "code_agents":
        return group.code_agents_allowed
    return group.api_keys_allowed
