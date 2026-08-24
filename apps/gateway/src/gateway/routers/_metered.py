"""The metering every ``/v1`` route shares.

Five surfaces now bill through the same ledger: chat completions, embeddings,
responses, Anthropic messages and image generation. What differs between them
is the request shape, the usage shape and the streaming protocol. What must
*not* differ is the ordering — resolve, check access, reserve, record, call,
settle — because that ordering is the only thing standing between a caller and
an unmetered request.

So it lives here once. A route supplies the protocol-specific parts and calls
these in order; it does not get to reinvent when the reservation happens or
whether a refused upstream releases or settles.

**Every exit path settles; none of them releases.** The two routes that existed
before this module disagreed on that — the chat route settled a failed request
and the embeddings route released it — and the disagreement was invisible
because for a failure with no output the two are the same thing. They are not
the same thing in general: ``settle`` charges whatever was actually used and
``release`` charges zero, so releasing after a provider failed *mid-generation*
would hand back tokens the provider had already billed us for. Settling is
correct in both cases, so it is the only one used here, and the recorder
decides what "actually used" means — it already returns zero for a request that
failed before producing anything.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.access import accessible_model_by_name
from gateway.accounting import RequestAccounting, RequestContext, TokenCounts
from gateway.accounting.cost import CostBreakdown, CurrencyMismatch, compute_cost, select_price
from gateway.accounting.tokens import TokenEstimator
from gateway.config import Settings
from gateway.deps import Principal
from gateway.errors import (
    BadRequestError,
    ModelNotFoundError,
    ServiceUnavailableError,
    UpstreamUnavailableError,
    error_response,
)
from gateway.models import ApiSurface, ModelDef, ModelKind, UsageStatus
from gateway.providers import ProviderConfigurationError, ProviderRegistry
from gateway.quota import (
    QuotaAmounts,
    QuotaEngine,
    QuotaExceeded,
    QuotaUnavailable,
    Reservation,
)
from gateway.redaction import RedactionOutcome
from gateway.upstream import OpenAICompatibleUpstream

logger = logging.getLogger(__name__)

#: Which model kinds each surface will serve, and the route to send someone to
#: when they get it wrong. Held as data rather than as an ``if`` per route so
#: that adding a surface cannot quietly skip the check.
_SURFACE_KINDS: dict[ApiSurface, tuple[ModelKind, ...]] = {
    ApiSurface.CHAT_COMPLETIONS: (ModelKind.CHAT,),
    # The Responses and Messages surfaces are alternative shapes for the same
    # conversational models, not a different kind of model.
    ApiSurface.RESPONSES: (ModelKind.CHAT,),
    ApiSurface.MESSAGES: (ModelKind.CHAT,),
    ApiSurface.EMBEDDINGS: (ModelKind.EMBEDDING,),
    ApiSurface.IMAGES: (ModelKind.IMAGE,),
}

_KIND_ROUTES: dict[ModelKind, str] = {
    ModelKind.CHAT: "/v1/chat/completions, /v1/responses or /v1/messages",
    ModelKind.EMBEDDING: "/v1/embeddings",
    ModelKind.IMAGE: "/v1/images/generations",
}


async def resolve_model(
    session: AsyncSession,
    name: str,
    *,
    principal: Principal,
    surface: ApiSurface,
) -> ModelDef:
    """The model this caller may use on this surface, or a 404/400 explaining why.

    "Not available to you" and "does not exist" deliberately answer the same
    404: which models another group can reach is not this caller's business.
    A model that exists and is the wrong *kind* is a 400 instead — that is a
    mistake the caller can fix, so the answer names the route that would have
    worked rather than forwarding the request to fail upstream with a
    provider-specific error nobody can act on.
    """
    stmt = accessible_model_by_name(
        name, user_id=principal.user.id, group_ids=[principal.billing_group.id]
    )
    model = (await session.execute(stmt)).scalars().first()
    if model is None:
        raise ModelNotFoundError(
            f"The model {name!r} does not exist or you do not have access to it."
        )

    allowed = _SURFACE_KINDS[surface]
    if model.kind not in allowed:
        raise BadRequestError(
            f"{model.name!r} is a{'n' if model.kind.value[0] in 'aeiou' else ''} "
            f"{model.kind.value} model. Use {_KIND_ROUTES[model.kind]} for it.",
            code="wrong_model_kind",
        )
    return model


async def resolve_upstream(
    providers: ProviderRegistry, model: ModelDef
) -> OpenAICompatibleUpstream:
    """That model's provider, with that provider's credentials and pool.

    Surfaces here, before any accounting row exists, so a misconfigured
    provider never leaves an orphaned in-progress row behind.
    """
    try:
        return await providers.upstream_for(model.provider)
    except ProviderConfigurationError as exc:
        logger.error("provider misconfigured for model %s: %s", model.name, exc)
        raise UpstreamUnavailableError(str(exc)) from exc


def estimate_cost(model: ModelDef, counts: TokenCounts, *, currency: str) -> CostBreakdown:
    price = select_price(list(model.prices))
    try:
        return compute_cost(counts, price, billing_currency=currency, model_name=model.name)
    except CurrencyMismatch as exc:
        # Refuse rather than bill in the wrong currency.
        raise BadRequestError(str(exc), code="price_currency_mismatch") from exc


@dataclass(slots=True)
class Metered:
    """One request's reservation and its usage row, held together.

    They are one object because they have to be released or settled together:
    a reservation that outlives its row silently blocks a budget, and a row
    finalised without settling leaves the counter high until the window rolls.
    """

    accounting: RequestAccounting
    reservation: Reservation
    quota: QuotaEngine

    async def upstream_unreachable(self, exc: Exception) -> UpstreamUnavailableError:
        """Record the failure, settle at the true cost, and build the 503.

        Nothing was generated, so the actuals are zero and the reservation is
        handed back in full — but by the same path as every other outcome.
        """
        actuals = await self.accounting.finalise(
            status=UsageStatus.UPSTREAM_ERROR,
            error_code="upstream_unreachable",
            error_message=str(exc),
        )
        await self.quota.settle(self.reservation, actuals)
        return UpstreamUnavailableError("The upstream provider could not be reached.")

    async def upstream_refused(
        self, *, status_code: int, message: str | None, content: Any
    ) -> JSONResponse:
        """Record a non-2xx, settle, and pass the provider's error through.

        Passed through rather than rewritten because clients act on these: a
        provider's own "context length exceeded" is more useful than any
        summary of it this gateway could invent.
        """
        actuals = await self.accounting.finalise(
            status=UsageStatus.UPSTREAM_ERROR,
            upstream_status=status_code,
            error_code="upstream_error",
            error_message=message,
        )
        await self.quota.settle(self.reservation, actuals)
        return JSONResponse(status_code=status_code, content=content if content is not None else {})

    async def completed(self, *, upstream_status: int | None = None) -> QuotaAmounts:
        actuals = await self.accounting.finalise(
            status=UsageStatus.COMPLETED, upstream_status=upstream_status
        )
        await self.quota.settle(self.reservation, actuals)
        return actuals


async def begin(
    request: Request,
    *,
    session: AsyncSession,
    principal: Principal,
    settings: Settings,
    quota: QuotaEngine,
    estimator: TokenEstimator,
    model: ModelDef,
    surface: ApiSurface,
    request_id: str,
    worst_case: TokenCounts,
    outcome: RedactionOutcome | None = None,
    streamed: bool = False,
) -> Metered | JSONResponse:
    """Reserve the worst case, then open the usage row. In that order.

    Reserving first means a caller over budget is refused before any money is
    spent. Opening the row second — but still before the upstream call — means
    a client that vanishes mid-generation still leaves a record of what it
    used.

    Returns the 429 response body instead of raising it, because a quota
    refusal is a normal answer with a ``retry-after`` header on it, not an
    exception.
    """
    worst_case_cost = estimate_cost(model, worst_case, currency=settings.billing_currency)

    try:
        reservation = await quota.check_and_reserve(
            session,
            principal.quota_subject,
            QuotaAmounts(tokens=Decimal(worst_case.total), cost=worst_case_cost.total),
        )
    except QuotaExceeded as exc:
        logger.info("quota refusal for user=%s: %s", principal.user.id, exc)
        return error_response(
            f"Quota exceeded. {exc}",
            status_code=429,
            type_="insufficient_quota",
            code="quota_exceeded",
            headers={"retry-after": str(exc.retry_after_seconds)},
        )
    except QuotaUnavailable as exc:
        raise ServiceUnavailableError(
            "Quota state is temporarily unavailable, so the request was refused "
            "rather than served unmetered."
        ) from exc

    accounting = RequestAccounting(
        context=RequestContext(
            request_id=request_id,
            model_name=model.name,
            currency=settings.billing_currency,
            streamed=streamed,
            user_id=principal.user.id,
            group_id=principal.billing_group.id,
            api_key_id=principal.api_key.id if principal.api_key else None,
            model_id=model.id,
            estimated_prompt_tokens=worst_case.prompt,
            redaction_engine=outcome.engine if outcome else None,
            redacted_entity_count=outcome.entity_count if outcome else 0,
            surface=surface,
            upstream_cost_unit=model.provider.upstream_cost_unit,
            plugin=model.provider.plugin,
            billing_mode=model.provider.billing_mode.value,
        ),
        session_factory=request.app.state.session_factory,
        settings=settings,
        estimator=estimator,
        model=model,
    )
    await accounting.begin()
    return Metered(accounting=accounting, reservation=reservation, quota=quota)


def request_id_for(request: Request, settings: Settings) -> str:
    return request.headers.get(settings.request_id_header) or uuid.uuid4().hex


def error_text(payload: Any) -> str | None:
    """A provider's error message, if it put one where we can find it."""
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or "")[:500] or None
    return str(error or "")[:500] or None
