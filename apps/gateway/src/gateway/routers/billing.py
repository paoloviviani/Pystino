"""Which groups may this caller bill, and which one pays by default.

ADR 0061. One endpoint, and it exists because of a door problem rather than a
missing feature: `GET /api/me` has answered this since the console shipped, but
every `/api` route is behind `get_management_user`, which reads a session
cookie and nothing else. A chat client authenticating with an OIDC access token
cannot present one, so it could set `x-bill-to` but never discover what to put
in it.

**Why not read the token's `groups` claim**, which such a client already holds
and which would cost the gateway nothing. Because a claim is what the
*directory* believes, and memberships here are not that: `memberships.source`
distinguishes `oidc` from `manual`, and a group an administrator granted by
hand appears in no token (ADR 0057). Deriving the list from the claim would
hide exactly the group that was just granted, which is the bug that ADR
recorded for `is_admin`, the default billing group and the sole-group rule —
all three read effective memberships now, and so does this.

**What this deliberately does not report.** No spend, no quota, no remaining
allowance. That keeps it clear of the two problems recorded against usage
reporting on `/v1`: "remaining" is the minimum over five scopes of cost- and
token-based rules, and cost has no standard header, so a figure here shaped
like OpenAI's and meaning something subtly different would be worse than none.
Group names a caller is already a member of carry no such ambiguity — this is
the safe slice of that deferred work, not a down payment on the rest.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from gateway.deps import PrincipalDep
from gateway.schemas import GroupSummary

router = APIRouter(prefix="/v1", tags=["pystino"])


class BillableGroup(GroupSummary):
    """A group this caller may bill, and whether it is the one that pays now."""

    is_default: bool


class BillableGroups(BaseModel):
    """The caller's billable groups.

    ``object`` and ``data`` mirror every other ``/v1`` listing so a client
    already parsing `/v1/models` needs no second shape. ``billing_group`` is
    what *this* request was billed to, which is not always the default: a
    request carrying ``x-bill-to`` reports the group it asked for, so a client
    can confirm the header took effect rather than infer it.
    """

    object: str = "list"
    data: list[BillableGroup]
    billing_group: str


@router.get("/billing/groups", response_model=BillableGroups)
async def list_billable_groups(principal: PrincipalDep) -> BillableGroups:
    # No query. The principal already carries the user with memberships loaded
    # — authentication needed them to resolve the billing group at all — so
    # asking the database again would add a round trip to the budget
    # `test_query_counts.py` pins for no new information.
    default_id = principal.user.default_billing_group_id
    groups = [
        BillableGroup(
            id=group.id,
            name=group.name,
            description=group.description,
            is_default=group.id == default_id,
        )
        for membership in principal.user.memberships
        # A disabled group is not billable, and `resolve_billing_group` refuses
        # it — so listing it would offer a choice that fails on use.
        if (group := membership.group) is not None and group.is_active
    ]
    groups.sort(key=lambda g: g.name)
    return BillableGroups(data=groups, billing_group=principal.billing_group.name)
