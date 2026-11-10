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
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway import extraction
from gateway.accounting import TokenCounts
from gateway.config import EffectivePolicy, Settings
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
from gateway.errors import BadRequestError, ContentBlockedError, GatewayError
from gateway.models import ApiSurface, ModelDef
from gateway.privacy import attribution
from gateway.privacy.combine import Combined, Retarget, plan
from gateway.privacy.rules import DEFAULT_READER, filetype_of
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
    policy: EffectivePolicy | None,
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
    if header_name:
        return header_name
    # A link names its file in its path ("https://host/a/referto.pdf?sig=..."):
    # the name and extension a rule can match on, without fetching anything.
    location = body.document.location()
    if location.startswith(("http://", "https://")):
        base = unquote(urlsplit(location).path).rsplit("/", 1)[-1]
        return base or None
    return None


def _declared_mime(body: OcrRequest) -> str | None:
    location = body.document.location()
    if location.startswith("data:"):
        return (location[5:].partition(",")[0].split(";")[0]) or None
    return None


async def _reader_model(
    session: Any,
    reader: str,
    *,
    principal: Any,
    template: _metered.ResolvedModel,
    http: Any,
    settings: Settings,
) -> _metered.ResolvedModel | None:
    """The model a "read with" rule names, if this caller may use it, else None.

    A rule cannot grant access: the name goes through the same resolution a
    direct call does, so a caller without the grant gets None, and the route then
    fails closed rather than quietly using another model. A built-in extractor
    model whose engine the extractor does not run now is None too: the file is
    refused rather than read by an engine nobody chose.
    """
    try:
        found = await _metered.resolve_model(session, reader, principal=principal, surface=SURFACE)
    except GatewayError:
        return None
    if not await _engine_runs(found.model, http, settings):
        return None
    return _metered.ResolvedModel(
        model=found.model,
        requested_name=template.requested_name,
        attempt=template.attempt,
        last=template.last,
        routed=template.routed,
    )


async def _engine_runs(model: ModelDef, http: Any, settings: Settings) -> bool:
    """Whether the built-in extractor runs this model's engine (fail closed: an extractor
    that cannot be asked does not run a non-default engine). Models that are not the
    extractor's, and the plain markitdown reader, always pass."""
    if model.provider is None or model.provider.plugin != LOCAL_EXTRACTOR:
        return True
    upstream = model.upstream_model or model.name
    if extraction.engine_of_model(upstream) in (None, "markitdown"):
        return True  # runs on every build: nothing to ask
    engines = await extraction.reported_engines(http, _extractor_endpoint(model, settings))
    return extraction.model_availability(upstream, engines)


def _routing(
    request: Request,
    facts: Any,
    combined: Combined,
    *,
    rerouted: bool,
    **handled: Any,
) -> dict[str, Any]:
    names = _metered.policy_names_of(request, combined)
    outcome = "rerouted" if rerouted else "rule" if combined.winner or names else "default"
    return attribution.build(facts, combined=combined, outcome=outcome, policies=names, **handled)


async def _refuse(
    request: Request,
    session: Any,
    *,
    principal: Any,
    model: ModelDef,
    settings: Settings,
    request_id: str,
    facts: Any,
    combined: Combined,
    rule: Any,
    why: str,
) -> None:
    """The 403 for a file that may not be read, naming the rule that said so.

    Raised before anything is sent anywhere, so nothing is charged; the row
    records the refusal beside the routing.
    """
    await _metered.refuse_by_rule(
        request,
        session,
        principal=principal,
        model=model,
        surface=SURFACE,
        settings=settings,
        request_id=request_id,
        routing=attribution.build(
            facts,
            combined=combined,
            outcome="refused",
            policies=_metered.policy_names_of(request, combined),
        ),
        rule_name=rule.rule.name,
        pipeline_name=rule.pipeline.name,
        why=why,
    )


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

    # -- privacy pipelines (ADR 0113) -----------------------------------------
    # Every pipeline in scope runs and the results combine: the highest level on
    # the ladder wins, the policies add up. Judged on this request alone, from
    # what it shows: the file's name and type, who is asking, which reader the
    # caller named. Whether the file is a scan is only known once it is read, so
    # a rule that asks has the file read on the built-in extractor first (it
    # never leaves the stack for that). After a reroute the pipelines that cover
    # the new reader also run.
    privacy = state_of(request)
    privacy.ensure_loaded()
    facts = attribution.base_facts(
        "extract",
        principal,
        model,
        requested_name=resolved.requested_name,
    )
    mime = inline[1] if inline is not None else _declared_mime(body)
    name = attribution.clean_filename(_document_name(request, body))
    facts = replace(facts, filename=name, mime=mime, filetype=filetype_of(mime, name))
    named = resolved
    holder: dict[str, Any] = {"resolved": resolved}

    async def retarget(reader: str) -> Retarget:
        current = holder["resolved"]
        if reader in (current.model.name, current.requested_name):
            return Retarget(None, same=True)
        moved = await _reader_model(
            session,
            reader,
            principal=principal,
            template=named,
            http=control_http,
            settings=settings,
        )
        if moved is None:
            return Retarget(
                None,
                impossible=(
                    f"it must be read with '{reader}', and that reader is not available to you."
                ),
            )
        if inline is None and (
            moved.model.provider is not None and moved.model.provider.plugin == LOCAL_EXTRACTOR
        ):
            # The built-in extractor reads only documents sent with the request.
            return Retarget(
                None,
                impossible=(
                    f"it must be read by '{reader}', which is on the built-in extractor and reads "
                    "only documents sent with the request, and this document is a link."
                ),
            )
        holder["resolved"] = moved
        return Retarget(
            replace(
                attribution.base_facts(
                    "extract",
                    principal,
                    moved.model,
                    requested_name=moved.requested_name,
                ),
                filename=name,
                mime=mime,
                filetype=facts.filetype,
                is_scan=facts.is_scan,
            )
        )

    pipelines = privacy.pipelines_of("extract")
    combined = await plan(privacy.evaluator, pipelines, facts, retarget=retarget)
    probe: Any = None
    if combined.pending:
        # The answer depends on a fact found while reading: read once on the
        # built-in extractor (the file stays here), then plan again.
        holder["resolved"] = named
        probe_model = None
        if inline is not None:
            # The default reader first (every build has it), then any other of the
            # built-in extractor's models the caller may use.
            for probe_name in sorted(
                privacy.builtin_readers, key=lambda n: (n != DEFAULT_READER, n)
            ):
                probe_model = await _reader_model(
                    session,
                    probe_name,
                    principal=principal,
                    template=named,
                    http=control_http,
                    settings=settings,
                )
                if probe_model is not None:
                    break
        if probe_model is None:
            assert combined.pending_run is not None
            await _refuse(
                request,
                session,
                principal=principal,
                model=model,
                settings=settings,
                request_id=request_id,
                facts=facts,
                combined=combined,
                rule=combined.pending_run,
                why=(
                    "this file has to be read by the built-in extractor to tell whether it is a "
                    "scan, and that is not possible for this request (the document is a link, "
                    "or no built-in reader is available to you)."
                ),
            )
        assert probe_model is not None and inline is not None
        try:
            probe = await extraction.extract_document(
                control_http,
                _extractor_endpoint(probe_model.model, settings),
                inline[0],
                media_type=inline[1],
                filename=name,
                request_id=request_id,
                timeout_seconds=settings.extractor.timeout_seconds,
            )
        except (extraction.ExtractionTimeout, UpstreamError) as exc:
            raise BadRequestError(
                f"The document could not be read here to be judged: {exc}",
                code="document_unreadable",
            ) from exc
        facts = replace(
            facts,
            is_scan=_scan_of(probe),
        )
        holder["resolved"] = named
        combined = await plan(privacy.evaluator, pipelines, facts, retarget=retarget)
    if combined.impossible is not None or combined.refused:
        run = combined.reader_run if combined.impossible is not None else combined.winner
        assert run is not None
        await _refuse(
            request,
            session,
            principal=principal,
            model=model,
            settings=settings,
            request_id=request_id,
            facts=facts,
            combined=combined,
            rule=run,
            why=combined.impossible or "this file may not be read.",
        )
    resolved = holder["resolved"]
    model = resolved.model
    local = model.provider is not None and model.provider.plugin == LOCAL_EXTRACTOR
    engine = extraction.engine_of_model(model.upstream_model or model.name) if local else None
    if local and not await _engine_runs(model, control_http, settings):
        # The model exists (grants, prices) but this extractor image cannot run its
        # engine; reading it with another engine under this name would be a lie.
        raise BadRequestError(
            f"The model '{model.name}' is not available on this deployment's extractor "
            "(its engine is not installed). Choose another reader.",
            code="extractor_engine_unavailable",
        )
    policy = _metered.effective_policy_of(request, combined)
    rerouted = resolved is not named
    if rerouted:
        facts = replace(
            attribution.base_facts(
                "extract", principal, model, requested_name=resolved.requested_name
            ),
            filename=name,
            mime=mime,
            filetype=facts.filetype,
            is_scan=facts.is_scan,
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

        async def extract_with(reader: str | None) -> Any:
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
            if probe is not None and engine is None and body.page_images is None:
                # The scan question was already answered by reading once on the
                # built-in extractor with its default engine: that is this read
                # (unless the caller wants page images, which the probe did not ask for).
                outcome = probe
            else:
                outcome = await extract_with(engine)
            facts = replace(facts, is_scan=_scan_of(outcome))
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

        routing = _routing(
            request,
            facts,
            combined,
            rerouted=rerouted,
            extractor=outcome.extractor or None,
            fallback_reason=outcome.fallback_reason,
            reader=model.name,
            engine=engine,
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
                policy=policy,
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
            policy=policy,
            routing=_routing(request, facts, combined, rerouted=rerouted),
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
