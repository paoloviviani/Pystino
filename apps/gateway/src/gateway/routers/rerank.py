"""``POST /v1/rerank``.

The embeddings pipeline for a different operation: resolve, check access,
redact, reserve, call the provider, record, settle. What differs:

* **Metered by input token**, the query and the documents together. A reranker
  scores each document against the query, so the reservation counts the query
  once per document, as the provider tokenises each pair (Melious reported 251
  billed tokens for a one-line query and three one-line documents). The settled
  figure is the provider's own (``usage``, or Melious's ``meta``); when it
  reports none, the estimate stands, as for embeddings. There is no output
  rate: scores are not text.
* **Redaction applies to the query and every document** (they leave for the
  provider, and a document is exactly the text an embeddings route would be
  asked to index). The reply echoes the *caller's* document text by index,
  never the provider's copy, so there is nothing to restore.
* **The shape is normalised** in ``gateway.rerank``: providers disagree on the
  document form, ``return_documents`` and where usage lives.
* **Bounded**: ``GATEWAY_RERANK__MAX_DOCUMENTS`` and ``MAX_TOTAL_BYTES``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway import rerank as shape
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
from gateway.models import ApiSurface
from gateway.routers import _metered
from gateway.routing import affinity_key
from gateway.schemas import RerankRequest
from gateway.upstream import UpstreamError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["openai"])

SURFACE = ApiSurface.RERANK


@router.post("/rerank", response_model=None)
async def create_rerank(
    request: Request,
    body: RerankRequest,
    principal: PrincipalDep,
    session: SessionDep,
    settings: SettingsDep,
    quota: QuotaDep,
    redactor: RedactorDep,
    providers: ProvidersDep,
    estimator: EstimatorDep,
) -> JSONResponse:
    texts = shape.validate(
        query=body.query,
        documents=body.documents,
        top_n=body.top_n,
        max_documents=settings.rerank.max_documents,
        max_total_bytes=settings.rerank.max_total_bytes,
    )
    top_n = min(body.top_n, len(texts)) if body.top_n is not None else None

    request_id = _metered.request_id_for(request, settings)
    plan = await _metered.resolve_plan(
        session,
        body.model,
        principal=principal,
        surface=SURFACE,
        affinity=affinity_key(request.headers, body),
        fx=request.app.state.fx,
        currency=settings.billing_currency,
        catalogue=request.app.state.catalogue,
    )

    async def serve(resolved: _metered.ResolvedModel) -> JSONResponse:
        model = resolved.model
        upstream = await _metered.resolve_upstream(providers, resolved)

        # -- redaction ----------------------------------------------------------
        # One message per string, the query first, so the one engine covers every
        # route; placeholders are deterministic, so a query and the documents it
        # names agree with each other.
        outcome = await _metered.redact_or_block(
            request,
            [{"role": "user", "content": text} for text in [body.query, *texts]],
            redactor=redactor,
            session=session,
            principal=principal,
            model=model,
            settings=settings,
            surface=SURFACE,
            request_id=request_id,
        )
        redacted = [str(message.get("content") or "") for message in outcome.messages]
        query, documents = redacted[0], redacted[1:]

        # -- reservation --------------------------------------------------------
        # The query is read once per document by the provider.
        query_tokens = estimator.count_text(query)
        prompt_tokens = sum(query_tokens + estimator.count_text(text) for text in documents)
        _metered.enforce_input_limit(model, prompt_tokens)
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
            worst_case=TokenCounts(prompt=prompt_tokens, completion=0),
            outcome=outcome,
            streamed=False,
        )
        if isinstance(metered, JSONResponse):
            return metered

        # Only what the gateway has normalised goes upstream: other fields pass
        # through, `return_documents` does not (the reply is built here).
        payload: dict[str, Any] = body.model_dump(exclude_unset=True)
        payload.pop("return_documents", None)
        payload["model"] = resolved.upstream_model
        payload["query"] = query
        payload["documents"] = documents
        if top_n is not None:
            payload["top_n"] = top_n
        payload = metered.shape_payload(payload, surface=SURFACE)

        try:
            response = await upstream.rerank(payload, request_id=request_id)
        except UpstreamError as exc:
            raise await metered.upstream_unreachable(exc) from exc

        if response.status_code >= 400:
            return await metered.upstream_refused(
                status_code=response.status_code,
                message=_metered.error_text(response.payload),
                content=response.payload,
            )

        upstream_payload = response.payload or {}
        # Feeds usage (from `usage` or Melious's `meta`), and the model and
        # provider that actually served it.
        metered.accounting.observe_payload(
            shape.ledger_payload(upstream_payload), headers=response.headers
        )
        try:
            body_out = shape.reply(
                upstream_payload,
                texts=texts,
                top_n=top_n,
                return_documents=bool(body.return_documents),
                model=resolved.requested_name,
                prompt_tokens=prompt_tokens,
            )
        except ValueError as exc:
            raise await metered.upstream_unreachable(
                exc, message=f"The provider returned an unusable rerank reply: {exc}."
            ) from exc
        return JSONResponse(
            status_code=response.status_code,
            content=body_out,
            background=metered.completed_after_response(upstream_status=response.status_code),
        )

    return await _metered.serve_with_failover(session, plan, serve)
