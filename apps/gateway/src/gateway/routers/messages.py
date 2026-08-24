"""``POST /v1/messages`` — Anthropic's Messages API.

A second shape for the same chat models, metered through the same ledger. The
reference provider serves it from the same base URL with the same credential,
so this is a request/response translation problem and not a second gateway.

Three things are specific to this surface, and each of them is a place where
reusing the OpenAI code would have produced a wrong number rather than an
error:

**Usage arrives in two frames.** ``message_start`` carries the input count,
``message_delta`` a cumulative output count. Handled in ``protocols`` by
merging rather than letting the last frame win.

**``input_tokens`` excludes the cached tokens** — the opposite of OpenAI, where
``prompt_tokens`` includes them. Handled in ``TokenCounts.from_anthropic_usage``.

**Events are named.** ``event: message_start`` before each ``data:`` line, and
clients switch on that name, so a synthesised frame from the redaction stage
has to carry one too.

The system prompt is redacted with the messages, because a name in a system
prompt is exactly as sensitive as one in a user turn.
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
from gateway.routers.chat import STREAM_HEADERS, spawn_finalisation
from gateway.schemas import MessagesRequest
from gateway.sse import SSEEvent, chain, iter_sse_events
from gateway.sse.pipeline import StreamStage, tap
from gateway.upstream import UpstreamError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["anthropic"])

SURFACE = ApiSurface.MESSAGES


@router.post("/messages", response_model=None)
async def create_message(
    request: Request,
    body: MessagesRequest,
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

    request_id = _metered.request_id_for(request, settings)
    model = await _metered.resolve_model(session, body.model, principal=principal, surface=SURFACE)
    upstream = await _metered.resolve_upstream(providers, model)

    outcome = await redactor.redact_request(body.as_messages())

    prompt_tokens = estimator.count_messages(outcome.messages)
    # `max_tokens` is required on this API, so the worst case is exact rather
    # than a configured guess — the one surface where that is true.
    worst_case = TokenCounts(prompt=prompt_tokens, completion=body.max_tokens)

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
        response = await upstream.messages(payload, request_id=request_id)
    except UpstreamError as exc:
        raise await metered.upstream_unreachable(exc) from exc

    if response.status_code >= 400:
        return await metered.upstream_refused(
            status_code=response.status_code,
            message=_anthropic_error_text(response.payload),
            content=response.payload
            or error_payload("The upstream provider returned an error.", type_="api_error"),
        )

    out = dict(response.payload or {})
    metered.accounting.observe_payload(out, headers=response.headers)

    if out.get("model") is not None:
        out["model"] = model.name
    for block in out.get("content") or []:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            block["text"] = await redactor.redact_response_text(block["text"], outcome)

    await metered.completed(upstream_status=response.status_code)
    return JSONResponse(status_code=response.status_code, content=out)


def _anthropic_error_text(payload: Any) -> str | None:
    """Anthropic's error envelope, which is shaped like OpenAI's here.

    ``{"type": "error", "error": {"type": ..., "message": ...}}`` — the same
    nesting, so the shared reader works; kept as a named function so that a
    future divergence has somewhere to go.
    """
    return _metered.error_text(payload)


def _rename_stage(client_facing_name: str) -> StreamStage:
    """Report our model name. Only ``message_start`` carries one."""

    async def stage(events: AsyncIterator[SSEEvent]) -> AsyncIterator[SSEEvent]:
        async for event in events:
            payload = event.json()
            if isinstance(payload, dict) and payload.get("type") == "message_start":
                message = payload.get("message")
                if isinstance(message, dict) and message.get("model") not in (
                    None,
                    client_facing_name,
                ):
                    message["model"] = client_facing_name
                    event.replace_json(payload)
            yield event

    return stage


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
    stack = AsyncExitStack()
    try:
        upstream_response = await stack.enter_async_context(
            upstream.stream_messages(payload, request_id=request_id)
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
                await metered.completed(upstream_status=upstream_response.status_code)
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
