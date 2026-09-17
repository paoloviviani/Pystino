"""Gateway-specific ``/v1`` capabilities, namespaced off the OpenAI-standard paths.

ADR 0074. A caller's quota rules and spend have existed since Phase 2, but only
under ``/api/me/*`` — behind a session cookie (``get_management_user``), which
refuses a bearer credential outright. That is unreachable for the chat, which
may be on a different origin, and for any programmatic ``/v1`` caller: a
program holding a ``gwk_`` key has no cookie to present and learns its ceiling
only by being refused.

Usage reporting on ``/v1`` was deferred once already (2026-09-04), and this
does not reopen that decision — the deferral was about mimicking OpenAI's
usage *shape*, which does not fit five-scope, cost-and-token quotas with no
standard header for "remaining". ``/v1/pystino/usage`` is deliberately the
gateway's own shape, under a path no OpenAI-compatible client will ever probe
by convention, so a figure here is never mistaken for that standard one.

The one route so far returns everything `/api/me/limits`, `/api/me/usage` and
`/api/me/usage/groups` separately answer, as one document — a client asking
"where do I stand" gets it in one round trip rather than three, and the three
queries live in `caller_usage.py` so this and the console read identical
numbers.
"""

from __future__ import annotations

from fastapi import APIRouter

from gateway.caller_usage import caller_group_usage, caller_limits, caller_usage
from gateway.deps import PrincipalDep, QuotaDep, SessionDep, SettingsDep
from gateway.schemas import PystinoUsageResponse

router = APIRouter(prefix="/v1/pystino", tags=["pystino"])


@router.get("/usage", response_model=PystinoUsageResponse)
async def pystino_usage(
    principal: PrincipalDep,
    session: SessionDep,
    quota: QuotaDep,
    settings: SettingsDep,
) -> PystinoUsageResponse:
    """The caller's own quota rules and spend, whichever credential holds it.

    An API key and an OIDC access token both resolve to a `Principal` wrapping
    the same `User` row memberships already loaded (authenticating either
    needed them to resolve the billing group), so `user.group_ids()` is the
    caller's *effective* memberships (ADR 0057) regardless of which credential
    is presented — never the token's `groups` claim, which a hand-granted
    group appears in no token of.

    An issued key answers for its own owner, same as `/v1/me` and
    `/v1/billing/groups`: identity is not the privileged part of this
    document, only `is_admin` ever needed hiding from a key (ADR 0065), and
    this route reports none.
    """
    user = principal.user
    group_ids = user.group_ids()
    limits = await caller_limits(session, quota, user_id=user.id, group_ids=group_ids)
    usage = await caller_usage(session, user_id=user.id, settings=settings)
    groups = await caller_group_usage(session, group_ids=group_ids, settings=settings)
    return PystinoUsageResponse(limits=limits, usage=usage, groups=groups)
