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
from dataclasses import replace
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
from gateway.privacy import attribution
from gateway.privacy.evaluate import Evaluation
from gateway.privacy.rules import filetype_of
from gateway.privacy.state import state_of
from gateway.redaction.report import SOURCE_DOCUMENT, build_detail, build_summary, relabel
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
    routing: dict[str, Any] | None = None,
    request_id: str = "",
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
        _record_routing_only(request, payload, metered, redactor, routing, request_id)
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
        detail = build_detail(
            exc.findings or {}, policy, blocked=True, relabel_sources=SOURCE_DOCUMENT
        )
        if routing is not None:
            detail = attribution.with_routing(detail, {**routing, "outcome": "blocked"})
        metered.accounting.observe_redaction(
            engine=getattr(redactor, "name", None),
            scope=policy.scope if policy else None,
            rule_id=policy.rule_id if policy else None,
            detail=detail,
        )
        await metered.completed(upstream_status=200)
        content: dict[str, Any] = {
            "error": {
                "message": (
                    "The extracted text contains data this deployment's redaction "
                    f"policy refuses to return: {exc}"
                ),
                "type": "invalid_request_error",
                "code": exc.code,
            }
        }
        if _metered.wants_redaction_summary(request):
            content[_metered.REDACTION_SUMMARY_FIELD] = build_summary(
                request_id=request_id, detail=detail, blocked=True
            )
        return JSONResponse(status_code=403, content=content)

    for page, message in zip(pages, outcome.messages, strict=True):
        page["markdown"] = str(message.get("content") or "")
    detail = relabel(outcome.detail, SOURCE_DOCUMENT)
    if routing is not None and (detail is not None or _metered.privacy_configured(request)):
        detail = attribution.with_routing(detail, routing)
    metered.accounting.observe_redaction(
        engine=outcome.engine,
        entity_count=outcome.entity_count,
        scope=outcome.scope,
        rule_id=outcome.rule_id,
        detail=detail,
    )
    _attach_summary(request, payload, detail, routing, request_id, in_force=outcome.in_force)
    return None


def _attach_summary(
    request: Request,
    payload: dict[str, Any],
    detail: dict[str, Any] | None,
    routing: dict[str, Any] | None,
    request_id: str,
    *,
    in_force: bool,
) -> None:
    """Put ``pystino_redaction`` on the OCR response for a caller that asked for it."""
    if not _metered.wants_redaction_summary(request):
        return
    if not in_force and not (routing is not None and _metered.privacy_configured(request)):
        return
    payload[_metered.REDACTION_SUMMARY_FIELD] = build_summary(
        request_id=request_id, detail=detail, blocked=False, routing=routing
    )


def _record_routing_only(
    request: Request,
    payload: dict[str, Any],
    metered: Any,
    redactor: Any,
    routing: dict[str, Any] | None,
    request_id: str,
) -> None:
    """A document with no text to screen still gets its routing attribution."""
    if routing is None:
        return
    detail = None
    if _metered.privacy_configured(request):
        detail = attribution.with_routing(None, routing)
        metered.accounting.observe_redaction(engine=getattr(redactor, "name", None), detail=detail)
    _attach_summary(
        request,
        payload,
        detail,
        routing,
        request_id,
        in_force=getattr(redactor, "name", "noop") != "noop",
    )


def _scan_of(outcome: Any) -> bool:
    return attribution.is_scan_from(
        outcome.extractor, outcome.kind.value, bool(outcome.page_images)
    )


_NAME_FIELDS = ("document_name", "file_name", "filename", "name")


def _document_name(request: Request, body: OcrRequest) -> str | None:
    """The file's name as the caller gave it, from wherever clients put it.

    Mistral's OCR shape names it ``document_name``; some clients send
    ``file_name``/``filename``, a ``name=`` parameter on the data URI, or an
    ``x-filename`` header (the extractor's own convention). None when nobody said.
    """
    extra = body.document.model_extra or {}
    for field in _NAME_FIELDS:
        value = extra.get(field)
        if isinstance(value, str) and value.strip():
            return value
    header, _, _ = body.document.location().partition(",")
    for part in header.split(";")[1:]:
        key, _, value = part.partition("=")
        if key.strip().lower() in ("name", "filename") and value:
            return value
    header_name: str | None = request.headers.get("x-filename")
    return header_name or None


def _declared_mime(body: OcrRequest) -> str | None:
    location = body.document.location()
    if location.startswith("data:"):
        return (location[5:].partition(",")[0].split(";")[0]) or None
    return None


#: What each reader a rule can ask for reads with, by the extractor's own names.
_READER_NAMES = {"docling": ("docling",), "ocr": ("rapidocr",)}


def _read_by(extractor: str, reader: str) -> bool:
    """Whether *extractor* is (a form of) the reader a rule asked for."""
    if reader == "markitdown":
        return not any(name in extractor for name in ("docling", "rapidocr"))
    return any(name in extractor for name in _READER_NAMES[reader])


async def _refuse_after_reading(
    request: Request,
    metered: Any,
    *,
    redactor: Any,
    routing: dict[str, Any],
    rule_name: str,
    request_id: str,
) -> JSONResponse:
    """The 403 for a rule that could only apply once the document was read here.

    The reading happened on this deployment's own extractor, so nothing left it
    and nothing is charged. The row records the refusal beside the routing.
    """
    detail = attribution.with_routing(None, {**routing, "outcome": "refused"})
    detail["blocked"] = True
    metered.accounting.observe_redaction(engine=getattr(redactor, "name", None), detail=detail)
    await metered.completed(upstream_status=200)
    content: dict[str, Any] = {
        "error": {
            "message": f"Refused by the privacy rule '{rule_name}': this document may not be read.",
            "type": "invalid_request_error",
            "code": "routing_refused",
        }
    }
    if _metered.wants_redaction_summary(request):
        content[_metered.REDACTION_SUMMARY_FIELD] = build_summary(
            request_id=request_id, detail=detail, blocked=True
        )
    return JSONResponse(status_code=403, content=content)


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
    resolved = await _metered.resolve_model(
        session, body.model, principal=principal, surface=SURFACE
    )
    # The target, when the name sent was an alias (ADR 0101); everything below
    # follows what serves the request.
    model = resolved.model

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

    # -- privacy routing (ADR 0113) -----------------------------------------
    # Judged on this request alone, from what it shows: the file's name and type,
    # who is asking, where it would go. Whether it is a scan is only known once
    # it is read, so a rule that depends on that has the reading done here first.
    privacy = state_of(request)
    read_rules = privacy.rules_of("read")
    facts = attribution.base_facts("read", principal, model, requested_name=resolved.requested_name)
    mime = inline[1] if inline is not None else _declared_mime(body)
    name = attribution.clean_filename(_document_name(request, body))
    facts = replace(facts, filename=name, mime=mime, filetype=filetype_of(mime, name))
    plan = await privacy.evaluator.evaluate(read_rules, facts)
    engine: str | None = None
    fired = plan.fired
    if fired is not None and fired.action_type == "refuse":
        await _metered.refuse_by_rule(
            request,
            session,
            principal=principal,
            model=model,
            surface=SURFACE,
            settings=settings,
            request_id=request_id,
            routing=attribution.build(facts, evaluation=plan, outcome="refused"),
            rule_name=fired.name,
            why="this file may not be read.",
        )
    if fired is not None and fired.action_type == "reader":
        engine = str(fired.action.get("reader"))
    needs_local_read = engine is not None or plan.pending is not None
    if needs_local_read and not local:
        # The rule wants the file read here, which overrides the model the caller
        # named. Only possible for a document that travelled with the request and
        # when a local reader exists that the caller may use; otherwise fail
        # closed, naming the rule that could not be carried out.
        rerouted = None
        if inline is not None:
            rerouted = await _metered.reroute_to_local(
                session,
                privacy.local_extractor_models,
                principal=principal,
                surface=SURFACE,
                template=resolved,
            )
        if rerouted is None:
            blocking = fired or plan.pending_rule
            assert blocking is not None
            await _metered.refuse_by_rule(
                request,
                session,
                principal=principal,
                model=model,
                surface=SURFACE,
                settings=settings,
                request_id=request_id,
                routing=attribution.build(facts, evaluation=plan, outcome="refused"),
                rule_name=blocking.name,
                why=(
                    "it must be read on this server, and that is not possible for this "
                    "request (the document is a link, or no local reader is available to you)."
                ),
            )
        assert rerouted is not None
        resolved = rerouted
        model = resolved.model
        local = True
        facts = replace(
            attribution.base_facts(
                "read", principal, model, requested_name=resolved.requested_name
            ),
            filename=name,
            mime=mime,
            filetype=facts.filetype,
        )

    upstream = None if local else await _metered.resolve_upstream(providers, resolved)

    # -- reservation --------------------------------------------------------
    # What can be proven before the call: the caller's own page selection —
    # list or range string, both resolved to a true count by `page_count()`,
    # never `len()` on the field itself, which would count characters instead
    # of pages for a range like "0-100" — or one page. See the module
    # docstring for why this is a floor rather than a ceiling, and what that
    # costs.
    reserved_pages = body.page_count() or 1
    metered = await _metered.begin(
        fx=request.app.state.fx,
        session_factory=request.app.state.session_factory,
        session=session,
        principal=principal,
        settings=settings,
        quota=quota,
        estimator=estimator,
        resolved=resolved,
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

        async def read_with(reader: str | None) -> Any:
            return await extraction.extract_document(
                control_http,
                _extractor_endpoint(model, settings),
                data,
                media_type=media_type,
                filename=name,
                engine=reader,
                request_id=request_id,
                page_images=(
                    min(body.page_images.max_pages, extraction.MAX_PAGE_IMAGES)
                    if body.page_images
                    else 0
                ),
                long_side=(
                    min(body.page_images.long_side, extraction.MAX_LONG_SIDE)
                    if body.page_images
                    else None
                ),
                timeout_seconds=settings.extractor.timeout_seconds,
            )

        try:
            outcome = await read_with(engine)
            facts = replace(facts, is_scan=_scan_of(outcome))
            final: Evaluation = plan
            if read_rules and (plan.pending is not None or engine is None):
                # The scan fact is known now: judge the rules again, in full. A
                # different reader than the one used is a second reading.
                final = await privacy.evaluator.evaluate(read_rules, facts)
                wanted = final.fired.action.get("reader") if final.fired else None
                if (
                    final.fired is not None
                    and final.fired.action_type == "reader"
                    and wanted != engine
                    and not _read_by(outcome.extractor, str(wanted))
                ):
                    engine = str(wanted)
                    outcome = await read_with(engine)
                    facts = replace(facts, is_scan=_scan_of(outcome))
            elif read_rules:
                final = await privacy.evaluator.evaluate(read_rules, facts)
        except extraction.ExtractionTimeout as exc:
            raise await metered.upstream_unreachable(
                exc,
                message=(
                    "The document reader took too long and was stopped. A larger "
                    "document needs a longer GATEWAY_EXTRACTOR__TIMEOUT_SECONDS "
                    f"(now {settings.extractor.timeout_seconds:.0f}s)."
                ),
            ) from exc
        except UpstreamError as exc:
            # A local extractor that is down and a remote one that is down are
            # the same event from the caller's side, and the same ledger row.
            raise await metered.upstream_unreachable(exc) from exc

        routing = attribution.build(
            facts,
            evaluation=final,
            outcome="rule" if final.fired is not None else "default",
            extractor=outcome.extractor or None,
            fallback_reason=outcome.fallback_reason,
            reader=engine,
        )
        if final.fired is not None and final.fired.action_type == "refuse":
            return await _refuse_after_reading(
                request,
                metered,
                redactor=redactor,
                routing=routing,
                rule_name=final.fired.name,
                request_id=request_id,
            )

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
                outcome,
                model_name=resolved.upstream_model,
                want_page_images=body.page_images is not None,
            )
        except extraction.DocumentRefused as exc:
            # Nothing was read, so nothing is charged — and the row records the
            # refusal rather than a successful request that returned no pages.
            if _metered.privacy_configured(request):
                metered.accounting.observe_redaction(
                    engine=getattr(redactor, "name", None),
                    detail=attribution.with_routing(None, routing),
                )
            return await metered.upstream_refused(
                status_code=422,
                message=str(exc),
                content={"error": {"message": str(exc), "code": exc.code}},
            )

        metered.accounting.observe_payload(extracted)
        if (
            blocked := await _redact_pages(
                request,
                extracted,
                redactor=redactor,
                principal=principal,
                model=model,
                metered=metered,
                routing=routing,
                request_id=request_id,
            )
        ) is not None:
            return blocked
        # Our name for the caller, the extractor's own in the ledger.
        extracted["model"] = resolved.requested_name
        return JSONResponse(
            status_code=200,
            content=extracted,
            background=metered.completed_after_response(upstream_status=200),
        )

    assert upstream is not None  # `local` is false, so this was resolved above
    payload = body.upstream_payload(upstream_model=resolved.upstream_model)
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
    if (
        blocked := await _redact_pages(
            request,
            body_out,
            redactor=redactor,
            principal=principal,
            model=model,
            metered=metered,
            routing=attribution.build(
                facts,
                evaluation=plan,
                outcome="default",
            ),
            request_id=request_id,
        )
    ) is not None:
        return blocked

    # Our model name, not the counterparty's, exactly as every other route does:
    # clients compare the echoed name against what they sent. The upstream's own
    # name is in the ledger.
    if body_out.get("model") is not None:
        body_out["model"] = resolved.requested_name
    return JSONResponse(
        status_code=response.status_code,
        content=body_out,
        background=metered.completed_after_response(upstream_status=response.status_code),
    )
