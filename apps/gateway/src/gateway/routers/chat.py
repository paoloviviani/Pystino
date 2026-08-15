"""``POST /v1/chat/completions``.

The ordering here is the whole point of the endpoint, so it is spelled out:

1. resolve the model and check the billing group may use it;
2. redact the outbound messages;
3. count the prompt locally (needed for the reservation, and as the fallback if
   the upstream never reports usage);
4. **check quotas and reserve** the worst case — before any money is spent;
5. create the in-progress usage row — before the upstream call, so a client that
   vanishes still leaves a record;
6. call the upstream, forcing ``stream_options.include_usage``;
7. stream through the pipeline, accumulating usage and text as we go;
8. **finalise accounting and settle the reservation** — after, on every exit path
   including client disconnect.

Stage order inside the pipeline matters as much as step order. Usage capture is
placed *before* usage stripping, so accounting sees the token counts even when the
client never asked for them and will not receive them.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from decimal import Decimal
from typing import Any

import orjson
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.access import accessible_model_by_name
from gateway.accounting import (
    RequestAccounting,
    RequestContext,
    TokenCounts,
    compute_cost,
    select_price,
)
from gateway.accounting.cost import CostBreakdown, CurrencyMismatch
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
    error_payload,
    error_response,
)
from gateway.models import ModelDef, UsageStatus
from gateway.providers import ProviderConfigurationError
from gateway.quota import (
    QuotaAmounts,
    QuotaEngine,
    QuotaExceeded,
    QuotaUnavailable,
    Reservation,
)
from gateway.redaction import RedactionOutcome, Redactor
from gateway.schemas import ChatCompletionRequest
from gateway.sse import SSEEvent, chain, iter_sse_events
from gateway.sse.pipeline import StreamStage, tap
from gateway.upstream import OpenAICompatibleUpstream, UpstreamError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["openai"])

_STREAM_HEADERS = {
    "cache-control": "no-cache",
    "connection": "keep-alive",
    # Stops nginx buffering the stream into uselessness.
    "x-accel-buffering": "no",
}


async def load_model_for_caller(
    session: AsyncSession, name: str, *, user_id: uuid.UUID | None, group_id: uuid.UUID
) -> ModelDef:
    """Fetch a model this caller is allowed to use.

    Access is the union of group and personal grants; the predicate lives in
    gateway.access so this and /v1/models cannot drift apart.

    "Not available to you" and "does not exist" deliberately return the same
    404: which models another group can reach is not this caller's business.
    """
    stmt = accessible_model_by_name(name, user_id=user_id, group_ids=[group_id])
    model = (await session.execute(stmt)).scalars().first()
    if model is None:
        raise ModelNotFoundError(
            f"The model {name!r} does not exist or you do not have access to it."
        )
    return model


def rename_model_stage(client_facing_name: str) -> StreamStage:
    """Report our model name, not the upstream's.

    We rewrite ``model`` on the way out, so leaving the provider's name in the
    response would leak the mapping and break clients that compare the echoed
    name with what they sent.
    """

    async def stage(events: AsyncIterator[SSEEvent]) -> AsyncIterator[SSEEvent]:
        async for event in events:
            payload = event.json()
            if isinstance(payload, dict) and payload.get("model") not in (
                None,
                client_facing_name,
            ):
                payload["model"] = client_facing_name
                event.replace_json(payload)
            yield event

    return stage


def usage_visibility_stage(client_wants_usage: bool) -> StreamStage:
    """Strip token usage from the client's stream unless it asked for it.

    Two distinct cases, and conflating them corrupts the stream:

    * a *usage-only* frame (usage present, ``choices`` empty) is dropped whole;
    * a frame carrying both usage and content has only its ``usage`` key removed,
      because dropping it would swallow generated text.
    """

    async def stage(events: AsyncIterator[SSEEvent]) -> AsyncIterator[SSEEvent]:
        async for event in events:
            if client_wants_usage:
                yield event
                continue

            payload = event.json()
            if isinstance(payload, dict) and payload.get("usage") is not None:
                if not payload.get("choices"):
                    continue
                payload.pop("usage", None)
                event.replace_json(payload)
            yield event

    return stage


def build_upstream_payload(
    body: ChatCompletionRequest,
    *,
    outcome: RedactionOutcome,
    upstream_model: str,
) -> dict[str, Any]:
    """The request we actually send.

    ``exclude_unset`` forwards exactly what the client sent — including every
    parameter this gateway knows nothing about — and nothing it did not. Adding
    defaults of our own would silently change model behaviour.
    """
    payload = body.model_dump(exclude_unset=True)
    payload["model"] = upstream_model
    payload["messages"] = outcome.messages

    if body.stream:
        # Without this, a streamed response carries no token counts at all and
        # accounting would record zero for every streaming request. Merged rather
        # than replaced, so other stream options the client set survive.
        options = dict(body.stream_options or {})
        options["include_usage"] = True
        payload["stream_options"] = options

    return payload


def estimate_cost(model: ModelDef, counts: TokenCounts, *, currency: str) -> CostBreakdown:
    price = select_price(list(model.prices))
    try:
        return compute_cost(counts, price, billing_currency=currency, model_name=model.name)
    except CurrencyMismatch as exc:
        # Refuse rather than bill in the wrong currency.
        raise BadRequestError(str(exc), code="price_currency_mismatch") from exc


# response_model=None because this endpoint returns either a JSONResponse or a
# StreamingResponse, and FastAPI would otherwise try to build a Pydantic response
# model from that union. The response body is OpenAI's schema, not ours, and is
# passed through rather than re-validated.
@router.post("/chat/completions", response_model=None)
async def chat_completions(
    request: Request,
    body: ChatCompletionRequest,
    principal: PrincipalDep,
    session: SessionDep,
    settings: SettingsDep,
    quota: QuotaDep,
    redactor: RedactorDep,
    providers: ProvidersDep,
    estimator: EstimatorDep,
) -> JSONResponse | StreamingResponse:
    if not body.messages:
        raise BadRequestError("'messages' must contain at least one message.")

    request_id = request.headers.get(settings.request_id_header) or uuid.uuid4().hex
    model = await load_model_for_caller(
        session,
        body.model,
        user_id=principal.user.id,
        group_id=principal.billing_group.id,
    )

    # Which endpoint serves this model, with that endpoint's credentials. A
    # misconfigured provider surfaces here, before any accounting row exists.
    try:
        upstream = await providers.upstream_for(model.provider)
    except ProviderConfigurationError as exc:
        logger.error("provider misconfigured for model %s: %s", model.name, exc)
        raise UpstreamUnavailableError(str(exc)) from exc

    # -- redaction (step 2) -------------------------------------------------
    outcome = await redactor.redact_request(body.messages)

    # -- local counting and reservation estimate (steps 3-4) ----------------
    prompt_tokens = estimator.count_messages(outcome.messages)
    max_output = (
        body.requested_max_output_tokens(settings.quota.default_max_output_tokens)
        * body.choice_count()
    )
    worst_case = TokenCounts(prompt=prompt_tokens, completion=max_output)
    worst_case_cost = estimate_cost(model, worst_case, currency=settings.billing_currency)

    try:
        reservation = await quota.check_and_reserve(
            session,
            principal.quota_subject,
            QuotaAmounts(
                tokens=Decimal(worst_case.total),
                cost=worst_case_cost.total,
            ),
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

    # -- usage row before the upstream call (step 5) ------------------------
    accounting = RequestAccounting(
        context=RequestContext(
            request_id=request_id,
            model_name=model.name,
            currency=settings.billing_currency,
            streamed=body.stream,
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

    payload = build_upstream_payload(body, outcome=outcome, upstream_model=model.upstream_model)

    if body.stream:
        return await _stream_response(
            request=request,
            payload=payload,
            model=model,
            body=body,
            outcome=outcome,
            redactor=redactor,
            upstream=upstream,
            accounting=accounting,
            quota=quota,
            reservation=reservation,
            request_id=request_id,
        )

    return await _buffered_response(
        payload=payload,
        model=model,
        outcome=outcome,
        redactor=redactor,
        upstream=upstream,
        accounting=accounting,
        quota=quota,
        reservation=reservation,
        request_id=request_id,
    )


async def _buffered_response(
    *,
    payload: dict[str, Any],
    model: ModelDef,
    outcome: RedactionOutcome,
    redactor: Redactor,
    upstream: OpenAICompatibleUpstream,
    accounting: RequestAccounting,
    quota: QuotaEngine,
    reservation: Reservation,
    request_id: str,
) -> JSONResponse:
    """Non-streaming completion."""
    try:
        response = await upstream.chat_completion(payload, request_id=request_id)
    except UpstreamError as exc:
        actuals = await accounting.finalise(
            status=UsageStatus.UPSTREAM_ERROR,
            error_code="upstream_unreachable",
            error_message=str(exc),
        )
        await quota.settle(reservation, actuals)
        raise UpstreamUnavailableError("The upstream provider could not be reached.") from exc

    if not response.ok:
        actuals = await accounting.finalise(
            status=UsageStatus.UPSTREAM_ERROR,
            upstream_status=response.status_code,
            error_code="upstream_error",
            error_message=(response.raw[:500].decode("utf-8", "replace") if response.raw else None),
        )
        await quota.settle(reservation, actuals)
        # Pass the provider's own error through: clients act on these.
        content = response.payload or error_payload(
            "The upstream provider returned an error.", type_="api_error"
        )
        return JSONResponse(status_code=response.status_code, content=content)

    body_payload = response.payload or {}
    accounting.observe_payload(body_payload)

    # Report our model name, and restore any placeholders we introduced.
    body_payload["model"] = model.name
    for choice in body_payload.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            message["content"] = await redactor.redact_response_text(message["content"], outcome)

    actuals = await accounting.finalise(
        status=UsageStatus.COMPLETED, upstream_status=response.status_code
    )
    await quota.settle(reservation, actuals)

    return JSONResponse(status_code=200, content=body_payload)


async def _stream_response(
    *,
    request: Request,
    payload: dict[str, Any],
    model: ModelDef,
    body: ChatCompletionRequest,
    outcome: RedactionOutcome,
    redactor: Redactor,
    upstream: OpenAICompatibleUpstream,
    accounting: RequestAccounting,
    quota: QuotaEngine,
    reservation: Reservation,
    request_id: str,
) -> JSONResponse | StreamingResponse:
    """Streaming completion.

    The upstream stream is opened *before* the response is returned, so that an
    upstream error can still become a real HTTP status. Once a StreamingResponse
    has been handed back, the status line is already committed and the only way to
    report a failure is an error frame inside the stream.
    """
    stack = AsyncExitStack()
    try:
        upstream_response = await stack.enter_async_context(
            upstream.stream_chat_completion(payload, request_id=request_id)
        )
    except UpstreamError as exc:
        await stack.aclose()
        actuals = await accounting.finalise(
            status=UsageStatus.UPSTREAM_ERROR,
            error_code="upstream_unreachable",
            error_message=str(exc),
        )
        await quota.settle(reservation, actuals)
        raise UpstreamUnavailableError("The upstream provider could not be reached.") from exc

    if upstream_response.status_code >= 400:
        raw = await upstream_response.aread()
        await stack.aclose()
        actuals = await accounting.finalise(
            status=UsageStatus.UPSTREAM_ERROR,
            upstream_status=upstream_response.status_code,
            error_code="upstream_error",
            error_message=raw[:500].decode("utf-8", "replace") if raw else None,
        )
        await quota.settle(reservation, actuals)
        try:
            content = orjson.loads(raw) if raw else None
        except orjson.JSONDecodeError:
            content = None
        return JSONResponse(
            status_code=upstream_response.status_code,
            content=content
            or error_payload("The upstream provider returned an error.", type_="api_error"),
        )

    async def body_iterator() -> AsyncIterator[bytes]:
        completed = False
        try:
            events = iter_sse_events(upstream_response.aiter_bytes())
            pipeline = chain(
                events,
                # Observe first: accounting must see usage frames that the client
                # will not be shown.
                tap(accounting.observe_event),
                rename_model_stage(model.name),
                redactor.response_stage(outcome),
                usage_visibility_stage(body.client_wants_usage()),
            )
            async for event in pipeline:
                yield event.encode()
                await accounting.maybe_flush()
            completed = True
        finally:
            await stack.aclose()
            if completed:
                actuals = await accounting.finalise(
                    status=UsageStatus.COMPLETED,
                    upstream_status=upstream_response.status_code,
                )
                await quota.settle(reservation, actuals)
            else:
                # The client went away (or something failed mid-stream). We are
                # very likely inside a cancelled task, where awaiting anything
                # re-raises immediately — so finalisation is handed to a detached
                # task instead of awaited. Without this, the tokens already
                # generated and paid for would never be recorded.
                _spawn_finalisation(
                    request,
                    accounting=accounting,
                    quota=quota,
                    reservation=reservation,
                    upstream_status=upstream_response.status_code,
                )

    return StreamingResponse(
        body_iterator(),
        media_type="text/event-stream",
        headers=_STREAM_HEADERS,
    )


def _spawn_finalisation(
    request: Request,
    *,
    accounting: RequestAccounting,
    quota: QuotaEngine,
    reservation: Reservation,
    upstream_status: int | None,
) -> None:
    """Finalise a disconnected stream outside the cancelled task."""

    async def finalise() -> None:
        try:
            actuals = await accounting.finalise(
                status=UsageStatus.CLIENT_DISCONNECTED,
                upstream_status=upstream_status,
            )
            await quota.settle(reservation, actuals)
        except Exception:
            logger.exception("failed to finalise a disconnected stream")

    task = asyncio.create_task(finalise())
    # Keep a strong reference: a task with no references can be garbage
    # collected mid-flight, which would lose exactly the write we are trying to
    # protect.
    tasks: set[asyncio.Task[None]] = request.app.state.background_tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)
