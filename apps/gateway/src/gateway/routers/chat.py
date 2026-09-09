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
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from typing import Any

import orjson
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from llmp_shared import Restored
from sqlalchemy.ext.asyncio import AsyncSession

from gateway import agents
from gateway.accounting import (
    RequestAccounting,
    TokenCounts,
)
from gateway.deps import (
    EstimatorDep,
    Principal,
    PrincipalDep,
    ProvidersDep,
    QuotaDep,
    RedactorDep,
    SessionDep,
    SettingsDep,
)
from gateway.errors import (
    BadRequestError,
    error_payload,
)
from gateway.knowledge.store import store_for
from gateway.models import ApiSurface, ModelDef, UsageStatus
from gateway.protocols import reader_for
from gateway.quota import (
    QuotaEngine,
    Reservation,
)
from gateway.redaction import RedactionOutcome, Redactor
from gateway.routers import _metered
from gateway.schemas import ChatCompletionRequest
from gateway.sse import SSEEvent, chain, iter_sse_events
from gateway.sse.pipeline import StreamStage, tap
from gateway.upstream import OpenAICompatibleUpstream, UpstreamError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["openai"])

SURFACE = ApiSurface.CHAT_COMPLETIONS

#: Re-exported: the embeddings route and the tests import it from here, and it
#: was defined here before the metering was shared out.

STREAM_HEADERS = {
    "cache-control": "no-cache",
    "connection": "keep-alive",
    # Stops nginx buffering the stream into uselessness.
    "x-accel-buffering": "no",
}


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
    """The request we actually send, before the counterparty's plugin sees it.

    ``exclude_unset`` forwards exactly what the client sent — including every
    parameter this gateway knows nothing about — and nothing it did not. Adding
    defaults of our own would silently change model behaviour.

    Which is why ``stream_options.include_usage`` is no longer added here. Asking
    for usage on a stream is not a property of this route: a generic
    OpenAI-compatible endpoint sends none unless asked, while the reference router
    sends it either way and warns against undocumented parameters. That was
    ``providers.forward_stream_options`` and is now
    ``GenericOpenAIPlugin.prepare_payload`` — see ADR 0032. The route calls
    ``metered.shape_payload`` next.
    """
    payload = body.model_dump(exclude_unset=True)
    payload["model"] = upstream_model
    payload["messages"] = outcome.messages
    return payload



async def _retrieve_for_agent(
    request: Request,
    session: AsyncSession,
    resolved: agents.ResolvedAgent,
    *,
    principal: Principal,
    body: ChatCompletionRequest,
) -> str | None:
    """The passages this agent's bases offer for the latest question.

    None when there is nothing to add — no bases attached, no user text to
    retrieve on, or nothing above the agent's score floor. None rather than an
    empty string, so `agents.apply` adds no message at all: an empty context
    block tells a model there was material and it was blank, which is worse
    than saying nothing.

    **Failure here does not fail the request.** A retrieval that cannot run —
    the embedding provider is down, a base has never been indexed — degrades to
    an ordinary completion, logged. The alternative is refusing to answer a
    chat because a knowledge base is unavailable, which trades a slightly worse
    answer for no answer at all. That is a judgement, and it is the same one
    ADR 0060 makes about the deferred ledger write: the caller's request is
    worth more than the completeness of the extra.

    It is *not* silent, though. `retrieval_degraded` on the log line is what an
    operator greps for when an agent starts giving answers that ignore its
    documents.
    """
    if not resolved.bases:
        return None
    question = agents.last_user_text(body.messages)
    if not question.strip():
        return None

    ingestor = getattr(request.app.state, "ingestor", None)
    if ingestor is None:  # pragma: no cover - wired at startup
        return None

    store = store_for(session.bind.dialect.name if session.bind else "sqlite")
    passages: list[tuple[str, str]] = []
    for base in resolved.bases:
        if base.dimensions is None or base.embedding_model_id is None:
            # Never indexed. Not an error: an agent may be attached to a base
            # somebody is still filling.
            continue
        try:
            owner, group = await ingestor.billing_for(session, base)
            vector = await ingestor.embed_query(
                session, base=base, owner=owner, group=group, text=question
            )
            hits = await store.search(
                session,
                knowledge_base_id=base.id,
                dimensions=base.dimensions,
                query=vector,
                limit=resolved.agent.retrieval_limit,
                min_score=resolved.agent.retrieval_min_score,
            )
        except Exception:
            logger.warning(
                "retrieval_degraded: agent=%s base=%s — answering without it",
                resolved.agent.name,
                base.id,
                exc_info=True,
            )
            continue
        passages.extend((hit.title or "untitled", hit.text) for hit in hits)

    if not passages:
        return None
    # Bounded across every base rather than per base: an agent with four bases
    # and a limit of six would otherwise put twenty-four passages in front of
    # the model, and the cost of a prompt is the caller's.
    passages = passages[: resolved.agent.retrieval_limit]
    return agents.format_context(passages)

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

    request_id = _metered.request_id_for(request, settings)

    # -- the agent, if this name is one (step 1b) ---------------------------
    #
    # Before redaction on purpose, and the ordering is load-bearing in both
    # directions: the system prompt and any retrieved passages are text heading
    # for a provider, so they must pass through the redaction layer like
    # anything else; and they have to be in `messages` before the tokens are
    # counted, or the reservation is taken against a smaller payload than the
    # one actually sent (ADR 0062).
    resolved = await agents.resolve(session, body.model, principal=principal)
    if resolved is not None:
        model = resolved.model
        # The name the caller sent is what the ledger records, so a report can
        # say "this spend was an agent" rather than showing only its model.
        agent_context = await _retrieve_for_agent(
            request, session, resolved, principal=principal, body=body
        )
        body.messages = agents.apply(body.messages, resolved, context=agent_context)
        # Tools and sampling defaults go into the request's *extra* fields,
        # which is where they already live: `ChatCompletionRequest` is
        # `extra="allow"` so a provider's own parameters pass through
        # untouched, and writing them here means everything downstream —
        # `bound_web_search`, the payload dump, the reservation — reads one
        # object rather than each remembering to consult the agent.
        extra = body.__pydantic_extra__
        if extra is not None:
            merged = agents.merge_tools(extra.get("tools"), resolved.agent.tools)
            if merged is not None:
                extra["tools"] = merged
            agents.apply_generation(extra, resolved.agent.generation)
    else:
        model = await _metered.resolve_model(
            session, body.model, principal=principal, surface=SURFACE
        )
    upstream = await _metered.resolve_upstream(providers, model)

    # -- redaction (step 2) -------------------------------------------------
    outcome = await _metered.redact_or_block(
        request,
        body.messages,
        redactor=redactor,
        session=session,
        principal=principal,
        model=model,
        settings=settings,
        surface=SURFACE,
        request_id=request_id,
    )

    # -- local counting and reservation estimate (steps 3-4) ----------------
    prompt_tokens = estimator.count_messages(outcome.messages)
    max_output = (
        body.requested_max_output_tokens(settings.quota.default_max_output_tokens)
        * body.choice_count()
    )
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

    # -- reserve, then open the usage row (steps 4-5) -----------------------
    metered = await _metered.begin(
        fx=request.app.state.fx,
        session_factory=request.app.state.session_factory,
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

    payload = build_upstream_payload(body, outcome=outcome, upstream_model=model.upstream_model)
    search.apply(payload)
    payload = metered.shape_payload(payload, surface=SURFACE)

    if body.stream:
        return await _stream_response(
            request=request,
            payload=payload,
            model=model,
            body=body,
            outcome=outcome,
            redactor=redactor,
            upstream=upstream,
            metered=metered,
            request_id=request_id,
        )

    return await _buffered_response(
        payload=payload,
        model=model,
        outcome=outcome,
        redactor=redactor,
        upstream=upstream,
        metered=metered,
        request_id=request_id,
    )


async def _buffered_response(
    *,
    payload: dict[str, Any],
    model: ModelDef,
    outcome: RedactionOutcome,
    redactor: Redactor,
    upstream: OpenAICompatibleUpstream,
    metered: _metered.Metered,
    request_id: str,
) -> JSONResponse:
    """Non-streaming completion."""
    try:
        response = await upstream.chat_completion(payload, request_id=request_id)
    except UpstreamError as exc:
        raise await metered.upstream_unreachable(exc) from exc

    if not response.ok:
        return await metered.upstream_refused(
            status_code=response.status_code,
            message=(response.raw[:500].decode("utf-8", "replace") if response.raw else None),
            content=response.payload
            or error_payload("The upstream provider returned an error.", type_="api_error"),
        )

    body_payload = response.payload or {}
    metered.accounting.observe_payload(body_payload, headers=response.headers)

    # Report our model name, and restore any placeholders we introduced.
    body_payload["model"] = model.name
    # One restoration per choice, kept by choice index: each choice is different
    # text, so the *positions* the substitutions edited differ even though the
    # placeholder map is one per request.
    restored_by_choice: dict[int, Restored] = {}
    for position, choice in enumerate(body_payload.get("choices") or []):
        if not isinstance(choice, dict):
            continue
        declared = choice.get("index")
        which = declared if isinstance(declared, int) else position
        message = choice.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            restored = await redactor.restore_response(message["content"], outcome)
            message["content"] = restored.text
            restored_by_choice[which] = restored

    # A citation is character offsets into the text just rewritten, so it moves
    # with it or it points at the wrong words (ADR 0059).
    if any(item.moved for item in restored_by_choice.values()):
        reader_for(SURFACE).shift_citations(
            body_payload,
            lambda choice_index, offset: (
                restored_by_choice[choice_index].shift(offset)
                if choice_index in restored_by_choice
                else offset
            ),
        )

    # Finalised after the answer is sent, not before it (ADR 0060).
    return JSONResponse(
        status_code=200,
        content=body_payload,
        background=metered.completed_after_response(upstream_status=response.status_code),
    )


async def _stream_response(
    *,
    request: Request,
    payload: dict[str, Any],
    model: ModelDef,
    body: ChatCompletionRequest,
    outcome: RedactionOutcome,
    redactor: Redactor,
    upstream: OpenAICompatibleUpstream,
    metered: _metered.Metered,
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
            events = iter_sse_events(upstream_response.aiter_bytes())
            pipeline = chain(
                events,
                # Observe first: accounting must see usage frames that the client
                # will not be shown.
                tap(metered.accounting.observe_event),
                rename_model_stage(model.name),
                redactor.response_stage(outcome),
                usage_visibility_stage(body.client_wants_usage()),
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
                # The client went away (or something failed mid-stream). We are
                # very likely inside a cancelled task, where awaiting anything
                # re-raises immediately — so finalisation is handed to a detached
                # task instead of awaited. Without this, the tokens already
                # generated and paid for would never be recorded.
                spawn_finalisation(
                    request,
                    accounting=metered.accounting,
                    quota=metered.quota,
                    reservation=metered.reservation,
                    upstream_status=upstream_response.status_code,
                )

    return StreamingResponse(
        body_iterator(),
        media_type="text/event-stream",
        headers=STREAM_HEADERS,
    )


async def settle_completed(
    request: Request, *, metered: _metered.Metered, upstream_status: int | None
) -> None:
    """Settle a stream that ran to the end, even if the client leaves mid-write.

    This looks like it could be a bare ``await metered.completed(...)`` and was
    one, and that was a bug worth spelling out because it is invisible in every
    test and in most manual use.

    A client that closes the connection the instant it reads the terminal
    ``data: [DONE]`` frame does so while this coroutine is inside the database
    write that settles the request. uvicorn cancels the request task; the
    ``await`` raises ``CancelledError`` part-way through; the connection is torn
    down mid-statement, and the row stays ``in_progress`` with zero tokens and
    zero cost for a request the provider generated and billed us for in full.
    Measured against the live stack on 2026-08-28, from the chat application,
    which drains its stream and then hangs up promptly — exactly the well-behaved
    client that triggers it most reliably.

    The disconnect branch beside this one has been cancellation-proof since it
    was written, and its comment explains why in almost these words. The
    reasoning was simply never applied to its neighbour: a stream that *finished*
    looked like the safe case.

    The write runs as a detached task, and this awaits it through
    ``shield``. So the ordering is unchanged when nothing goes wrong — the
    response does not complete until the row is settled, which is what the
    ledger tests rely on — and when the outer task is cancelled the write is
    already running somewhere that cancellation does not reach.

    ``RequestAccounting`` opens its own session for finalisation, so the
    detached write does not depend on a request-scoped session that is being
    torn down. That is what makes detaching sufficient rather than merely
    hopeful.
    """
    task = asyncio.create_task(metered.completed(upstream_status=upstream_status))
    # A strong reference, for the same reason spawn_finalisation keeps one: a
    # task nothing refers to can be collected mid-flight, losing precisely the
    # write this exists to protect.
    tasks: set[asyncio.Task[Any]] = request.app.state.background_tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    await asyncio.shield(task)


def spawn_finalisation(
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
