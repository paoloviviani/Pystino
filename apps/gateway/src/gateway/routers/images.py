"""``POST /v1/images/generations``.

Metered like everything else, with one thing this gateway had not needed
before: **a unit of billing that is not a token.**

Most image models are priced per generated image, by size, and report no token
usage at all. Some — the newer OpenAI ones — are priced per token and do report
usage. Both are served here, and the price row decides which applies:
``per_image`` set means ``n`` images are charged at that rate; token rates
apply to whatever usage came back. A model can carry both, and a model can
carry neither, in which case the row records zero cost with a null price and
the gap shows up in reporting rather than hiding in a rounding.

``image_size`` is recorded on every row even though nothing prices by size yet.
It is the difference between being able to introduce size-dependent pricing
later and having a ledger that cannot say what was bought (ADR 0030).

The prompt is redacted like any other. The images are not touched: a base64
PNG is not text, and there is nothing in it a placeholder could be restored
into. The ``revised_prompt`` a provider echoes back **is** restored, because it
is derived from the text we redacted and would otherwise show the caller a
placeholder where their own word had been.
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
    RedactorDep,
    SessionDep,
    SettingsDep,
)
from gateway.errors import BadRequestError, error_payload
from gateway.models import ApiSurface
from gateway.routers import _metered
from gateway.schemas import ImageGenerationRequest
from gateway.upstream import UpstreamError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["openai"])

SURFACE = ApiSurface.IMAGES


@router.post("/images/generations", response_model=None)
async def create_image(
    request: Request,
    body: ImageGenerationRequest,
    principal: PrincipalDep,
    session: SessionDep,
    settings: SettingsDep,
    quota: QuotaDep,
    redactor: RedactorDep,
    providers: ProvidersDep,
    estimator: EstimatorDep,
) -> JSONResponse:
    if not body.prompt.strip():
        raise BadRequestError("'prompt' must not be empty.")

    request_id = _metered.request_id_for(request, settings)
    model = await _metered.resolve_model(session, body.model, principal=principal, surface=SURFACE)
    upstream = await _metered.resolve_upstream(providers, model)

    outcome = await redactor.redact_request([{"role": "user", "content": body.prompt}])
    redacted_prompt = str(outcome.messages[0].get("content") or "") if outcome.messages else ""

    # The reservation has to cover the pictures, not just the prompt: on a
    # per-image-priced model the prompt is a rounding error and `n` is the
    # bill. Asked for up front, because `n` is in the request.
    prompt_tokens = estimator.count_text(redacted_prompt)
    worst_case = TokenCounts(prompt=prompt_tokens, completion=0, images=body.n)

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
        worst_case=worst_case,
        outcome=outcome,
        streamed=False,
    )
    if isinstance(metered, JSONResponse):
        return metered

    payload = body.upstream_payload(outcome, upstream_model=model.upstream_model)

    try:
        response = await upstream.images(payload, request_id=request_id)
    except UpstreamError as exc:
        raise await metered.upstream_unreachable(exc) from exc

    if response.status_code >= 400:
        return await metered.upstream_refused(
            status_code=response.status_code,
            message=_metered.error_text(response.payload),
            content=response.payload
            or error_payload("The upstream provider returned an error.", type_="api_error"),
        )

    out = dict(response.payload or {})
    metered.accounting.observe_payload(out)
    # Counted from what came back, not from what was asked for: a provider that
    # returns three images for a request for four must be billed for three.
    produced = sum(1 for entry in out.get("data") or [] if isinstance(entry, dict))
    metered.accounting.observe_images(produced, _reported_size(out) or body.size)

    for entry in out.get("data") or []:
        if isinstance(entry, dict) and isinstance(entry.get("revised_prompt"), str):
            entry["revised_prompt"] = await redactor.redact_response_text(
                entry["revised_prompt"], outcome
            )

    await metered.completed(upstream_status=response.status_code)
    return JSONResponse(status_code=response.status_code, content=out)


def _reported_size(payload: dict[str, object]) -> str | None:
    """What the provider says it produced, which can differ from what was asked.

    ``size: "auto"`` is the common case: the caller did not choose and the
    provider did. Recording the provider's answer means the ledger says what
    was actually generated.
    """
    size = payload.get("size")
    return size if isinstance(size, str) and size else None
