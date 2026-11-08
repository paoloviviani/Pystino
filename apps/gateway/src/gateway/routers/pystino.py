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

from collections.abc import Sequence
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request

from gateway.caller_usage import caller_group_usage, caller_limits, caller_usage
from gateway.deps import PrincipalDep, QuotaDep, SessionDep, SettingsDep
from gateway.errors import BadRequestError
from gateway.privacy import service as privacy_service
from gateway.privacy.schemas import ModelSummary, PrivacySummaryResponse
from gateway.privacy.state import state_of
from gateway.routers._metered import clean_tag
from gateway.schemas import PystinoUsageResponse, TagUsageResponse
from gateway.tag_usage import MAX_TAGS, usage_by_tag

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


@router.get("/privacy", response_model=PrivacySummaryResponse)
async def pystino_privacy(
    request: Request, principal: PrincipalDep, session: SessionDep
) -> PrivacySummaryResponse:
    """What the privacy pipelines do for this caller, per embedding and per chat model.

    ADR 0113: a client such as Cerea shows it beside the embedder choice, so the
    choice is informed up front (there is no after-the-fact "switch and
    reindex" prompt). Read-only, for the calling user, in short words ("refuses
    health data", "redacts with Default"): labels and names, never a pattern, an
    allow-list or another user's pipeline. Only the pipelines that cover this
    caller and that model count; a pipeline scoped to one API key is left out
    because it does not apply to every call.
    """
    embedding, chat = await privacy_service.summary_for(
        session,
        state_of(request),
        principal.user,
        principal.billing_group.id if principal.billing_group else None,
    )

    def shown(rows: Sequence[tuple[object, list[str], list[str]]], kind: str) -> list[ModelSummary]:
        return [
            ModelSummary(
                model=str(getattr(m, "name", "")), kind=kind, summary=words, pipelines=names
            )
            for m, words, names in rows
        ]

    return PrivacySummaryResponse(embedding=shown(embedding, "embedding"), chat=shown(chat, "chat"))


def _valid_tags(tags: list[str]) -> list[str]:
    cleaned = [clean_tag(t) for t in tags]
    if not tags or len(tags) > MAX_TAGS or any(t is None for t in cleaned):
        raise BadRequestError(f"Give between 1 and {MAX_TAGS} valid tags.")
    return [t for t in cleaned if t is not None]


@router.get("/usage/tags", response_model=TagUsageResponse)
async def pystino_usage_by_tag(
    principal: PrincipalDep,
    session: SessionDep,
    settings: SettingsDep,
    tag: Annotated[list[str], Query()],
    since: datetime | None = None,
    until: datetime | None = None,
) -> TagUsageResponse:
    """What the gateway metered for the caller's requests that carried these tags.

    A tag is the ``x-pystino-tag`` header a client put on its requests. Only the
    caller's own requests are ever read: the filter on the user is not a
    parameter, so another user's tag of the same name returns nothing. Gateway
    usage only (bring-your-own-key rows and requests still running are left
    out), the figure a quota counts.
    """
    rows = await usage_by_tag(
        session,
        user_id=principal.user.id,
        tags=_valid_tags(tag),
        currency=settings.billing_currency,
        since=since,
        until=until,
    )
    return TagUsageResponse(tags=rows)
