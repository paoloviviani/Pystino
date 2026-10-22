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
import random
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, TypeVar, cast

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.background import BackgroundTask

from gateway.access import accessible_model_by_name, alias_targets, alias_unavailable_reason
from gateway.accounting import RequestAccounting, RequestContext, TokenCounts
from gateway.accounting.cost import CostBreakdown, compute_cost, select_price
from gateway.accounting.tokens import TokenEstimator
from gateway.catalogue import Catalogue, CatalogueFacts
from gateway.config import EffectivePolicy, Settings
from gateway.deps import Principal
from gateway.errors import (
    AliasUnavailableError,
    BadRequestError,
    ContentBlockedError,
    ModelNotFoundError,
    ServiceUnavailableError,
    UpstreamUnavailableError,
    error_response,
)
from gateway.fx import FXService
from gateway.models import (
    ApiSurface,
    ModelDef,
    ModelKind,
    Provider,
    UsageRecord,
    UsageSource,
    UsageStatus,
)
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
from gateway.routing import (
    MAX_ATTEMPTS,
    Candidate,
    SelectionContext,
    StickyHit,
    needs_write,
    recall,
    remember,
    rule_named,
    session_key_hash,
    status_falls_back,
)
from gateway.types import utcnow
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
    ApiSurface.SEARCH: (ModelKind.SEARCH,),
    ApiSurface.DECISIONS: (ModelKind.DECISION,),
}

_KIND_ROUTES: dict[ModelKind, str] = {
    ModelKind.CHAT: "/v1/chat/completions, /v1/responses or /v1/messages",
    ModelKind.EMBEDDING: "/v1/embeddings",
    ModelKind.IMAGE: "/v1/images/generations",
    ModelKind.OCR: "/v1/ocr",
    ModelKind.SEARCH: "/v1/search",
    ModelKind.DECISION: "/v1/decisions or /v1/systemone",
}


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    """What a request will actually be served by, and what the caller asked for.

    Two fields because an alias makes them different (ADR 0101): ``model`` is
    always the **target**, because everything downstream — the upstream call,
    the price, the quota, the ledger's ``model_id`` — follows what serves the
    request, while ``requested_name`` is the name the caller sent, which the
    ledger keeps beside it so a retarget cannot rewrite history.

    A frozen dataclass rather than an attribute hung on the ORM row, which was
    the tempting spelling: the row is in the identity map and the attribute
    would outlive the request that set it, so a second resolution of the same
    alias in one session would read the first one's value.
    """

    model: ModelDef
    requested_name: str
    #: Which try this is (1-based) in the plan the request was given, and
    #: whether it is the last one. A failure on a non-last attempt moves on to
    #: the next target (see `AttemptFailed`); on the last it is the caller's
    #: answer, exactly as it is for a direct call. A direct call is attempt 1 of
    #: 1, so nothing here changes for it.
    attempt: int = 1
    last: bool = True
    #: Whether this came through an alias with several targets, i.e. whether the
    #: ledger should number the attempt.
    routed: bool = False

    @property
    def via_alias(self) -> bool:
        """Whether the caller reached this model through an alias.

        Cheap enough to ask per request and the only honest way to phrase the
        two things that differ: which name goes in ``requested_model``, and
        which name a "wrong kind" error quotes back at the caller.
        """
        return self.requested_name != self.model.name

    @property
    def provider(self) -> Provider:
        """The provider that will serve this request.

        **Non-optional by construction**, which is the point of carrying the
        resolution rather than the raw row: ``ck_models_alias_or_served`` makes
        an alias's provider null and a served model's not, and resolution never
        returns an alias. Every reader that used to take a ``ModelDef`` and hope
        now has to go through here, so the null case is one check in one place
        instead of an ``AttributeError`` on a request that worked.
        """
        provider = self.model.provider
        if provider is None:  # pragma: no cover - the constraint forbids this
            raise AliasUnavailableError(
                f"The model alias {self.requested_name!r} has no available target."
            )
        return provider

    @property
    def upstream_model(self) -> str:
        """The name to send the provider — the target's, never the alias's.

        Same construction as :attr:`provider`, and the same reasoning: an alias
        has no upstream name of its own, so sending one would send ``None`` and
        let the provider reject the request with its own error, which is the
        failure mode this whole module exists to avoid.
        """
        upstream = self.model.upstream_model
        if upstream is None:  # pragma: no cover - the constraint forbids this
            raise AliasUnavailableError(
                f"The model alias {self.requested_name!r} has no available target."
            )
        return upstream


class AttemptFailed(Exception):
    """One target of a routed alias could not serve this request; try the next.

    Raised only for a **non-last** attempt, and only after that attempt's ledger
    row has been finalised and its reservation settled, so catching it leaves
    nothing open. It carries what the caller would have been given had this been
    the only target (``response`` for a pass-through or a 429, ``error`` for a
    503), which is what the planner hands on if it turns out nothing else is
    left. Never escapes `serve_with_failover`.
    """

    def __init__(
        self,
        reason: str,
        *,
        response: Response | None = None,
        error: Exception | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.response = response
        self.error = error


ResponseT = TypeVar("ResponseT", bound=Response)


@dataclass(frozen=True, slots=True)
class _Sticky:
    """What the planner learned about this session, for the write after serving."""

    alias_id: uuid.UUID
    key_hash: str
    ttl_seconds: int
    hit: StickyHit | None


@dataclass(frozen=True, slots=True)
class RoutePlan:
    """The targets to try for one request, in order (issue P1).

    One element for a direct call or an alias with a single target; up to
    `MAX_ATTEMPTS` for a routed alias. ``attempts[i].last`` is true only on the
    final one.
    """

    attempts: tuple[ResolvedModel, ...]
    sticky: _Sticky | None = None

    @property
    def first(self) -> ResolvedModel:
        return self.attempts[0]


def _wrong_kind(name: str, model: ModelDef) -> BadRequestError:
    # The name quoted back is the one the caller sent. "…is an embedding model"
    # naming a model they never asked for sends them off to fix a request that
    # was already correct.
    return BadRequestError(
        f"{name!r} is a{'n' if model.kind.value[0] in 'aeiou' else ''} "
        f"{model.kind.value} model. Use {_KIND_ROUTES[model.kind]} for it.",
        code="wrong_model_kind",
    )


def _facts_of(target: ModelDef, catalogue: Catalogue | None) -> CatalogueFacts | None:
    """The catalogue's facts about the provider behind *target*, if it is linked."""
    provider = target.provider
    if catalogue is None or provider is None:
        return None
    return catalogue.facts_for(provider.catalogue_id)


async def resolve_plan(
    session: AsyncSession,
    name: str,
    *,
    principal: Principal,
    surface: ApiSurface,
    affinity: str | None = None,
    fx: FXService | None = None,
    currency: str = "EUR",
    rng: random.Random | None = None,
    catalogue: Catalogue | None = None,
) -> RoutePlan:
    """The models to try for this caller and surface, or a 404/400/503 saying why not.

    "Not available to you" and "does not exist" deliberately answer the same
    404: which models another group can reach is not this caller's business.
    A model that exists and is the wrong *kind* is a 400 instead — that is a
    mistake the caller can fix, so the answer names the route that would have
    worked rather than forwarding the request to fail upstream with a
    provider-specific error nobody can act on.

    **Access is decided on the name the caller sent, and only that name**
    (ADR 0101). If that name is an alias, the alias row is the grant and the
    targets are loaded with no access filter at all; a caller who may use `fast`
    and not the models behind it is exactly the case the alias exists for. Each
    target is returned as a served model, so provider, upstream name, prices,
    input limits and kind all follow it.

    For an alias with several targets the order is: unavailable targets dropped
    (inactive, inactive provider, wrong kind), the alias's rule applied, then —
    when the caller sent a session key and the alias has stickiness on — the
    target that session last used moved to the front if it is still in the list.
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
    if not model.is_alias:
        if model.kind not in allowed:
            raise _wrong_kind(name, model)
        return RoutePlan(attempts=(ResolvedModel(model=model, requested_name=name),))

    rows = (await session.execute(alias_targets(model))).unique().all()
    candidates: list[Candidate] = []
    wrong_kind: ModelDef | None = None
    reasons = []
    for target, position, weight in rows:
        reason = alias_unavailable_reason(model, target)
        if reason is not None:
            reasons.append(reason)
        elif target.kind not in allowed:
            wrong_kind = wrong_kind or target
        else:
            candidates.append(
                Candidate(
                    target=target,
                    position=position,
                    weight=weight,
                    facts=_facts_of(target, catalogue),
                )
            )
    if not candidates and wrong_kind is not None:
        raise _wrong_kind(name, wrong_kind)

    rule = rule_named(model.route_rule)
    context = SelectionContext(
        alias=model,
        surface=surface,
        session_key=affinity,
        currency=currency,
        fx=fx,
        rng=rng or random.Random(),  # noqa: S311 - ordering targets, not secrets
    )
    ordered = await rule.order(candidates, context) if candidates else []
    if not ordered:
        logger.info(
            "alias %r is unavailable: %s",
            model.name,
            reasons[0] if reasons else "no target",
        )
        raise AliasUnavailableError(f"The model alias {model.name!r} has no available target.")

    # Stickiness is decided by how many targets the alias *has*, not how many
    # are up: a session pinned to B while A was down must still be on B when A
    # comes back, which is the whole point of pinning it.
    sticky: _Sticky | None = None
    if len(rows) > 1 and affinity and model.route_sticky_ttl_seconds > 0:
        key_hash = session_key_hash(principal.user.id, affinity)
        hit = await recall(session, model.id, key_hash)
        sticky = _Sticky(
            alias_id=model.id,
            key_hash=key_hash,
            ttl_seconds=model.route_sticky_ttl_seconds,
            hit=hit,
        )
        if hit is not None:
            pinned = [c for c in ordered if c.target.id == hit.target_model_id]
            if pinned:
                ordered = pinned + [c for c in ordered if c is not pinned[0]]

    chosen = [c.target for c in ordered[:MAX_ATTEMPTS]]
    routed = len(rows) > 1
    return RoutePlan(
        attempts=tuple(
            ResolvedModel(
                model=target,
                requested_name=name,
                attempt=index + 1,
                last=index == len(chosen) - 1,
                routed=routed,
            )
            for index, target in enumerate(chosen)
        ),
        sticky=sticky,
    )


async def resolve_model(
    session: AsyncSession,
    name: str,
    *,
    principal: Principal,
    surface: ApiSurface,
    affinity: str | None = None,
) -> ResolvedModel:
    """The one model this request will be served by, with no fallback.

    For the surfaces that do not retry (OCR and search): the plan's first choice,
    with stickiness honoured on the way in but never written, since nothing here
    knows the call succeeded. A routed alias on those surfaces is therefore
    served by its rule's first pick and fails if that target fails.
    """
    plan = await resolve_plan(
        session, name, principal=principal, surface=surface, affinity=affinity
    )
    first = plan.first
    return ResolvedModel(model=first.model, requested_name=first.requested_name)


async def dereference(session: AsyncSession, resolved: ResolvedModel) -> ResolvedModel:
    """Replace an alias with its first target; a served model passes straight through.

    Separate from :func:`resolve_model` because the search surface has a second
    way in — a group policy naming a backend by id rather than by the caller's
    own name — and that path needs the same dereference without re-running the
    access check it has already done by a different route. One target, no
    fallback, as for `resolve_model`.
    """
    alias = resolved.model
    if not alias.is_alias:
        return resolved
    rows = (await session.execute(alias_targets(alias))).unique().all()
    candidates = [
        Candidate(target=target, position=position, weight=weight)
        for target, position, weight in rows
        if alias_unavailable_reason(alias, target) is None
    ]
    ordered = (
        await rule_named(alias.route_rule).order(
            candidates, SelectionContext(alias=alias, surface=ApiSurface.SEARCH)
        )
        if candidates
        else []
    )
    if not ordered:
        logger.info("alias %r is unavailable", alias.name)
        raise AliasUnavailableError(f"The model alias {alias.name!r} has no available target.")
    return ResolvedModel(model=ordered[0].target, requested_name=resolved.requested_name)


async def serve_with_failover[ResponseT: Response](
    session: AsyncSession,
    plan: RoutePlan,
    serve: Callable[[ResolvedModel], Awaitable[ResponseT]],
) -> ResponseT:
    """Run ``serve`` against each target of the plan until one answers.

    ``serve`` is a route's whole post-resolution pipeline (redact, count,
    admit, record, call, shape the answer), so everything per-target — the
    redaction policy, the input limit, the price, the quota rules that name the
    target — is evaluated per attempt for free. A non-last attempt that cannot
    serve raises `AttemptFailed` *after* settling its own row and reservation;
    this moves on. The last attempt's outcome is the caller's, as it is for a
    direct call.

    A response with a status below 400 pins the session (when the plan has a
    sticky memory) to the target that produced it. A failure to write that
    memory is logged and never fails the request: stickiness is an optimisation
    on top of an answer the caller already has.
    """
    failure: AttemptFailed | None = None
    for resolved in plan.attempts:
        try:
            response = await serve(resolved)
        except AttemptFailed as failed:
            logger.info(
                "attempt %d on %s failed (%s); trying the next target",
                resolved.attempt,
                resolved.model.name,
                failed.reason,
            )
            failure = failed
            continue
        if response.status_code < 400:
            await _stick(session, plan, resolved)
        return response
    # Unreachable by construction (the last attempt never raises AttemptFailed);
    # kept so that a future change cannot turn it into a silent `None`.
    assert failure is not None
    if failure.error is not None:
        raise failure.error
    assert failure.response is not None
    return cast(ResponseT, failure.response)


async def _stick(session: AsyncSession, plan: RoutePlan, resolved: ResolvedModel) -> None:
    sticky = plan.sticky
    if sticky is None:
        return
    now = utcnow()
    if not needs_write(sticky.hit, resolved.model.id, sticky.ttl_seconds, now):
        return
    try:
        await remember(
            session,
            sticky.alias_id,
            sticky.key_hash,
            resolved.model.id,
            sticky.ttl_seconds,
            now=now,
        )
    except Exception:
        logger.exception("could not remember the session's target; continuing unpinned")


def enforce_input_limit(model: ModelDef, prompt_tokens: int) -> None:
    """Refuse a prompt over the operator-set input cap, before any accounting.

    ``max_input_tokens`` is set by hand (no catalogue reports it), so a request
    that exceeds it is rejected here with a 400 naming the limit — an error the
    caller can act on — rather than forwarded to fail upstream with whatever the
    vendor's message happens to be. Null means unknown, never zero: the check
    must be silent exactly when the operator has not recorded a limit.
    """
    if model.max_input_tokens is not None and prompt_tokens > model.max_input_tokens:
        raise BadRequestError(
            f"This prompt is about {prompt_tokens} tokens; model "
            f"{model.name!r} accepts at most {model.max_input_tokens} input tokens.",
            code="prompt_too_long",
        )


async def resolve_upstream(
    providers: ProviderRegistry, resolved: ResolvedModel
) -> OpenAICompatibleUpstream:
    """That model's provider, with that provider's credentials and pool.

    Surfaces here, before any accounting row exists, so a misconfigured
    provider never leaves an orphaned in-progress row behind.

    Takes the resolution rather than the row so that the provider is
    ``resolved.provider`` — the **target's**, for an alias — and a caller
    cannot pass an alias row here and get an ``AttributeError`` on the hot path.
    """
    try:
        return await providers.upstream_for(resolved.provider)
    except ProviderConfigurationError as exc:
        logger.error("provider misconfigured for model %s: %s", resolved.model.name, exc)
        error = UpstreamUnavailableError(str(exc))
        if not resolved.last:
            # A target we cannot even call is the operator's fault, not the
            # request's: the next one may be fine. No row exists yet, so there
            # is nothing to settle.
            raise AttemptFailed("provider_misconfigured", error=error) from exc
        raise error from exc


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
    #: True on a non-last attempt of a routed alias: a failure that is the
    #: target's, not the request's, ends *this attempt* (row finalised, quota
    #: settled, as ever) and raises `AttemptFailed` instead of answering.
    may_fall_back: bool = False
    #: For the warning a credential or balance failure logs.
    provider_name: str = ""

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
        error = UpstreamUnavailableError("The upstream provider could not be reached.")
        if self.may_fall_back:
            raise AttemptFailed("upstream_unreachable", error=error)
        return error

    async def upstream_refused(
        self,
        *,
        status_code: int,
        message: str | None,
        content: Any,
        upstream_status: int | None = None,
    ) -> JSONResponse:
        """Record a non-2xx, settle, and pass the provider's error through.

        Passed through rather than rewritten because clients act on these: a
        provider's own "context length exceeded" is more useful than any
        summary of it this gateway could invent.

        ``upstream_status`` separates the two numbers for the one case where
        they differ: the counterparty answered **2xx** with a body this
        deployment cannot read, so the caller gets a 502 and the ledger must
        still record what the counterparty actually did. Defaulting it to
        ``status_code`` keeps every other caller — where the two genuinely are
        the same number — unchanged. Writing 502 into ``upstream_status`` there
        would send whoever reconciles the bill looking for a failure on the
        vendor's side that never happened, which is the same class of mistake
        as the estimated-usage disclosure recorded in AGENTS.md.
        """
        actuals = await self.accounting.finalise(
            status=UsageStatus.UPSTREAM_ERROR,
            upstream_status=status_code if upstream_status is None else upstream_status,
            error_code="upstream_error",
            error_message=message,
        )
        await self.quota.settle(self.reservation, actuals)
        response = JSONResponse(
            status_code=status_code, content=content if content is not None else {}
        )
        if self.may_fall_back and status_falls_back(status_code, content, message):
            if status_code in (401, 402, 403):
                logger.warning(
                    "provider %r answered %d (our credential, balance or quota, not the "
                    "request); falling back to the next target",
                    self.provider_name,
                    status_code,
                )
            raise AttemptFailed(f"upstream_{status_code}", response=response)
        return response

    async def completed(self, *, upstream_status: int | None = None) -> QuotaAmounts:
        actuals = await self.accounting.finalise(
            status=UsageStatus.COMPLETED, upstream_status=upstream_status
        )
        await self.quota.settle(self.reservation, actuals)
        return actuals

    def completed_after_response(self, *, upstream_status: int | None = None) -> BackgroundTask:
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
        tests (see AGENTS.md), not a different response class.
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
    *,
    # `fx` and `session_factory` rather than the `Request` these used to be read
    # from. Nothing else in this function ever touched the request, and taking
    # them explicitly is what lets knowledge-base ingestion — which has no
    # request, because extraction and embedding belong in a background task
    # rather than a request path (ADR 0019) — reserve and record through *this*
    # pipeline instead of a second copy of it. A second copy of money code is
    # the one duplication this project cannot afford.
    fx: FXService,
    session_factory: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    principal: Principal,
    settings: Settings,
    quota: QuotaEngine,
    estimator: TokenEstimator,
    resolved: ResolvedModel,
    surface: ApiSurface,
    request_id: str,
    worst_case: TokenCounts,
    # The most searches this request may run against a backend of our own
    # (docs/web-search-plan.md, phase 2). A separate argument rather than a
    # field on `worst_case`, because `TokenCounts` is the structure
    # `compute_cost` multiplies by a rate and this quantity deliberately has
    # none — see `LimitMetric.OWN_SEARCH_REQUESTS`.
    #
    # Zero by default, and every surface passes zero today: the search backends
    # are not built. The path is here so that the surface which does build them
    # reserves through *this* function, rather than discovering at that point
    # that the shared pipeline cannot carry its unit and writing a second one.
    worst_case_own_searches: int = 0,
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
    # The **target** (ADR 0101): admission and the reservation are priced by
    # what serves the request, and rules that name the target apply, exactly as
    # if the target had been called.
    model = resolved.model
    worst_case_cost = await estimate_cost(
        model, worst_case, currency=settings.billing_currency, fx=fx
    )

    try:
        reservation = await quota.check_and_reserve(
            session,
            principal.quota_subject,
            QuotaAmounts(
                tokens=Decimal(worst_case.total),
                cost=worst_case_cost.total,
                own_search_requests=Decimal(worst_case_own_searches),
            ),
        )
    except QuotaExceeded as exc:
        logger.info("quota refusal for user=%s: %s", principal.user.id, exc)
        refusal = error_response(
            f"Quota exceeded. {exc}",
            status_code=429,
            type_="insufficient_quota",
            code="quota_exceeded",
            headers={"retry-after": str(exc.retry_after_seconds)},
        )
        if not resolved.last:
            # Admission is per attempt: a rule that names this target (or this
            # target's provider) can refuse here while the next target is fine.
            # No ledger row exists for a refusal, so nothing else to undo.
            raise AttemptFailed("quota_exceeded", response=refusal) from exc
        return refusal
    except QuotaUnavailable as exc:
        raise ServiceUnavailableError(
            "Quota state is temporarily unavailable, so the request was refused "
            "rather than served unmetered."
        ) from exc

    accounting = RequestAccounting(
        context=RequestContext(
            request_id=request_id,
            model_name=model.name,
            # The alias, when the caller named one. Null for a direct call,
            # which is every request that existed before ADR 0101.
            requested_model=resolved.requested_name if resolved.via_alias else None,
            route_attempt=resolved.attempt if resolved.routed else None,
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
            plugin=resolved.provider.plugin,
            billing_mode=resolved.provider.billing_mode.value,
        ),
        session_factory=session_factory,
        settings=settings,
        fx=fx,
        estimator=estimator,
        model=model,
    )
    await accounting.begin()
    return Metered(
        accounting=accounting,
        reservation=reservation,
        quota=quota,
        may_fall_back=not resolved.last,
        provider_name=resolved.provider.name,
    )


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
