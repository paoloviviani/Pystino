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
from starlette.background import BackgroundTask

from gateway.access import accessible_model_by_name
from gateway.accounting import RequestAccounting, RequestContext, TokenCounts
from gateway.accounting.cost import CostBreakdown, compute_cost, select_price
from gateway.accounting.tokens import TokenEstimator
from gateway.config import EffectivePolicy, Settings
from gateway.deps import Principal
from gateway.errors import (
    BadRequestError,
    ContentBlockedError,
    ModelNotFoundError,
    ServiceUnavailableError,
    UpstreamUnavailableError,
    error_response,
)
from gateway.fx import FXService
from gateway.models import ApiSurface, ModelDef, ModelKind, UsageRecord, UsageSource, UsageStatus
from gateway.providers import ProviderConfigurationError, ProviderRegistry
from gateway.quota import (
    QuotaAmounts,
    QuotaEngine,
    QuotaExceeded,
    QuotaUnavailable,
    Reservation,
)
from gateway.redaction import RedactionOutcome, Redactor
from gateway.redaction.resolver import RedactionResolver
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
    ApiSurface.OCR: (ModelKind.OCR,),
}

_KIND_ROUTES: dict[ModelKind, str] = {
    ModelKind.CHAT: "/v1/chat/completions, /v1/responses or /v1/messages",
    ModelKind.EMBEDDING: "/v1/embeddings",
    ModelKind.IMAGE: "/v1/images/generations",
    ModelKind.OCR: "/v1/ocr",
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


async def estimate_cost(
    model: ModelDef,
    counts: TokenCounts,
    *,
    currency: str,
    fx: FXService | None = None,
) -> CostBreakdown:
    """The worst-case cost of the request, in the **billing currency**.

    The quota engine compares this against limits set in the billing currency,
    so a price in a foreign currency converts here — the one decision point
    ADR 0054 allows conversion at besides settle. No rate (API down, nothing
    stored yet) refuses the request rather than serving it unmetered: a cost
    ceiling that silently stops applying because the rate service had a bad
    day is worse than an explicit outage.
    """
    price = select_price(list(model.prices))
    if price is None:
        return CostBreakdown.zero(currency)
    breakdown = compute_cost(counts, price, fallback_currency=currency)
    if breakdown.currency.upper() != currency.upper():
        rate = await fx.rate(breakdown.currency, currency) if fx is not None else None
        if rate is None:
            raise BadRequestError(
                f"model {model.name!r} is priced in {breakdown.currency} and no "
                f"{breakdown.currency}->{currency} exchange rate is available; try "
                "again shortly or price it in the billing currency",
                code="fx_rate_unavailable",
            )
        return breakdown.scaled(rate.rate, currency)
    return breakdown


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

    def shape_payload(self, payload: dict[str, Any], *, surface: ApiSurface) -> dict[str, Any]:
        """Let the counterparty's plugin adjust the body before it is sent.

        Called by every ``/v1`` route rather than by the one that happened to
        need it, so the seam is not a trap: this replaced
        ``providers.forward_stream_options``, which lived in the chat route
        alone, and the next thing a counterparty needs adding to a body will
        have somewhere to go without another column.

        Reuses the plugin the recorder already resolved, so a request cannot
        shape its body with one plugin and read its usage with another.
        """
        return self.accounting.plugin.prepare_payload(payload, surface=surface)

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
        return JSONResponse(
            status_code=status_code, content=content if content is not None else {}
        )

    async def completed(self, *, upstream_status: int | None = None) -> QuotaAmounts:
        actuals = await self.accounting.finalise(
            status=UsageStatus.COMPLETED, upstream_status=upstream_status
        )
        await self.quota.settle(self.reservation, actuals)
        return actuals

    def completed_after_response(
        self, *, upstream_status: int | None = None
    ) -> BackgroundTask:
        """The same work, run once the answer is already on its way out.

        Attach to the response instead of awaiting: a Starlette background task
        runs *after* the last byte is sent, so the caller no longer waits for a
        ``UPDATE usage_records`` and a counter settle it has no use for. On a
        ~26ms request that is the couple of milliseconds those two round trips
        cost, and rather more at p95, where they queue behind everything else.

        Three things make this safe rather than merely faster.

        **The row already exists.** It was inserted before the upstream call,
        so a request in this window is visible as ``in_progress`` — which
        reporting already counts separately and discloses as "still in flight
        and excluded" — rather than absent.

        **Nothing downstream reads the return value.** Every caller ignored the
        ``QuotaAmounts`` this returns; the reservation is settled against the
        counter store, not handed back to the route.

        **A failure is logged, not raised.** By the time this runs the caller
        has a 200 and the upstream has served them, so raising would report a
        failure for a request that succeeded and bill them for it anyway. What
        it costs is that a settle which fails leaves the row ``in_progress``
        instead of returning a 500 — visible in the ledger and in the log,
        which is the better of two bad answers.

        Streaming is deliberately **not** routed through here: its
        finalisation already runs in the body iterator's ``finally``, and the
        open cancellation bug there needs ``spawn_finalisation`` and its own
        tests (see CLAUDE.md), not a different response class.
        """
        return BackgroundTask(self._settle_quietly, upstream_status)

    async def _settle_quietly(self, upstream_status: int | None) -> None:
        try:
            await self.completed(upstream_status=upstream_status)
        except Exception:
            logger.exception(
                "failed to finalise request %s after the response was sent; its row "
                "stays in_progress",
                self.accounting.request_id,
            )



@dataclass(frozen=True, slots=True)
class WebSearchBound:
    """How many provider-side web searches this request may be charged for.

    Server-side search is billed **per search, on top of tokens** — $10 per
    1,000 for Anthropic — so a request that asks for it has a cost component
    that no token count implies and no `max_tokens` bounds (ADR 0058).

    Two fields because there are two honest answers to "is this a ceiling".
    ``reserved`` is what the quota engine holds. ``enforced`` says whether the
    same number was written into the outgoing tool definition, so the
    counterparty will refuse the search after it. Where it can be, it is: a
    number we reserve against but do not enforce is a figure that looks like a
    ceiling and is not one, which is the shape the OCR surface is stuck with
    (a document's page count has no cap field to write into) and this surface
    is not.
    """

    reserved: int
    enforced: bool
    default: int

    def apply(self, payload: dict[str, Any]) -> None:
        """Write the cap into the tools of *payload*, where the tool takes one.

        Called on the payload rather than on the request body because the
        payload is a dump — mutating the body's copy would change nothing that
        gets sent. Both this and ``reserved`` come out of the same detection
        over the same tools list, which is what stops the number we reserve and
        the number we enforce from drifting apart.
        """
        for tool in _search_tools(payload.get("tools")):
            if _declared_cap(tool) is None and _takes_a_cap(tool):
                tool["max_uses"] = self.default


def _search_tools(tools: Any) -> list[dict[str, Any]]:
    """The server-side search tools in a request's ``tools`` array.

    Matches ``web_search`` and Anthropic's date-versioned
    ``web_search_20250305`` family. Deliberately not OpenRouter's
    ``openrouter:web_search``: that is their namespace for a tool *they*
    execute, and nothing here executes a search.
    """
    if not isinstance(tools, list):
        return []
    found: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        kind = tool.get("type")
        if isinstance(kind, str) and (kind == "web_search" or kind.startswith("web_search_")):
            found.append(tool)
    return found


def _declared_cap(tool: dict[str, Any]) -> int | None:
    cap = tool.get("max_uses")
    return cap if isinstance(cap, int) and cap > 0 else None


def _takes_a_cap(tool: dict[str, Any]) -> bool:
    """Whether ``max_uses`` is a field this tool definition actually has.

    Only the date-versioned Anthropic family. Writing it into OpenAI's
    ``web_search`` tool — whose options are ``search_context_size``,
    ``filters`` and friends — risks a 400 on a request that would otherwise
    have worked, and refusing somebody's request to protect a reservation is
    the wrong trade.
    """
    return str(tool.get("type", "")).startswith("web_search_")


def bound_web_search(tools: Any, *, default: int) -> WebSearchBound:
    """Read the search cap out of a request, filling in the deployment's.

    A request that does not ask for search reserves nothing: this must not
    put a cost on every ordinary completion.
    """
    found = _search_tools(tools)
    if not found:
        return WebSearchBound(reserved=0, enforced=True, default=default)

    reserved = 0
    enforced = True
    for tool in found:
        if (cap := _declared_cap(tool)) is not None:
            # The caller bounded it themselves, and the provider will hold them
            # to it. Nothing to write and nothing to guess.
            reserved += cap
        else:
            reserved += default
            enforced = enforced and _takes_a_cap(tool)
    return WebSearchBound(reserved=reserved, enforced=enforced, default=default)


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
    worst_case_cost = await estimate_cost(
        model, worst_case, currency=settings.billing_currency, fx=request.app.state.fx
    )

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
            redaction_scope=outcome.scope if outcome else None,
            redaction_rule_id=outcome.rule_id if outcome else None,
            surface=surface,
            plugin=model.provider.plugin,
            billing_mode=model.provider.billing_mode.value,
        ),
        session_factory=request.app.state.session_factory,
        settings=settings,
        fx=request.app.state.fx,
        estimator=estimator,
        model=model,
    )
    await accounting.begin()
    return Metered(accounting=accounting, reservation=reservation, quota=quota)


def redaction_policy(
    request: Request, *, principal: Principal, model: ModelDef | None = None
) -> EffectivePolicy | None:
    """The folded policy for this request, from state already in hand.

    Zero queries, and that is the design rather than an optimisation: every
    subject a rule can name — the model, its provider, the billing group, the
    person, the key — is already loaded by the time redaction runs, and the
    rules themselves arrive on the resolver's ten-second poll (ADR 0038). A
    lookup here would have cost a sixth select and broken the budget
    `test_query_counts.py` pins.

    None when no resolver is mounted, which is what a test harness that builds
    the app without one does; the redactor then uses its own deployment policy.
    """
    resolver: RedactionResolver | None = getattr(request.app.state, "redaction", None)
    if resolver is None:
        return None
    effective: EffectivePolicy = resolver.policy_for(
        provider_id=model.provider_id if model is not None else None,
        model_id=model.id if model is not None else None,
        group_id=principal.billing_group.id,
        user_id=principal.user.id,
        api_key_id=principal.api_key.id if principal.api_key is not None else None,
    )
    return effective


async def redact_or_block(
    request: Request,
    messages: list[dict[str, Any]],
    *,
    redactor: Redactor,
    session: AsyncSession,
    principal: Principal,
    model: ModelDef,
    settings: Settings,
    surface: ApiSurface,
    request_id: str,
) -> RedactionOutcome:
    """Redact under this request's effective policy, or refuse and record it.

    One function rather than two lines in each of five routes, because the
    recording is the part that is easy to forget: a blocked request never
    reaches ``begin``, so without this it would leave **no trace at all** — and
    "this deployment refused 400 prompts last month" is a number a
    data-protection review asks for (ADR 0038).

    The row is written before the error propagates, on the request's own session,
    and it is not a billing row: zero tokens, zero cost, ``status=blocked``. What
    it carries is who, when, which model, and which rule decided.
    """
    policy = redaction_policy(request, principal=principal, model=model)
    try:
        return await redactor.redact_request(messages, policy=policy)
    except ContentBlockedError as exc:
        session.add(
            UsageRecord(
                request_id=request_id,
                status=UsageStatus.BLOCKED,
                user_id=principal.user.id,
                group_id=principal.billing_group.id,
                api_key_id=principal.api_key.id if principal.api_key else None,
                model_id=model.id,
                model_name=model.name,
                streamed=False,
                api_surface=surface,
                currency=settings.billing_currency,
                usage_source=UsageSource.UNAVAILABLE,
                cost=Decimal(0),
                redaction_engine=getattr(redactor, "name", None),
                redaction_scope=policy.scope if policy else None,
                redaction_rule_id=policy.rule_id if policy else None,
                error_code=exc.code,
                # Safe by construction: ContentBlockedError never quotes the
                # matched text, which is the whole point of its own docstring.
                error_message=str(exc),
            )
        )
        await session.commit()
        raise


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
