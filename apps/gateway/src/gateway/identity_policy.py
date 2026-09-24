"""What an identity provider can do, and who decides groups and admin (ADR 0088).

OIDC itself has no listing API, so "sync users from the IdP" means different
things per directory: Keycloak has an admin REST API, Authelia a users file,
Entra/Okta/Authentik push over SCIM, and a generic issuer offers nothing beyond
the claims in a login. `kind` names the directory; its capabilities decide
which controls the console shows, and which sync adapters a provider may use.

Three orthogonal answers per provider, each with a safe default:

- **group_source** — *where* the directory's group answer comes from:
  ``claim`` (the token, at login), ``directory`` (the mirror a sync adapter
  fills), or ``none`` (the console is the only authority). *How often* it
  applies is the existing ``group_sync`` (every login, first login, never).
- **admin_source** — ``console`` (ADR 0069's default: the directory never
  administers) or ``claim``: admin iff ``admin_claim`` carries one of
  ``admin_values``, refreshed on the same schedule as groups. Provenance on
  the user row (``users.admin_source``) means the directory only ever revokes
  an admin flag *it* granted, and a revocation that would leave no active
  administrator is refused.
- **sync_adapter** — none, ``authelia_file``, ``keycloak_admin`` or ``scim``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from gateway.oidc import normalise_groups, resolve_claim

KINDS = ("generic", "authelia", "keycloak", "entra", "okta", "authentik", "google")
GROUP_SOURCES = ("claim", "directory", "none")
ADMIN_SOURCES = ("console", "claim")
SYNC_ADAPTERS = ("none", "authelia_file", "keycloak_admin", "scim")
DEPROVISION = ("disable", "ignore")


@dataclass(frozen=True)
class Capabilities:
    #: A groups claim can be read from the token at login.
    claims_groups: bool
    #: Adapters that can list users and groups (pull), for this kind.
    pull_adapters: tuple[str, ...]
    #: The directory can push users and groups to us over SCIM 2.0.
    scim_push: bool
    #: A pulled person can be keyed (issuer, subject) before their first login.
    #: False for Authelia, whose `sub` is an opaque id minted at first login.
    subject_before_login: bool
    #: The directory can say someone is gone (disabled, deleted).
    deprovision: bool

    @property
    def adapters(self) -> tuple[str, ...]:
        return (*self.pull_adapters, *(("scim",) if self.scim_push else ()))

    def as_dict(self) -> dict[str, Any]:
        return {
            "claims_groups": self.claims_groups,
            "pull_adapters": list(self.pull_adapters),
            "scim_push": self.scim_push,
            "subject_before_login": self.subject_before_login,
            "deprovision": self.deprovision,
            "adapters": list(self.adapters),
        }


_CAPABILITIES: dict[str, Capabilities] = {
    "authelia": Capabilities(True, ("authelia_file",), False, False, True),
    "keycloak": Capabilities(True, ("keycloak_admin",), False, True, True),
    "entra": Capabilities(True, (), True, True, True),
    "okta": Capabilities(True, (), True, True, True),
    "authentik": Capabilities(True, (), True, True, True),
    # No groups claim: groups come from the console (or a future Directory
    # API adapter).
    "google": Capabilities(False, (), False, True, False),
    # OIDC has no listing API. A generic issuer that happens to speak SCIM can
    # still push; everything else is just-in-time at login.
    "generic": Capabilities(True, (), True, False, False),
}


def capabilities(kind: str) -> Capabilities:
    return _CAPABILITIES.get(kind, _CAPABILITIES["generic"])


class PolicyError(ValueError):
    """A provider configuration its kind cannot honour."""


def validate(
    kind: str, group_source: str, admin_source: str, sync_adapter: str, deprovision: str
) -> None:
    if kind not in KINDS:
        raise PolicyError(f"kind must be one of {', '.join(KINDS)}")
    if group_source not in GROUP_SOURCES:
        raise PolicyError(f"group_source must be one of {', '.join(GROUP_SOURCES)}")
    if admin_source not in ADMIN_SOURCES:
        raise PolicyError(f"admin_source must be one of {', '.join(ADMIN_SOURCES)}")
    if deprovision not in DEPROVISION:
        raise PolicyError(f"sync_deprovision must be one of {', '.join(DEPROVISION)}")
    caps = capabilities(kind)
    if sync_adapter not in ("none", *caps.adapters):
        raise PolicyError(
            f"a {kind} provider cannot use the {sync_adapter!r} adapter "
            f"(available: {', '.join(caps.adapters) or 'none'})"
        )
    if group_source == "directory" and sync_adapter == "none":
        raise PolicyError("group_source 'directory' needs a sync adapter to fill the directory")
    if group_source == "claim" and not caps.claims_groups:
        raise PolicyError(f"a {kind} provider sends no groups claim; use 'directory' or 'none'")


@dataclass(frozen=True)
class AdminRule:
    """``admin_source=claim``: which claim, and which values confer admin."""

    claim: str
    values: frozenset[str]

    def matches(self, claims: Mapping[str, Any], mappings: Mapping[str, str] | None = None) -> bool:
        """Does this token say administrator?

        Values are compared both as the directory spells them and as they are
        called here after the provider's group mappings (ADR 0048), so a rule
        written in either vocabulary works.
        """
        raw = normalise_groups(resolve_claim(dict(claims), self.claim))
        mapped = {(mappings or {}).get(value, value) for value in raw}
        return bool(self.values & (set(raw) | mapped))


def admin_rule(
    admin_source: str, admin_claim: str, admin_values: Iterable[str]
) -> AdminRule | None:
    if admin_source != "claim":
        return None
    values = frozenset(v.strip() for v in admin_values if v and v.strip())
    return AdminRule(claim=admin_claim or "groups", values=values)
