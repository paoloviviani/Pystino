"""``POST /v1/search/{backend}`` — an authenticated, metering passthrough.

The gateway's job here is deliberately small, and the design says so twice:
it authenticates the caller, checks their grant on the backend, counts the
request, attaches the backend's own credential, and forwards the body
**verbatim** to the vendor, returning the vendor's answer **verbatim**. It
does not translate. A common search API — one request shape and one response
shape that the gateway translates per counterparty — was considered and
rejected: every vendor difference the translation absorbed would be knowledge
maintained here forever, and the layer would have to grow a field for the
first caller that needed one. The caller that wants to speak each vendor —
this deployment's chat — is one consumer, not a fleet.

What the pipeline keeps from every other metered route — resolve, check
access, reserve, call, record, settle — it keeps through ``routers/_metered.py``
and not beside it, for the reason ingestion goes through it too: a second
private copy of reserve → record → settle would make search spend invisible
to the reports built to catch it.

Five things about this surface, each forced by the shape of the operation:

**The unit is a request, and it has no price.** Not a token, not a page. The
decision is recorded on ``LimitMetric.OWN_SEARCH_REQUESTS``: half the
candidate vendors' rates cannot be read at source, so a rate table would be a
guess in half its rows wearing the same type as a measurement, while a count
is never wrong and reconciles against a vendor's dashboard with no currency
and no rounding. The reservation is ``worst_case_own_searches=1`` and
``worst_case=TokenCounts()`` — nothing else.

**The grant is per backend, and the body is not read.** One model row per
backend — named after it — carries the access grant, and the caller's own
request body reaches the vendor untouched: Linkup's ``q`` and ``depth``, Exa's
``query`` and ``type``, stay the caller's business. The gateway knowing which
body field held the tier would be vendor knowledge in the one place this
design refuses to keep it, so the tier-granular grants the translated route
once offered collapse to per-backend ones. A caller who may use a backend may
run any depth it sells; the request ceiling bounds volume, and the vendor's
pricing differences are the operator's to reconcile against the count.

**The search is counted before it is made, and a failure does not refund it.**
``observe_own_search`` runs before the HTTP request, so a vendor error, a
timeout and a torn-down connection all leave the count in the ledger. The
alternative — counting only a 2xx — makes the ceiling raisable by making the
search fail. What it costs is honest and worth stating: a misconfigured
backend burns a caller's search budget while returning nothing. That is the
cheaper of the two wrong answers, and it is visible, because the row records
the backend it went to.

**Nothing a caller sends reaches a URL.** The address is
``provider.base_url`` + the plugin's ``search_path``, both configuration; the
body travels as data. Redirects are not followed (``build_http_client`` sets
``follow_redirects=False``), so a vendor answering 302 to a link-local address
does not make this gateway fetch it.

**Redaction is the one thing this design gives up, and it says so.** The
translated route redacted the query, because it knew which field held it. A
passthrough that promised not to read the body cannot redact it, and
guessing — one vendor field name in one config table — is how the layer the
design refuses starts growing back. The egress policy therefore does not
cover search on this route; the caller that assembles the body can screen its
own query first, and this docstring is where the gap is recorded rather than
discovered.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway.accounting import TokenCounts
from gateway.deps import (
    EstimatorDep,
    PrincipalDep,
    ProvidersDep,
    QuotaDep,
    SecretsDep,
    SessionDep,
    SettingsDep,
)
from gateway.errors import BadRequestError, NotFoundError
from gateway.models import ApiSurface
from gateway.plugins import registry as plugin_registry
from gateway.plugins.search import SearchPlugin
from gateway.routers import _metered

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["openai"])

SURFACE = ApiSurface.SEARCH


async def _backend(
    backend_name: str, principal: PrincipalDep, session: SessionDep
):
    """The backend to search with, as its plugin and its grant anchor.

    The anchor is the one model row a backend owns — named after it — and it
    is what the caller's grant sits on. Resolving it through the same
    access-checked path every other surface uses means an ungranted backend is
    exactly as invisible as one that does not exist: a 404, not a 403, because
    which backends exist is not the caller's business.
    """
    try:
        model = await _metered.resolve_model(
            session, backend_name, principal=principal, surface=SURFACE
        )
    except NotFoundError as exc:
        raise NotFoundError(f"No such search backend: {backend_name}") from exc
    provider = model.provider
    name = provider.plugin if provider is not None else None
    try:
        plugin = plugin_registry.resolve(name)
    except plugin_registry.UnknownPluginError as exc:
        raise BadRequestError(str(exc), code="unknown_search_backend") from exc

    if not isinstance(plugin, SearchPlugin):
        raise BadRequestError(
            f"The backend {backend_name!r} is served by the {plugin.name!r} plugin, "
            "which is not a search backend.",
            code="not_a_search_backend",
        )
    return model, plugin


@router.post("/search/{backend_name:path}", response_model=None)
async def search(
    backend_name: str,
    request: Request,
    principal: PrincipalDep,
    session: SessionDep,
    settings: SettingsDep,
    quota: QuotaDep,
    providers: ProvidersDep,
    estimator: EstimatorDep,
    secrets: SecretsDep,
) -> JSONResponse:
    model, plugin = await _backend(backend_name, principal, session)
    upstream = await _metered.resolve_upstream(providers, model)

    body = await request.body()
    if not body:
        raise BadRequestError(
            "Send the vendor's own JSON request body — the gateway forwards it "
            "verbatim.",
            code="empty_body",
        )

    # -- reservation --------------------------------------------------------
    # One call, one search. `worst_case` is empty because there is no token
    # cost to bound: an unpriced search has no cost ceiling because it has no
    # cost, which is the whole design rather than a gap in it.
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
        request_id=_metered.request_id_for(request, settings),
        worst_case=TokenCounts(),
        worst_case_own_searches=1,
        outcome=None,
        streamed=False,
    )
    if isinstance(metered, JSONResponse):
        return metered

    # Counted *before* the call, and never unwound — see the module docstring.
    metered.accounting.observe_own_search(1, backend=plugin.name, tier=None)

    # The backend's own credential, attached here and nowhere else: the caller
    # never sees a vendor key, and the gateway never reads the body it is
    # carrying.
    provider = model.provider
    credential = ""
    if provider is not None and provider.api_key_encrypted:
        try:
            credential = secrets.decrypt(provider.api_key_encrypted)
        except Exception as exc:
            raise BadRequestError(
                f"backend {provider.name!r}: {exc}", code="backend_key_unreadable"
            ) from exc

    import orjson

    try:
        payload = orjson.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("not a JSON object")
    except ValueError as exc:
        raise BadRequestError(
            "The body must be a JSON object in the vendor's own shape.",
            code="invalid_body",
        ) from exc

    try:
        response = await upstream.post_json(
            plugin.search_path,
            payload,
            request_id=_metered.request_id_for(request, settings),
            extra_headers=plugin.auth_headers(credential) if credential else None,
        )
    except Exception as exc:
        raise await metered.upstream_unreachable(exc) from exc

    if response.status_code >= 400:
        # The vendor's own error, verbatim rather than summarised, for the
        # reason every other route does it: "insufficient credits" is more
        # useful to whoever has to fix it than anything invented here.
        return await metered.upstream_refused(
            status_code=response.status_code,
            message=_metered.error_text(response.payload),
            content=response.payload,
        )

    # The vendor's answer, verbatim. Reading it — normalising result shapes,
    # pulling out a vendor-reported cost — was the layer this design removed.
    return JSONResponse(
        status_code=200,
        content=response.payload,
        background=metered.completed_after_response(upstream_status=response.status_code),
    )
