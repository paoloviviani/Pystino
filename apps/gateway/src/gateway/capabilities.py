"""Who holds which capability (ADR 0096).

Two capabilities gate surfaces a deployment may reserve for flagged groups:
``code_agents`` (the /code panel and the machine links that serve it) and
``api_keys`` (minting and using ``gwk_`` keys). Each has a deployment
setting — ``everyone`` or ``groups`` — folded over the environment exactly
like the OIDC policy (``oidc_config`` row over ``GATEWAY_ACCESS__*``), and a
per-group boolean granting it.

Pure, no I/O: these functions read the user and its memberships the caller
already loaded, so the bearer path's steady state costs no query —
``test_query_counts.py`` pins that. The rule is deliberately the join
gate's (ADR 0095): ``everyone``, or an administrator (so a misconfiguration
cannot lock the people who fix it out), or an active membership in an active
flagged group. A membership's *source* does not matter — a directory grant
counts as much as a hand grant — because a capability flag is a property of
the group, not of who vouched for the membership.
"""

from __future__ import annotations

from dataclasses import dataclass

from gateway.models import Group, User
from gateway.oidc_policy import CAPABILITY_MODES, OIDCPolicy


@dataclass(frozen=True, slots=True)
class Capabilities:
    """The two ADR 0096 capabilities a person effectively holds."""

    code_agents: bool
    api_keys: bool


def effective_capabilities(user: User, policy: OIDCPolicy) -> Capabilities:
    """The capabilities in force for one person, per the folded policy.

    ``policy.code_agents_mode`` / ``policy.api_keys_mode`` are already the
    row-over-env answer (``everyone`` or ``groups``), so this function only
    consults them and the memberships authentication already loaded.
    """
    return Capabilities(
        code_agents=_holds(user, policy.code_agents_mode, "code_agents"),
        api_keys=_holds(user, policy.api_keys_mode, "api_keys"),
    )


def _holds(user: User, mode: str, flag_name: str) -> bool:
    if mode not in CAPABILITY_MODES:
        mode = "everyone"
    if not user.is_active:
        return False
    if mode == "everyone" or user.is_admin:
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
