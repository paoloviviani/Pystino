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
from functools import partial
from typing import Any

import orjson
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from llmp_shared import Restored

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
from gateway.protocols import shift_annotations
from gateway.redaction import restore_reasoning_and_arguments
from gateway.routers import _metered
from gateway.routers.chat import STREAM_HEADERS, settle_completed, spawn_finalisation
from gateway.routing import affinity_key
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

    async def serve(resolved: _metered.ResolvedModel) -> JSONResponse | StreamingResponse:
        # The target, when the name sent was an alias (ADR 0101); everything below
        # follows what serves the request.
        model = resolved.model
        upstream = await _metered.resolve_upstream(providers, resolved)

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
        _metered.enforce_input_limit(model, prompt_tokens)
        max_output = body.max_output_tokens or settings.quota.default_max_output_tokens
        # Server-side web search is charged per search on top of tokens, and
        # nothing in the request bounds it unless the caller said so (ADR 0058).
        # Read once, here: the same object supplies the reservation below and the
        # cap written into the outgoing tools, so the two cannot disagree.
        search = _metered.bound_web_search(
            (body.model_extra or {}).get("tools"),
            default=settings.quota.default_max_web_searches,
        )
        worst_case = TokenCounts(
            prompt=prompt_tokens, completion=max_output, searches=search.reserved
        )

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
            worst_case=worst_case,
            outcome=outcome,
            streamed=body.stream,
        )
        if isinstance(metered, JSONResponse):
            return metered

        payload = body.upstream_payload(outcome, upstream_model=resolved.upstream_model)
        search.apply(payload)
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
            out["model"] = resolved.requested_name
        await _restore_body(out, redactor, outcome)

        return JSONResponse(
            status_code=response.status_code,
            content=out,
            background=metered.completed_after_response(upstream_status=response.status_code),
        )

    return await _metered.serve_with_failover(session, plan, serve)


def _shift_by(restored: Restored, _choice: int, offset: int) -> int:
    return restored.shift(offset)


async def _restore_body(out: dict[str, Any], redactor: Any, outcome: Any) -> None:
    """Restore every text the response carries, each from its own provider text.

    The wire body has no ``output_text`` — that is a convenience an SDK adds —
    so the text lives in the content blocks of the output items, and each block
    is restored from what the provider wrote in *that* block. The assembled
    field is restored too when a provider includes it. Citations are offsets
    into one block's text, so each block's own edits move its own annotations.
    """
    if isinstance(assembled := out.get("output_text"), str):
        out["output_text"] = (await redactor.restore_response(assembled, outcome)).text
    for item in out.get("output") or []:
        if not isinstance(item, dict) or item.get("type") in ("reasoning", "function_call"):
            continue
        for block in item.get("content") or []:
            if not isinstance(block, dict) or not isinstance(block.get("text"), str):
                continue
            restored = await redactor.restore_response(block["text"], outcome)
            block["text"] = restored.text
            if restored.moved:
                shift_annotations(block.get("annotations"), partial(_shift_by, restored), 0)
    restore_reasoning_and_arguments(out, outcome, surface=SURFACE)


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
