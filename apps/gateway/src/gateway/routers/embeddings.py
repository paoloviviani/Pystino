"""``POST /v1/embeddings``.

The same pipeline as a chat completion, minus everything streaming forces
(ADR 0028): resolve the model, check access, redact, reserve quota, call the
provider, record what it cost, settle.

Three differences, all forced by the shape of the operation rather than chosen:

* **No streaming**, so no SSE stages and no buffering.
* **``completion_tokens`` is always 0.** Cortecs documents this explicitly, and
  it follows from embeddings not being generated text. Cost is the input price
  alone, and an embedding model that happens to carry an output price is not an
  error — it is simply never charged.
* **The reservation counts input only.** ``default_max_output_tokens`` is a guess
  at completion length; applying it here would reserve thousands of tokens that
  cannot be spent and refuse callers who are well under budget.

On redaction, and this is the part worth knowing: the vector represents the text
**after** substitution. Redacting anyway, because an embeddings route that
forwarded raw text would let anyone bypass redaction by embedding the document
instead of asking about it. Placeholders are deterministic, so an indexed
document and a later query agree with each other — see ADR 0028 for what that
does and does not buy.
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
from gateway.errors import (
    BadRequestError,
)
from gateway.models import ApiSurface
from gateway.routers import _metered
from gateway.schemas import EmbeddingRequest
from gateway.upstream import UpstreamError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["openai"])

SURFACE = ApiSurface.EMBEDDINGS


@router.post("/embeddings", response_model=None)
async def create_embeddings(
    request: Request,
    body: EmbeddingRequest,
    principal: PrincipalDep,
    session: SessionDep,
    settings: SettingsDep,
    quota: QuotaDep,
    redactor: RedactorDep,
    providers: ProvidersDep,
    estimator: EstimatorDep,
) -> JSONResponse:
    texts = body.texts()
    if not texts or all(not text for text in texts):
        raise BadRequestError("'input' must contain at least one non-empty string.")

    request_id = _metered.request_id_for(request, settings)
    model = await _metered.resolve_model(session, body.model, principal=principal, surface=SURFACE)
    upstream = await _metered.resolve_upstream(providers, model)

    # -- redaction ----------------------------------------------------------
    # Reuses the message-shaped interface so one engine covers both routes: each
    # input becomes a user message, and the redacted text comes back out. There
    # is no response path to restore — the response is an array of numbers.
    outcome = await _metered.redact_or_block(
        request,
        [{"role": "user", "content": text} for text in texts],
        redactor=redactor,
        session=session,
        principal=principal,
        model=model,
        settings=settings,
        surface=SURFACE,
        request_id=request_id,
    )
    redacted = [str(message.get("content") or "") for message in outcome.messages]

    # -- reservation --------------------------------------------------------
    prompt_tokens = sum(estimator.count_text(text) for text in redacted)
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
        worst_case=TokenCounts(prompt=prompt_tokens, completion=0),
        outcome=outcome,
        streamed=False,
    )
    if isinstance(metered, JSONResponse):
        return metered

    payload: dict[str, Any] = body.model_dump(exclude_unset=True)
    payload["model"] = model.upstream_model
    # Single string in, single string out: a provider may index its response by
    # position, and turning one string into a one-element list would change the
    # shape the caller gets back.
    payload["input"] = redacted[0] if isinstance(body.input, str) else redacted
    payload = metered.shape_payload(payload, surface=SURFACE)

    try:
        response = await upstream.embeddings(payload, request_id=request_id)
    except UpstreamError as exc:
        raise await metered.upstream_unreachable(exc) from exc

    if response.status_code >= 400:
        return await metered.upstream_refused(
            status_code=response.status_code,
            message=_metered.error_text(response.payload),
            content=response.payload,
        )

    if response.payload is not None:
        # Feeds usage, and the model/provider that actually served it.
        metered.accounting.observe_payload(response.payload, headers=response.headers)

    body_out = dict(response.payload or {})
    # Report our model name, not the upstream's, exactly as the chat route does:
    # clients compare the echoed name with what they sent. The original is in the
    # ledger.
    if body_out.get("model") is not None:
        body_out["model"] = model.name
    # Finalised after the answer is sent, not before it (ADR 0060).
    return JSONResponse(
        status_code=response.status_code,
        content=body_out,
        background=metered.completed_after_response(upstream_status=response.status_code),
    )
