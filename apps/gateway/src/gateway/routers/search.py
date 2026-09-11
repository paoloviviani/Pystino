"""``POST /v1/search`` — a web search this gateway runs itself.

Phase 2 of [docs/web-search-plan.md](../../../../../docs/web-search-plan.md).
The same pipeline as every other metered route — resolve, check access, redact,
reserve, call, record, settle — through ``routers/_metered.py`` and not beside
it, for the reason ingestion goes through it too: a second private copy of
reserve → record → settle would make search spend invisible to the reports
built to catch it.

Five things about this surface differ from the others, each forced by the shape
of the operation.

**The unit is a request, and it has no price.** Not a token, not a page. The
decision was the user's and is recorded on ``LimitMetric.OWN_SEARCH_REQUESTS``:
half the candidate vendors' rates cannot be read at source, so a rate table
would be a guess in half its rows wearing the same type as a measurement, while
a count is never wrong and reconciles against a vendor's dashboard with no
currency and no rounding. So the reservation is ``worst_case_own_searches=1``
and ``worst_case=TokenCounts()`` — nothing else — and a price row on a search
model would charge nothing because there are no counts for it to multiply.

**The tier is the model, not a parameter.** A "model" on a search provider is
a *backend at a depth*: ``upstream_model`` carries Linkup's ``depth`` or Exa's
``type``. That makes which depth a caller may run a grant an administrator
makes through the existing model-access machinery, which matters because a
request ceiling bounds volume and not spend — Linkup ``deep`` is ten times
``flash`` and Exa ``deep-reasoning`` is twice ``instant``, so a thousand
searches is a number anyone can reason about and a bill nobody can. A caller
who may run two depths is granted two models.

**The search is counted before it is made, and a failure does not refund it.**
``observe_own_search`` is called before the HTTP request, so a vendor error, a
timeout and a torn-down connection all leave the count in the ledger. The
alternative — counting only a 2xx — makes the ceiling raisable by making the
search fail, which is the property the metering tests were written to hold
(see ``test_own_search_metering.py``). What it costs is honest and worth
stating: a misconfigured backend burns a caller's search budget while returning
nothing. That is the cheaper of the two wrong answers, and it is visible,
because the row records the backend it went to.

**The query is redacted, and that is a real cost.** It is an egress — text
derived from a prompt, sent to a third party who logs it — so it goes through
the same policy as every other one. The consequence is that searching for a
person by name under a policy that protects ``PERSON`` searches for
``<PERSON_a1b2>`` and finds nothing useful. That is not a bug to work around
here: redaction is scoped per provider (ADR 0038), so an administrator who
wants name searches turns the entity off *for the search provider* and leaves
it on for the model providers. Placeholders are deliberately **not** restored
in the results — a vendor returning our placeholder back to us would mean it
had indexed it, which has not happened, and running a restore over untrusted
web text to find nothing would be theatre.

**Nothing a caller sends reaches a URL.** The address is
``provider.base_url`` + the plugin's ``search_path``, both configuration; the
query, the domain filters and ``backend_options`` are JSON body fields and
cannot be anything else. Redirects are not followed (``build_http_client`` sets
``follow_redirects=False``), so a vendor answering 302 to a link-local address
does not make this gateway fetch it. That is the whole SSRF story on this
route, and it is deliberate rather than inherited: the neighbouring failure is
recorded in ``/v1/ocr``, where a caller-supplied ``document_url`` is refused
outright for the local extractor because *that* one would have had the gateway
fetch an address a caller chose.

**What comes back is untrusted text, and nothing here pretends otherwise.**
Titles and snippets are whatever a page's author wrote, and a caller that feeds
them straight into a prompt has added a prompt-injection surface. This route
returns them as data and executes nothing: it is phase 3 — a loop where the
gateway puts results back into a model's context — that would make the hazard
live, and docs/web-search-plan.md says so rather than leaving it to be
discovered. Written down here too because the route exists before the loop
does, and the first caller to build that loop will read this file and not that
one.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway.accounting import TokenCounts
from gateway.deps import (
    EstimatorDep,
    PrincipalDep,
    ProvidersDep,
    QuotaDep,
    RedactorDep,
    SessionDep,
    SettingsDep,
)
from gateway.errors import BadRequestError
from gateway.models import ApiSurface, ModelDef
from gateway.plugins import registry as plugin_registry
from gateway.plugins.search import (
    SearchConfigurationError,
    SearchOutcome,
    SearchPlugin,
    SearchQuery,
    SearchUnreadableError,
    normalise_tier,
)
from gateway.routers import _metered
from gateway.schemas import SearchRequest
from gateway.upstream import UpstreamError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["openai"])

SURFACE = ApiSurface.SEARCH


def _search_plugin(model: ModelDef) -> SearchPlugin:
    """The search backend behind *model*, or a 400 naming what is wrong.

    Checked structurally with ``isinstance`` against a runtime-checkable
    protocol rather than by comparing ``kind``, because ``kind`` is a column an
    administrator sets and this is a question about code: a row labelled
    ``search`` whose plugin cannot search is a configuration mistake, and it
    should read as one instead of raising ``AttributeError`` from inside the
    route and reaching the caller as a 500.
    """
    provider = model.provider
    name = provider.plugin if provider is not None else None
    try:
        plugin = plugin_registry.resolve(name)
    except plugin_registry.UnknownPluginError as exc:
        raise BadRequestError(str(exc), code="unknown_search_backend") from exc

    if not isinstance(plugin, SearchPlugin):
        raise BadRequestError(
            f"The model {model.name!r} is served by the {plugin.name!r} plugin, "
            "which is not a search backend. A search model must sit behind a "
            "provider whose type runs searches, such as Linkup or Exa.",
            code="not_a_search_backend",
        )
    return plugin


def _as_body(outcome: SearchOutcome, *, model: ModelDef, backend: str) -> dict[str, Any]:
    """The response a caller gets.

    Normalised rather than the vendor's own shape, which is the point of the
    route: swapping Linkup for Exa is an administrator's edit, and a caller
    that had parsed ``results[].name`` for one and ``results[].title`` for the
    other would break on it.

    It reports the count and deliberately reports no money — not ours, because
    there is none, and not the vendor's, because ``costDollars`` is a figure in
    a currency this deployment does not bill in, for an arrangement the caller
    is not party to.
    """
    return {
        "object": "search.results",
        # Our name for the model, never the tier we sent upstream — the same
        # rule every other surface follows, because clients compare the echoed
        # name against what they sent.
        "model": model.name,
        "backend": backend,
        "tier": outcome.tier,
        "results": [
            {
                "url": result.url,
                "title": result.title,
                "snippet": result.snippet,
                "content": result.content,
                "published_at": result.published_at,
                "author": result.author,
                "type": result.media_type,
            }
            for result in outcome.results
        ],
        # The metered quantity, echoed so a caller can reconcile its own usage
        # against a ceiling it can otherwise only learn by being refused.
        "search": {"requests": outcome.searches},
    }


@router.post("/search", response_model=None)
async def run_search(
    request: Request,
    body: SearchRequest,
    principal: PrincipalDep,
    session: SessionDep,
    settings: SettingsDep,
    quota: QuotaDep,
    redactor: RedactorDep,
    providers: ProvidersDep,
    estimator: EstimatorDep,
) -> JSONResponse:
    request_id = _metered.request_id_for(request, settings)
    model = await _metered.resolve_model(session, body.model, principal=principal, surface=SURFACE)
    plugin = _search_plugin(model)
    upstream = await _metered.resolve_upstream(providers, model)

    # -- redaction ----------------------------------------------------------
    # Before the reservation, as on every other surface, so a blocked query
    # leaves a `status=blocked` row and no charge — and so the query that is
    # sent is the one that was screened.
    outcome = await _metered.redact_or_block(
        request,
        [{"role": "user", "content": body.query}],
        redactor=redactor,
        session=session,
        principal=principal,
        model=model,
        settings=settings,
        surface=SURFACE,
        request_id=request_id,
    )
    first = outcome.messages[0] if outcome.messages else {}
    redacted_query = str(first.get("content") or "").strip()
    if not redacted_query:
        # A query of nothing but whitespace today; and, should a redaction mode
        # that *deletes* rather than substitutes ever exist, a query the policy
        # consumed entirely. Refused here either way, because a vendor asked to
        # search for "" answers with its own 400 and the caller reads that as
        # an outage rather than as their own mistake. Checked after redaction
        # rather than on the request model so that both cases are one branch.
        raise BadRequestError(
            "'query' must contain something to search for.",
            code="empty_query",
        )

    # The tier is the model's, not the caller's. `upstream_model` is where a
    # model row keeps the name it is served under upstream, and for a search
    # backend that name *is* the depth.
    tier = normalise_tier(model.upstream_model, plugin)
    query = SearchQuery(
        query=redacted_query,
        tier=tier,
        max_results=body.max_results,
        include_content=body.include_content,
        include_domains=body.include_domains,
        exclude_domains=body.exclude_domains,
        extra=body.backend_options,
    )

    try:
        payload = plugin.build_request(query)
    except SearchConfigurationError as exc:
        # Before `begin`, so a request the backend could never have served
        # reserves nothing and counts no search. Nothing left this deployment.
        raise BadRequestError(str(exc), code="search_not_supported") from exc

    # -- reservation --------------------------------------------------------
    # One call, one search. `worst_case` is empty because there is no token
    # cost to bound: an unpriced search model has no cost ceiling because it
    # has no cost, which is the whole design rather than a gap in it.
    metered = await _metered.begin(
        fx=request.app.state.fx,
        session_factory=request.app.state.session_factory,
        session=session,
        principal=principal,
        settings=settings,
        quota=quota,
        estimator=estimator,
        model=model,
        surface=SURFACE,
        request_id=request_id,
        worst_case=TokenCounts(),
        worst_case_own_searches=1,
        outcome=outcome,
        streamed=False,
    )
    if isinstance(metered, JSONResponse):
        return metered

    # Counted *before* the call, and never unwound. See the module docstring:
    # a ceiling that forgave a failed search would be raisable by making the
    # search fail. The tier recorded is the one asked for, which is the only
    # one that exists before an answer arrives — and neither vendor reports a
    # trustworthy one afterwards.
    metered.accounting.observe_own_search(1, backend=plugin.name, tier=tier)

    try:
        response = await upstream.post_json(
            plugin.search_path, payload, request_id=request_id
        )
    except UpstreamError as exc:
        raise await metered.upstream_unreachable(exc) from exc

    if response.status_code >= 400:
        # The vendor's own error, passed through rather than summarised, for
        # the reason every other route does it: "insufficient credits" is more
        # useful to whoever has to fix it than anything invented here.
        return await metered.upstream_refused(
            status_code=response.status_code,
            message=_metered.error_text(response.payload),
            content=response.payload,
        )

    try:
        results = plugin.read_results(response.payload, query=query)
    except SearchUnreadableError as exc:
        # A 2xx this plugin cannot read. Settled as an upstream error with the
        # search still counted — it was made, and the vendor will invoice it —
        # rather than returning an empty result list, which would record a
        # successful search that found nothing and be investigated by nobody.
        logger.warning(
            "search backend %s answered %s with a body this plugin cannot read: %s",
            plugin.name,
            response.status_code,
            exc,
        )
        return await metered.upstream_refused(
            status_code=502,
            # What the *vendor* did, which was answer successfully. Writing the
            # 502 here would send whoever reconciles the bill looking for a
            # vendor-side failure that never happened — the search ran, and
            # they will invoice it.
            upstream_status=response.status_code,
            message=str(exc),
            content={
                "error": {
                    "message": str(exc),
                    "type": "upstream_error",
                    "code": "search_response_unreadable",
                }
            },
        )

    if results.vendor_report:
        # Logged, never stored. A vendor's dollar figure in `upstream_cost`
        # would be read by the reconciliation report as a counterparty charge
        # against a price table that does not exist for searches — and Exa's
        # own schema says of this field that it "is not an invoice record".
        # Here it is worth exactly one line: something to hold an invoice
        # against when one arrives.
        logger.info(
            "search %s on %s reported %s; recorded as %d request(s) and no cost",
            request_id,
            plugin.name,
            results.vendor_report,
            results.searches,
        )

    return JSONResponse(
        status_code=200,
        content=_as_body(results, model=model, backend=plugin.name),
        background=metered.completed_after_response(upstream_status=response.status_code),
    )
