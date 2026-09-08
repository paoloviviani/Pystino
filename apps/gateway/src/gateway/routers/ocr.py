"""``POST /v1/ocr`` — document extraction.

The Cortecs and Mistral shape, because it is the one the ecosystem already
speaks: `model` plus a `document`, and back comes `pages[]` of markdown with a
`usage_info` carrying `pages_processed`. A RAG pipeline that can point its
document-extractor setting at Mistral can point it here instead, and then the
choice of *which* extractor runs becomes an administrator's grant rather than a
setting in somebody's tool.

The same pipeline as every other metered route (resolve, check access, reserve,
call, record, settle), with four differences that come from the shape of the
operation rather than from preference:

**The unit is a page.** Not a token, and not a millionth of one. Cost comes from
`model_prices.per_page`, the count from the counterparty's `pages_processed`,
and neither is inferred from the length of the returned text — that would be a
billing figure with no source.

**The reservation is a floor, not a worst case.** Every other surface can bound
its bill before the call: chat has `max_tokens`, images have `n`. A document's
page count is unknown until it has been read, and there is no field to bound it
unless the caller supplied `pages`. So admission reserves what it can prove — the
page selection if there is one, otherwise a single page — and the real count is
settled afterwards. The consequence is stated because it is real and not
hypothetical: **a caller close to their ceiling can exceed it with one large
document.** The alternative, reserving a pessimistic maximum, refuses ordinary
requests that would have fitted, and refusing the many to bound the one is a
worse trade for a ceiling that is already checked again at settle.

**Redaction cannot run on the way out, and matters more on the way back.** With
`document_url` the provider fetches the document itself, so the bytes never
reach us and there is nothing to inspect. What returns is the document as text,
which is where a scanned identity card becomes searchable — so this is the one
surface where the response is the sensitive half.

**Never streamed.** The API has no streaming form, and a partial transcription
is not billable output.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway import extraction
from gateway.accounting import TokenCounts
from gateway.config import Settings
from gateway.deps import (
    ControlHttpDep,
    EstimatorDep,
    PrincipalDep,
    ProvidersDep,
    QuotaDep,
    RedactorDep,
    SessionDep,
    SettingsDep,
)
from gateway.errors import BadRequestError, ContentBlockedError
from gateway.models import ApiSurface, ModelDef
from gateway.routers import _metered
from gateway.schemas import OcrRequest
from gateway.upstream import UpstreamError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["openai"])

SURFACE = ApiSurface.OCR

#: The plugin name that means "read it here, not over there". A provider row
#: naming it is the extraction service, not an endpoint on the internet.
LOCAL_EXTRACTOR = "extractor"


def _extractor_endpoint(model: ModelDef, settings: Settings) -> str:
    """Where the extraction service is.

    The provider row's own Base URL wins, because a deployment that scaled the
    extractor separately said so there; the setting is the fallback for a row
    left on the plugin's default.
    """
    configured = (model.provider.base_url or "").strip() if model.provider else ""
    return configured or settings.extractor.endpoint


async def _redact_pages(
    request: Request,
    payload: dict[str, Any],
    *,
    redactor: Any,
    principal: Any,
    model: ModelDef,
    metered: Any,
) -> JSONResponse | None:
    """Detect and substitute in `pages[].markdown`, in place.

    **The direction is the opposite of every other surface, and that is the
    point.** Elsewhere the request is redacted on the way out and placeholders
    are restored on the way back. Here the document went out as bytes or as a
    URL — with a URL we never even held it — and what returns is the document
    *as text*. That is the moment a scanned identity card stops being a picture
    and becomes searchable, indexable, greppable data, so the response is the
    half worth protecting.

    Returns a response to send instead, when the policy blocks; `None` when the
    pages were rewritten in place and the caller should carry on.

    The request is **not** un-charged when the policy blocks. The counterparty
    read the document and will invoice for it whatever we then decide to hand
    over, so the row stays `completed` with its real cost and records what was
    refused. Reversing the charge would be a nicer story and a false one.
    """
    pages = [page for page in (payload.get("pages") or []) if isinstance(page, dict)]
    texts = [page.get("markdown") for page in pages]
    if not any(isinstance(text, str) and text for text in texts):
        return None

    policy = _metered.redaction_policy(request, principal=principal, model=model)
    messages = [
        {"role": "user", "content": text if isinstance(text, str) else ""} for text in texts
    ]
    try:
        outcome = await redactor.redact_request(messages, policy=policy)
    except ContentBlockedError as exc:
        # Nothing was substituted — the policy refused outright — but which
        # rule refused it is the fact worth keeping.
        metered.accounting.observe_redaction(
            engine=getattr(redactor, "name", None),
            scope=policy.scope if policy else None,
            rule_id=policy.rule_id if policy else None,
        )
        await metered.completed(upstream_status=200)
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "message": (
                        "The extracted text contains data this deployment's redaction "
                        f"policy refuses to return: {exc}"
                    ),
                    "type": "invalid_request_error",
                    "code": exc.code,
                }
            },
        )

    for page, message in zip(pages, outcome.messages, strict=True):
        page["markdown"] = str(message.get("content") or "")
    metered.accounting.observe_redaction(
        engine=outcome.engine,
        entity_count=outcome.entity_count,
        scope=outcome.scope,
        rule_id=outcome.rule_id,
    )
    return None


@router.post("/ocr", response_model=None)
async def extract_document(
    request: Request,
    body: OcrRequest,
    principal: PrincipalDep,
    session: SessionDep,
    settings: SettingsDep,
    quota: QuotaDep,
    redactor: RedactorDep,
    providers: ProvidersDep,
    estimator: EstimatorDep,
    control_http: ControlHttpDep,
) -> JSONResponse:
    if not body.document.location():
        raise BadRequestError(
            "'document' must carry a 'document_url' or an 'image_url'.",
            code="invalid_document",
        )

    request_id = _metered.request_id_for(request, settings)
    model = await _metered.resolve_model(session, body.model, principal=principal, surface=SURFACE)

    # Which backend reads this document: our own extractor, or the model's
    # provider. Decided by `providers.plugin`, which is already the answer to
    # "what kind of thing is behind this row" — see plugins/extractor.py.
    local = model.provider is not None and model.provider.plugin == LOCAL_EXTRACTOR
    inline = body.document.inline_bytes()

    if local and inline is None:
        # Deliberate, and a security decision rather than a missing feature: a
        # URL here would have the *gateway* fetch a caller-supplied address, on
        # a network where the compose services, the database and the cloud
        # metadata endpoint all live. Server-side request forgery is not worth a
        # convenience, so the local extractor reads documents that travelled
        # with the request and says so.
        raise BadRequestError(
            "The local extractor reads documents sent with the request. Send the "
            "document as a base64 data URI, or choose a model whose provider "
            "fetches URLs itself.",
            code="document_url_not_supported",
        )

    upstream = None if local else await _metered.resolve_upstream(providers, model)

    # -- reservation --------------------------------------------------------
    # What can be proven before the call: the caller's own page selection, or
    # one page. See the module docstring for why this is a floor rather than a
    # ceiling, and what that costs.
    reserved_pages = len(body.pages) if body.pages else 1
    metered = await _metered.begin(
        request,
        session=session,
        principal=principal,
        settings=settings,
        quota=quota,
        estimator=estimator,
        model=model,
        surface=SURFACE,
        request_id=request_id,
        worst_case=TokenCounts(pages=reserved_pages),
        outcome=None,
        streamed=False,
    )
    if isinstance(metered, JSONResponse):
        return metered

    if local:
        assert inline is not None  # refused above; narrows the type
        data, media_type = inline
        try:
            outcome = await extraction.extract_document(
                control_http,
                _extractor_endpoint(model, settings),
                data,
                media_type=media_type,
                request_id=request_id,
            )
        except UpstreamError as exc:
            # A local extractor that is down and a remote one that is down are
            # the same event from the caller's side, and the same ledger row.
            raise await metered.upstream_unreachable(exc) from exc

        try:
            # Built with the *upstream* name, and rewritten to ours on the way
            # out a few lines below — the same order the proxied branch uses,
            # and for the same reason. `observe_payload` reads the served model
            # from this dict, so building it with our client-facing name made
            # the recorder compare `local-documents` against `markitdown` and
            # conclude the provider had substituted a model. It had not: those
            # are two names for one thing. The reports then disclosed that the
            # total "may not match the provider's invoice" for a request that
            # cost nothing and had no counterparty to invoice it.
            extracted = extraction.as_ocr_response(
                outcome, model_name=model.upstream_model
            )
        except extraction.DocumentRefused as exc:
            # Nothing was read, so nothing is charged — and the row records the
            # refusal rather than a successful request that returned no pages.
            return await metered.upstream_refused(
                status_code=422,
                message=str(exc),
                content={"error": {"message": str(exc), "code": exc.code}},
            )

        metered.accounting.observe_payload(extracted)
        if (blocked := await _redact_pages(
            request,
            extracted,
            redactor=redactor,
            principal=principal,
            model=model,
            metered=metered,
        )) is not None:
            return blocked
        await metered.completed(upstream_status=200)
        # Our name for the caller, the extractor's own in the ledger.
        extracted["model"] = model.name
        return JSONResponse(status_code=200, content=extracted)

    assert upstream is not None  # `local` is false, so this was resolved above
    payload = body.upstream_payload(upstream_model=model.upstream_model)
    payload = metered.shape_payload(payload, surface=SURFACE)

    try:
        response = await upstream.ocr(payload, request_id=request_id)
    except UpstreamError as exc:
        raise await metered.upstream_unreachable(exc) from exc

    if response.status_code >= 400:
        return await metered.upstream_refused(
            status_code=response.status_code,
            message=_metered.error_text(response.payload),
            content=response.payload,
        )

    if response.payload is not None:
        # Feeds `usage_info.pages_processed` and the model that actually served
        # it; the reader knows where both live.
        metered.accounting.observe_payload(response.payload, headers=response.headers)

    body_out: dict[str, Any] = dict(response.payload or {})
    if (blocked := await _redact_pages(
        request,
        body_out,
        redactor=redactor,
        principal=principal,
        model=model,
        metered=metered,
    )) is not None:
        return blocked

    await metered.completed(upstream_status=response.status_code)
    # Our model name, not the counterparty's, exactly as every other route does:
    # clients compare the echoed name against what they sent. The upstream's own
    # name is in the ledger.
    if body_out.get("model") is not None:
        body_out["model"] = model.name
    return JSONResponse(status_code=response.status_code, content=body_out)
