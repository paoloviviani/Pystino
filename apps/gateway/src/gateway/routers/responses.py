"""``POST /v1/responses`` — OpenAI's Responses API.

The same metering as every other route (``_metered``), over a different request
shape and a different streaming protocol. What is specific to this surface:

**``input`` is polymorphic.** A bare string, or a list of items that may be
chat-style messages or tool outputs. Both are normalised into messages for
redaction and counting, then written back in the shape they arrived in — a
caller who sent a string gets its redacted form back as a string, because
turning it into a one-element list would change what the provider sees.

**Statefulness is refused.** ``previous_response_id`` and ``store`` let a
provider hold the conversation, which would put prompt text outside this
gateway's redaction and outside its ledger: the tokens of a stored prefix are
billed to us on every follow-up and we would have no record of what they were.
Refused with a message that says so rather than forwarded (ADR 0030).

**Usage arrives once, on a terminal event.** ``response.completed`` — and also
``response.incomplete`` and ``response.failed``, because a truncated generation
still consumed tokens.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from typing import Any

import orjson
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

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
from gateway.routers.chat import STREAM_HEADERS, settle_completed, spawn_finalisation
from gateway.schemas import ResponsesRequest
from gateway.sse import chain, iter_sse_events
from gateway.sse.pipeline import tap
from gateway.upstream import UpstreamError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["openai"])

SURFACE = ApiSurface.RESPONSES


@router.post("/responses", response_model=None)
async def create_response(
    request: Request,
    body: ResponsesRequest,
    principal: PrincipalDep,
    session: SessionDep,
    settings: SettingsDep,
    quota: QuotaDep,
    redactor: RedactorDep,
    providers: ProvidersDep,
    estimator: EstimatorDep,
) -> JSONResponse | StreamingResponse:
    if body.previous_response_id is not None or body.store:
        # Refused rather than forwarded: a stored prefix is billed on every
        # follow-up and this gateway would have no record of what it contained,
        # so the ledger could not explain its own totals.
        raise BadRequestError(
            "'previous_response_id' and 'store' are not supported: this gateway keeps "
            "no server-side conversation state. Send the full conversation in 'input'.",
            code="stateful_responses_unsupported",
        )

    messages = body.as_messages()
    if not messages:
        raise BadRequestError("'input' must contain at least one message or a non-empty string.")

    request_id = _metered.request_id_for(request, settings)
    model = await _metered.resolve_model(session, body.model, principal=principal, surface=SURFACE)
    upstream = await _metered.resolve_upstream(providers, model)

    outcome = await _metered.redact_or_block(
        request,
        messages,
        redactor=redactor,
        session=session,
        principal=principal,
        model=model,
        settings=settings,
        surface=SURFACE,
        request_id=request_id,
    )

    prompt_tokens = estimator.count_messages(outcome.messages)
    # `instructions` is a system prompt by another name and is charged like one.
    prompt_tokens += estimator.count_text(body.instructions or "")
    max_output = body.max_output_tokens or settings.quota.default_max_output_tokens
    worst_case = TokenCounts(prompt=prompt_tokens, completion=max_output)

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
        streamed=body.stream,
    )
    if isinstance(metered, JSONResponse):
        return metered

    payload = body.upstream_payload(outcome, upstream_model=model.upstream_model)
    payload = metered.shape_payload(payload, surface=SURFACE)

    if body.stream:
        return await _stream(
            request=request,
            payload=payload,
            model_name=model.name,
            outcome=outcome,
            redactor=redactor,
            upstream=upstream,
            metered=metered,
            request_id=request_id,
        )

    try:
        response = await upstream.responses(payload, request_id=request_id)
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
    metered.accounting.observe_payload(out, headers=response.headers)

    # Our model name, not the provider's, and placeholders restored wherever
    # this surface repeats the answer.
    if out.get("model") is not None:
        out["model"] = model.name
    if assembled := _assembled_text(out):
        _rewrite_all(out, await redactor.redact_response_text(assembled, outcome))

    await metered.completed(upstream_status=response.status_code)
    return JSONResponse(status_code=response.status_code, content=out)


def _assembled_text(payload: dict[str, Any]) -> str:
    text = payload.get("output_text")
    return text if isinstance(text, str) else ""


def _rewrite_all(payload: dict[str, Any], restored: str) -> None:
    """Write the restored text back everywhere the response repeats it.

    Restoring once and copying is deliberate: the substitution is a pure
    mapping over the same string, so running it per field would produce the
    same result at N times the cost, and any divergence between the fields
    would be a bug the caller could see.
    """
    payload["output_text"] = restored
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for block in item.get("content") or []:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                block["text"] = restored


async def _stream(
    *,
    request: Request,
    payload: dict[str, Any],
    model_name: str,
    outcome: Any,
    redactor: Any,
    upstream: Any,
    metered: _metered.Metered,
    request_id: str,
) -> JSONResponse | StreamingResponse:
    """Open the upstream stream before committing to a 200.

    Once a StreamingResponse is handed back the status line is already sent and
    a failure can only be reported as an error frame inside the stream, which
    most clients handle badly.
    """
    stack = AsyncExitStack()
    try:
        upstream_response = await stack.enter_async_context(
            upstream.stream_responses(payload, request_id=request_id)
        )
    except UpstreamError as exc:
        await stack.aclose()
        raise await metered.upstream_unreachable(exc) from exc

    if upstream_response.status_code >= 400:
        raw = await upstream_response.aread()
        await stack.aclose()
        try:
            content = orjson.loads(raw) if raw else None
        except orjson.JSONDecodeError:
            content = None
        return await metered.upstream_refused(
            status_code=upstream_response.status_code,
            message=raw[:500].decode("utf-8", "replace") if raw else None,
            content=content
            or error_payload("The upstream provider returned an error.", type_="api_error"),
        )

    # The serving endpoint is a response *header* on some counterparties, and a
    # stream's frames never carry it. Told once, when the stream opens.
    metered.accounting.observe_served_by(
        metered.accounting.plugin.read_served_by(None, upstream_response.headers)
    )

    async def body_iterator() -> AsyncIterator[bytes]:
        completed = False
        try:
            pipeline = chain(
                iter_sse_events(upstream_response.aiter_bytes()),
                # Observe first: accounting must see the terminal usage event
                # whatever the stages downstream do with it.
                tap(metered.accounting.observe_event),
                _rename_stage(model_name),
                redactor.response_stage(outcome, surface=SURFACE),
            )
            async for event in pipeline:
                yield event.encode()
                await metered.accounting.maybe_flush()
            completed = True
        finally:
            await stack.aclose()
            if completed:
                await settle_completed(
                    request,
                    metered=metered,
                    upstream_status=upstream_response.status_code,
                )
            else:
                spawn_finalisation(
                    request,
                    accounting=metered.accounting,
                    quota=metered.quota,
                    reservation=metered.reservation,
                    upstream_status=upstream_response.status_code,
                )

    return StreamingResponse(
        body_iterator(), media_type="text/event-stream", headers=STREAM_HEADERS
    )


def _rename_stage(client_facing_name: str) -> Any:
    """Report our model name, on the frames that carry one.

    Nested here, unlike the chat route's version: on this surface the model
    lives inside the wrapped response object on lifecycle events, not at the
    top level of every frame.
    """

    async def stage(events: AsyncIterator[Any]) -> AsyncIterator[Any]:
        async for event in events:
            payload = event.json()
            if isinstance(payload, dict):
                target = payload.get("response")
                target = target if isinstance(target, dict) else payload
                if target.get("model") not in (None, client_facing_name):
                    target["model"] = client_facing_name
                    event.replace_json(payload)
            yield event

    return stage
