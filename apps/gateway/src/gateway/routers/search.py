"""Search, in two shapes sharing one ledger.

``POST /v1/search/{backend}`` is an authenticated, metering passthrough — and
stays one. ``POST /v1/search`` (no backend) is the unified route: one request
shape in, one response shape out, through the backend the caller's
*billing-group policy* names (``groups.search_model_id``).

The unified route revisits ADR 0071's refusal to translate, narrowly and for
a reason that did not exist then: a group policy is unenforceable while the
vendor dialect lives in the caller. Linkup wants ``q``/``depth`` and Exa
wants ``query``/``numResults``; a chat that must send the right one per group
is a chat that chooses backends, which is exactly what the policy takes away
from it. The translation surface is three fields in and three fields out —
query, count; title, URL, snippet — and it lives on the plugins, where vendor
quirks belong (ADR 0032). Anything fancier stays on the passthrough, whose
body and answer still travel verbatim.

What the pipeline keeps from every other metered route — resolve, check
access, reserve, call, record, settle — it keeps through ``routers/_metered.py``
and not beside it, for the reason ingestion goes through it too: a second
private copy of reserve → record → settle would make search spend invisible
to the reports built to catch it.

Six things about the unified surface, each forced by the shape of the operation:

**The backend is the group's, never the request's.** The body carries no
backend, no depth, no tier. The policy names one backend anchor row; the
grant check runs against it exactly as the passthrough checks the backend in
the path, so a policy pointing somewhere the group may not go is as invisible
as a backend that does not exist: a 404, not a 403. A group with no policy
cannot use this route at all — also a 404, with a message an administrator
can act on rather than one the caller can route around.

**It always runs at the vendor's default depth.** The unified body offers no
tier, so the ledger records none — the honest state, matching the
passthrough's ``tier=None``. A caller that needs Linkup ``deep`` or Exa
``deep-reasoning`` names the backend on the passthrough, where the tier is
the caller's own words and the spend is the operator's to reconcile.

**The unit is a request, and it has no price.** As on the passthrough: the
decision is recorded on ``LimitMetric.OWN_SEARCH_REQUESTS``, the reservation
is ``worst_case_own_searches=1`` and ``worst_case=TokenCounts()`` — nothing
else.

**The search is counted before it is made, and a failure does not refund it.**
``observe_own_search`` runs before the HTTP request, so a vendor error, a
timeout and a torn-down connection all leave the count in the ledger. A
misconfigured backend burns a caller's search budget while returning nothing
— the cheaper of the two wrong answers, and visible, because the row records
the backend it went to.

**The query is redacted, because the route knows which field holds it.** The
passthrough gives this up — a body it promises not to read cannot be screened
— and records the gap in this docstring's place. The unified route reads
exactly one field, so the standard engine screens it under the effective
policy before any vendor sees it.

**Nothing a caller sends reaches a URL.** The address is
``provider.base_url`` + the plugin's ``search_path``, both configuration; the
rebuilt body travels as data. Redirects are not followed
(``build_http_client`` sets ``follow_redirects=False``), so a vendor
answering 302 to a link-local address does not make this gateway fetch it.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import joinedload, selectinload

from gateway.access import accessible_models
from gateway.accounting import TokenCounts
from gateway.deps import (
    EstimatorDep,
    PrincipalDep,
    ProvidersDep,
    QuotaDep,
    RedactorDep,
    SecretsDep,
    SessionDep,
    SettingsDep,
)
from gateway.errors import BadRequestError, NotFoundError
from gateway.models import ApiSurface, ModelDef, ModelKind
from gateway.plugins import registry as plugin_registry
from gateway.plugins.search import SearchPlugin
from gateway.routers import _metered
from gateway.schemas import SearchRequest, SearchResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["openai"])

SURFACE = ApiSurface.SEARCH


async def _policy_backend(
    backend_id: uuid.UUID, principal: PrincipalDep, session: SessionDep
) -> tuple[ModelDef, SearchPlugin]:
    """The billing group's policy backend, as its plugin and its grant anchor.

    The grant check runs against the policy's anchor row through the same
    access predicate every other surface uses — group grants, personal grants
    and public models alike — so a policy pointing somewhere the group may not
    go resolves to nothing: a 404, not a 403, because which backends exist is
    not the caller's business. A policy naming a row of the wrong kind is a
    400 instead, a mistake the administrator can fix.
    """
    stmt = (
        accessible_models(user_id=principal.user.id, group_ids=[principal.billing_group.id])
        .where(ModelDef.id == backend_id)
        # Prices and the provider, like `accessible_model_by_name`: the
        # metering path needs both immediately, and touching an unloaded
        # relationship from async code raises MissingGreenlet rather than
        # quietly emitting a query.
        .options(selectinload(ModelDef.prices), joinedload(ModelDef.provider))
    )
    model = (await session.execute(stmt)).scalars().first()
    if model is None:
        raise NotFoundError("No search backend is available to your billing group.")
    if model.kind != ModelKind.SEARCH:
        raise BadRequestError(
            f"{model.name!r} is a {model.kind.value} model, not a search backend. "
            "Point the group's search policy at a search backend.",
            code="wrong_model_kind",
        )
    provider = model.provider
    name = provider.plugin if provider is not None else None
    try:
        plugin = plugin_registry.resolve(name)
    except plugin_registry.UnknownPluginError as exc:
        raise BadRequestError(str(exc), code="unknown_search_backend") from exc

    if not isinstance(plugin, SearchPlugin):
        raise BadRequestError(
            f"The backend {model.name!r} is served by the {plugin.name!r} plugin, "
            "which is not a search backend.",
            code="not_a_search_backend",
        )
    return model, plugin


@router.post("/search", response_model=None)
async def unified_search(
    payload: SearchRequest,
    request: Request,
    principal: PrincipalDep,
    session: SessionDep,
    settings: SettingsDep,
    quota: QuotaDep,
    redactor: RedactorDep,
    providers: ProvidersDep,
    estimator: EstimatorDep,
    secrets: SecretsDep,
) -> JSONResponse:
    request_id = _metered.request_id_for(request, settings)

    # The policy, not the request, names the backend. None means the group
    # cannot use this route at all — a 404 with a message an administrator
    # can act on, rather than a backend the caller could route around.
    backend_id = principal.billing_group.search_model_id
    if backend_id is None:
        raise NotFoundError(
            "No search provider is configured for your billing group.",
            code="no_search_provider",
        )
    model, plugin = await _policy_backend(backend_id, principal, session)
    upstream = await _metered.resolve_upstream(providers, model)

    # -- redaction ----------------------------------------------------------
    # The one field the route reads is the one field it screens, under the
    # effective policy, before any vendor sees it. A block is recorded by
    # `redact_or_block` itself and never reaches metering.
    outcome = await _metered.redact_or_block(
        request,
        [{"role": "user", "content": payload.query}],
        redactor=redactor,
        session=session,
        principal=principal,
        model=model,
        settings=settings,
        surface=SURFACE,
        request_id=request_id,
    )
    query = str(outcome.messages[0].get("content") or "")
    if not query.strip():
        raise BadRequestError("The search needs a query.", code="empty_query")

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
        request_id=request_id,
        worst_case=TokenCounts(),
        worst_case_own_searches=1,
        outcome=outcome,
        streamed=False,
    )
    if isinstance(metered, JSONResponse):
        return metered

    # Counted *before* the call, and never unwound — see the module docstring.
    # No tier: the unified body offers none and the vendor runs its default.
    metered.accounting.observe_own_search(1, backend=plugin.name, tier=None)

    # The backend's own credential, attached here and nowhere else: the caller
    # never sees a vendor key, and the gateway never forwards a caller body.
    provider = model.provider
    credential = ""
    if provider is not None and provider.api_key_encrypted:
        try:
            credential = secrets.decrypt(provider.api_key_encrypted)
        except Exception as exc:
            raise BadRequestError(
                f"backend {provider.name!r}: {exc}", code="backend_key_unreadable"
            ) from exc

    # The vendor's own request, rebuilt by its plugin at the vendor's default
    # depth — the only depth this route offers — plus whatever headers the
    # plugin needs beyond the credential (Jina's content-excluding header is
    # the case that matters; the passthrough never sends them, because a
    # verbatim answer must stay the caller's own shape).
    vendor_body = plugin.build_search_body(query, payload.max_results)
    call_headers = dict(plugin.auth_headers(credential)) if credential else {}
    call_headers.update(plugin.search_headers())
    try:
        response = await upstream.post_json(
            plugin.search_path,
            vendor_body,
            request_id=request_id,
            extra_headers=call_headers or None,
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

    # Normalised, not verbatim: the caller asked in the gateway's shape and
    # gets the gateway's shape back, with the backend named because one
    # search never mixes backends and the ledger says the same.
    answer = SearchResponse(
        results=plugin.read_search_results(response.payload),
        backend=plugin.name,
    )
    return JSONResponse(
        status_code=200,
        content=answer.model_dump(),
        background=metered.completed_after_response(upstream_status=response.status_code),
    )


async def _backend(
    backend_name: str, principal: PrincipalDep, session: SessionDep
) -> tuple[ModelDef, SearchPlugin]:
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
            "Send the vendor's own JSON request body — the gateway forwards it verbatim.",
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
