"""Who may join: the gate every sign-in door and `/v1` answer to (ADR 0095).

Pure, no I/O: these functions read rows and claims the caller already loaded,
so the bearer path's steady state costs no query — `test_query_counts.py`
pins that. The callers are `provision_user` (strangers), `sign_in` (group
members on every sign-in), `sync_user_from_claims` (both return paths), and
the console session check in `deps.get_management_user`.

The admin-rule matchers live here rather than in `oidc.py` because the gate
needs them and `oidc.py` needs the gate; the other way round would be a
cycle. They are unchanged from ADR 0093 §5.
"""

from __future__ import annotations

import logging
from typing import Any

from gateway.config import OIDCSettings
from gateway.email_normalize import is_trusted_email, normalize_email
from gateway.models import MembershipSource, User
from gateway.oidc_policy import OIDCPolicy

logger = logging.getLogger(__name__)


def _matches_email_rule(claims: dict[str, Any], settings: OIDCSettings) -> bool:
    """ADR 0093 §5.1. The literal boolean, an ASCII address, and list
    membership — all three, or it does not grant."""
    allowed = settings.admin_email_list()
    if not allowed or claims.get("email_verified") is not True:
        return False
    email = claims.get("email")
    if not isinstance(email, str):
        return False
    normalized, trusted = is_trusted_email(email)
    if not trusted:
        return False
    return normalized in {normalize_email(candidate) for candidate in allowed}


def _matches_claim_rule(
    claims: dict[str, Any], settings: OIDCSettings, mappings: dict[str, str] | None
) -> bool:
    """ADR 0093 §5.2. The existing `AdminRule` shape, fed from `.env`."""
    values = settings.admin_claim_value_list()
    if not (settings.admin_claim and values):
        return False
    from gateway.identity_policy import AdminRule

    rule = AdminRule(claim=settings.admin_claim, values=frozenset(values))
    return rule.matches(claims, mappings)


def vetted_names(mappings: dict[str, str] | None, allowlist: list[str] | None) -> frozenset[str]:
    """The local names whose IdP origin is vouched for the group gate (ADR
    0095 review): explicit mapping targets, plus the allowlist. An IdP group
    namespace is user-writable — a GitLab user can create a top-level group
    named like any required local one — so a *claimed* name counts toward
    the gate only when the operator has vouched for it through one of these."""
    names: set[str] = set((mappings or {}).values())
    names.update(allowlist or [])
    return frozenset(names)


def gate_vetted_names(
    settings: OIDCSettings, policy: OIDCPolicy | None, group_source: str
) -> frozenset[str] | None:
    """The vetted set a caller should use, or `None` when vetting is off.

    Vetting applies only to a claim-source provider — the one shape where a
    membership was created from a token's group names; there the set may
    legitimately be *empty* (no mappings, no allowlist), which refuses every
    claim-sourced name. With `group_source="directory"` (or `"none"`) no
    membership was claim-sourced, so `None`: everything counts, as before.
    """
    if group_source != "claim":
        return None
    if policy is None:
        return None
    return vetted_names(policy.group_mappings, settings.group_allowlist)


def holds_required_group(
    user: User,
    required: frozenset[str],
    *,
    vetted: frozenset[str] | None = None,
) -> bool:
    """A required group the person effectively holds — active, and vouched.

    `Membership.group` is `lazy="joined"`, and every caller loads the
    memberships already, so this costs no query.

    `vetted` (ADR 0095 review) is `gate_vetted_names`' answer: not `None`, a
    *claim-sourced* (oidc) membership counts only when its group's name is in
    the set — an explicit mapping target or the allowlist — because the IdP's
    group namespace is user-writable; an empty set refuses them all. A manual
    membership (the console's, or a pre-assigned directory grant) always
    counts. `None`: every membership counts, as before — the directory-source
    case, where nothing was claim-sourced.
    """
    return any(
        membership.group.name in required
        and membership.group.is_active
        and (
            membership.source is MembershipSource.MANUAL
            or vetted is None
            or membership.group.name in vetted
        )
        for membership in user.memberships
    )


def stranger_passes(
    mapped_names: list[str],
    required: frozenset[str],
    *,
    vetted: frozenset[str] | None = None,
) -> bool:
    """A stranger's mapped claim names against the required local names.

    `vetted` as in `holds_required_group`: not `None`, an unmapped same-name
    claim is not a pass — only names in the set count (an empty set refuses
    them all). `None`: every mapped name counts, as before (the
    directory-source case).
    """
    if vetted is None:
        return any(name in required for name in mapped_names)
    return any(name in required and name in vetted for name in mapped_names)


def exempt(
    user: User | None,
    claims: dict[str, Any] | None,
    settings: OIDCSettings,
    *,
    group_mappings: dict[str, str] | None = None,
    email_rule: bool = True,
) -> bool:
    """An identity the gate stands aside for: administrators, decided day one
    so a misconfigured group mode cannot lock every admin out (ADR 0095).

    `user.is_admin` covers everyone already promoted; the env admin rules
    cover a first sign-in the promotion has not seen yet. The email rule
    needs `email_verified`'s literal boolean, which a bearer claim set may
    not carry — the `/v1` path calls with `email_rule=False` and consults
    the claim rule only. Without claims (a console session check) only the
    flag can answer.
    """
    if user is not None and user.is_admin:
        return True
    if claims is None:
        return False
    if email_rule and _matches_email_rule(claims, settings):
        return True
    return _matches_claim_rule(claims, settings, group_mappings)


_PROVIDER_LABELS = {
    "authelia": "Authelia",
    "keycloak": "Keycloak",
    "entra": "Microsoft Entra ID",
    "okta": "Okta",
    "authentik": "Authentik",
    "google": "Google",
}


def provider_label(kind: str | None) -> str:
    """The `<provider>` refusal texts name. Unknown kinds (generic, and the
    GitLab-shaped providers behind it) read as "identity provider"."""
    if kind is None:
        return "identity provider"
    return _PROVIDER_LABELS.get(kind.strip().lower(), "identity provider")


def refusal_text(mode: str, *, email: str | None, provider: str) -> str:
    """The refusal a door shows the person (Cerea renders it as it arrives).

    Group names are never listed to strangers: naming them tells a prober
    which group to ask for, or to spoof.
    """
    if mode == "invite":
        whom = email if email else "your account"
        return (
            "This deployment is invitation-only. Ask an administrator to invite "
            f"{whom}, then sign in again."
        )
    if mode == "group":
        return (
            f"Your {provider} account isn't in a group that may use this "
            "deployment. Ask an administrator which group gives access."
        )
    if mode == "group_lost":
        return (
            "Your account is no longer in a group that may use this "
            "deployment. Ask an administrator."
        )
    return (
        "Your request to join is waiting for an administrator's approval. "
        "Try again once you've been told it's approved."
    )
