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
import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from gateway.access import accessible_model_by_name
from gateway.accounting import TokenCounts
from gateway.accounting.recorder import RequestAccounting, RequestContext
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
    ModelNotFoundError,
    ServiceUnavailableError,
    UpstreamUnavailableError,
    error_response,
)
from gateway.models import ModelKind, UsageStatus
from gateway.providers import ProviderConfigurationError
from gateway.quota import QuotaAmounts, QuotaExceeded, QuotaUnavailable
from gateway.routers.chat import estimate_cost
from gateway.schemas import EmbeddingRequest
from gateway.upstream import UpstreamError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["openai"])


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

    request_id = request.headers.get(settings.request_id_header) or uuid.uuid4().hex

    stmt = accessible_model_by_name(
        body.model,
        user_id=principal.user.id,
        group_ids=[principal.billing_group.id],
    )
    model = (await session.execute(stmt)).scalars().first()
    if model is None:
        raise ModelNotFoundError(
            f"The model {body.model!r} does not exist or you do not have access to it."
        )
    if model.kind is not ModelKind.EMBEDDING:
        # Refused here rather than forwarded, so the answer names the mistake
        # instead of arriving as whatever error that provider returns for a chat
        # model asked to embed.
        raise BadRequestError(
            f"{model.name!r} is a chat model. Use /v1/chat/completions for it, or "
            "choose a model catalogued as an embedding model.",
            code="wrong_model_kind",
        )

    try:
        upstream = await providers.upstream_for(model.provider)
    except ProviderConfigurationError as exc:
        logger.error("provider misconfigured for model %s: %s", model.name, exc)
        raise UpstreamUnavailableError(str(exc)) from exc

    # -- redaction ----------------------------------------------------------
    # Reuses the message-shaped interface so one engine covers both routes: each
    # input becomes a user message, and the redacted text comes back out. There
    # is no response path to restore — the response is an array of numbers.
    outcome = await redactor.redact_request([{"role": "user", "content": text} for text in texts])
    redacted = [str(message.get("content") or "") for message in outcome.messages]

    # -- reservation --------------------------------------------------------
    prompt_tokens = sum(estimator.count_text(text) for text in redacted)
    counts = TokenCounts(prompt=prompt_tokens, completion=0)
    reserve_cost = estimate_cost(model, counts, currency=settings.billing_currency)

    try:
        reservation = await quota.check_and_reserve(
            session,
            principal.quota_subject,
            QuotaAmounts(tokens=Decimal(counts.total), cost=reserve_cost.total),
        )
    except QuotaExceeded as exc:
        logger.info("quota refusal for user=%s: %s", principal.user.id, exc)
        return error_response(
            f"Quota exceeded. {exc}",
            status_code=429,
            type_="insufficient_quota",
            code="quota_exceeded",
            headers={"retry-after": str(exc.retry_after_seconds)},
        )
    except QuotaUnavailable as exc:
        raise ServiceUnavailableError(
            "Quota state is temporarily unavailable, so the request was refused "
            "rather than served unmetered."
        ) from exc

    accounting = RequestAccounting(
        context=RequestContext(
            request_id=request_id,
            model_name=model.name,
            currency=settings.billing_currency,
            streamed=False,
            user_id=principal.user.id,
            group_id=principal.billing_group.id,
            api_key_id=principal.api_key.id if principal.api_key else None,
            model_id=model.id,
            estimated_prompt_tokens=prompt_tokens,
            redaction_engine=outcome.engine,
            redacted_entity_count=outcome.entity_count,
        ),
        session_factory=request.app.state.session_factory,
        settings=settings,
        estimator=estimator,
        model=model,
    )
    await accounting.begin()

    payload: dict[str, Any] = body.model_dump(exclude_unset=True)
    payload["model"] = model.upstream_model
    # Single string in, single string out: a provider may index its response by
    # position, and turning one string into a one-element list would change the
    # shape the caller gets back.
    payload["input"] = redacted[0] if isinstance(body.input, str) else redacted

    try:
        response = await upstream.embeddings(payload, request_id=request_id)
    except UpstreamError as exc:
        await accounting.finalise(
            status=UsageStatus.UPSTREAM_ERROR,
            error_code="upstream_unreachable",
            error_message=str(exc),
        )
        await quota.release(reservation)
        raise UpstreamUnavailableError("The upstream provider could not be reached.") from exc

    if response.status_code >= 400:
        await accounting.finalise(
            status=UsageStatus.UPSTREAM_ERROR,
            upstream_status=response.status_code,
            error_code="upstream_error",
            error_message=_error_text(response.payload),
        )
        # Released, not settled: a refused request consumed no tokens, and
        # holding the reservation would charge the caller for the provider's
        # failure until the window rolled over.
        await quota.release(reservation)
        return JSONResponse(
            status_code=response.status_code,
            content=response.payload if response.payload is not None else {},
        )

    if response.payload is not None:
        # Feeds usage, and the model/provider that actually served it.
        accounting.observe_payload(response.payload)

    actuals = await accounting.finalise(
        status=UsageStatus.COMPLETED, upstream_status=response.status_code
    )
    await quota.settle(reservation, actuals)

    body_out = dict(response.payload or {})
    # Report our model name, not the upstream's, exactly as the chat route does:
    # clients compare the echoed name with what they sent. The original is in the
    # ledger.
    if body_out.get("model") is not None:
        body_out["model"] = model.name
    return JSONResponse(status_code=response.status_code, content=body_out)


def _error_text(payload: dict[str, Any] | None) -> str | None:
    if not payload:
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or "")[:500]
    return str(error or "")[:500] or None
