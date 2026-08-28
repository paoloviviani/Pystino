"""Conversations, messages, and the turn that streams.

The one non-obvious piece is :func:`send`. A turn is:

    persist the user's message → persist an empty assistant message →
    relay the gateway's stream to the browser while accumulating it →
    finalise the assistant row

and the order is the point. The assistant row exists *before* the model has
said anything, so a stream that dies — the browser closes, the process
restarts — leaves a row marked ``interrupted`` holding what was really
generated, rather than nothing at all. The gateway already bills those tokens;
a transcript that forgot them would disagree with the ledger, and the ledger is
right.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from chat_api.deps import Caller, CallerDep, DbDep, GatewayDep
from chat_api.gateway_client import GatewayError
from chat_api.models import Conversation, Message, MessageStatus, Role
from chat_api.routers import MOUNT_PATH
from chat_api.schemas import (
    ConversationCreate,
    ConversationDetail,
    ConversationOut,
    ConversationUpdate,
    MessageOut,
    ModelOut,
    SendMessage,
)
from chat_api.types import utcnow

logger = logging.getLogger(__name__)

router = APIRouter(prefix=f"{MOUNT_PATH}/api", tags=["chat"])

#: How much of the first user message becomes the conversation title until
#: something better exists. Naming a conversation with a model call is a
#: metered request the user did not ask for, so the first version does not make
#: one — a truncated prompt is a worse title and an honest one.
_TITLE_CHARS = 60


async def _owned(
    db: AsyncSession, caller: Caller, conversation_id: uuid.UUID
) -> Conversation:
    """Load a conversation, or 404.

    Ownership is checked in the query, not after it: a 403 for someone else's
    conversation confirms the conversation exists, and "does this id exist" is
    not a question a stranger gets to ask.
    """
    stmt = (
        select(Conversation)
        .where(
            Conversation.id == conversation_id,
            Conversation.issuer == caller.issuer,
            Conversation.subject == caller.subject,
            Conversation.archived_at.is_(None),
        )
        .options(selectinload(Conversation.messages))
    )
    conversation: Conversation | None = (await db.execute(stmt)).scalar_one_or_none()
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such conversation.")
    return conversation


@router.get("/models", response_model=dict)
async def models(caller: CallerDep, gateway: GatewayDep) -> dict[str, Any]:
    """Exactly what the gateway says this person may use.

    Not filtered, sorted or cached here. Access control is the gateway's, and a
    second opinion about it is a second place for it to be wrong.
    """
    try:
        available = await gateway.models(caller.access_token)
    except GatewayError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return {"data": [ModelOut(id=m.id, owned_by=m.owned_by).model_dump() for m in available]}


@router.get("/conversations", response_model=dict)
async def list_conversations(caller: CallerDep, db: DbDep, limit: int = 50) -> dict[str, Any]:
    stmt = (
        select(Conversation)
        .where(
            Conversation.issuer == caller.issuer,
            Conversation.subject == caller.subject,
            Conversation.archived_at.is_(None),
        )
        .order_by(Conversation.updated_at.desc())
        .limit(min(limit, 200))
    )
    rows = (await db.execute(stmt)).scalars().all()
    return {"data": [ConversationOut.model_validate(row).model_dump(mode="json") for row in rows]}


@router.post("/conversations", response_model=ConversationDetail, status_code=201)
async def create_conversation(
    body: ConversationCreate, caller: CallerDep, db: DbDep
) -> ConversationDetail:
    conversation = Conversation(
        issuer=caller.issuer,
        subject=caller.subject,
        model=body.model,
        title=body.title or "New chat",
    )
    db.add(conversation)
    await db.commit()
    await db.refresh(conversation, attribute_names=["messages"])
    return ConversationDetail.model_validate(conversation)


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: uuid.UUID, caller: CallerDep, db: DbDep
) -> ConversationDetail:
    return ConversationDetail.model_validate(await _owned(db, caller, conversation_id))


@router.patch("/conversations/{conversation_id}", response_model=ConversationOut)
async def update_conversation(
    conversation_id: uuid.UUID, body: ConversationUpdate, caller: CallerDep, db: DbDep
) -> ConversationOut:
    conversation = await _owned(db, caller, conversation_id)
    if body.title is not None:
        conversation.title = body.title[:255]
    if body.model is not None:
        conversation.model = body.model
    await db.commit()
    return ConversationOut.model_validate(conversation)


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: uuid.UUID, caller: CallerDep, db: DbDep) -> None:
    """Archived, not deleted.

    The messages reference gateway request ids, which are how a transcript is
    reconciled against the ledger. Deleting the rows would leave spend nobody
    can explain — so this hides the conversation and keeps the link.
    """
    conversation = await _owned(db, caller, conversation_id)
    conversation.archived_at = utcnow()
    await db.commit()


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


#: The four spellings a reasoning delta arrives under.
#:
#: There is no standard. DeepSeek and the providers that copied it use
#: ``reasoning_content``; OpenRouter uses ``reasoning``; OpenAI's own reasoning
#: models expose summaries under ``reasoning_summary`` shapes. The gateway
#: passes the frame through untouched, deliberately — that is its job — so the
#: spellings arrive here as the counterparty wrote them.
#:
#: Tolerating all of them is the same judgement as ``_CACHE_WRITE_KEYS`` in the
#: gateway's accounting: these differ only in **spelling**, not in meaning, and
#: reading one of them means a model that thinks visibly looks like a model that
#: stalled. Nothing here decides anything about money, so there is no rule
#: against being generous.
_REASONING_KEYS = ("reasoning_content", "reasoning", "thinking", "reasoning_text")


@dataclass
class _Turn:
    """What one streamed answer accumulated, and what it means.

    A small object rather than five locals, because the finalisation now runs
    from two places — the normal end of the stream, and the generator being
    closed under the client's stop button — and five locals passed to both is
    how the two drift apart.
    """

    request_id: str
    content: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    served_model: str | None = None
    failure: str | None = None
    interrupted: bool = False

    def absorb(self, frame: dict[str, Any]) -> list[tuple[str, str]]:
        """Read one frame, and say what to send the browser."""
        self.served_model = frame.get("model") or self.served_model
        if frame.get("usage"):
            self.usage = frame["usage"]

        out: list[tuple[str, str]] = []
        for choice in frame.get("choices") or []:
            delta = choice.get("delta") or {}
            for key in _REASONING_KEYS:
                piece = delta.get(key)
                if isinstance(piece, str) and piece:
                    self.reasoning.append(piece)
                    out.append(("reasoning", piece))
                    break
            piece = delta.get("content")
            if isinstance(piece, str) and piece:
                self.content.append(piece)
                out.append(("delta", piece))
        return out

    def status(self) -> MessageStatus:
        if self.failure:
            return MessageStatus.FAILED
        if self.interrupted:
            # Stopped on purpose, or the tab closed. Either way the text so far
            # was really generated and really billed; discarding it would make
            # the transcript disagree with the ledger.
            return MessageStatus.INTERRUPTED
        if self.usage or self.content or self.reasoning:
            return MessageStatus.COMPLETE
        # Nothing at all, and no error: the far end went away before the first
        # frame. Calling that "complete" shows an empty answer as the model's.
        return MessageStatus.INTERRUPTED


async def _persist(factory: Any, message_id: uuid.UUID, turn: _Turn) -> None:
    """Write the assistant message down. Runs exactly once per turn.

    Shielded, for the reason the gateway's `settle_completed` is shielded and
    found the same way: this is called from a generator that the client can
    close at any moment, and an await inside a cancelled task is torn down
    part-way through. Without it, pressing stop loses the very text the button
    was pressed to keep.
    """

    async def write() -> None:
        async with factory() as db2:
            row = await db2.get(Message, message_id)
            if row is not None:
                row.content = "".join(turn.content)
                row.reasoning = "".join(turn.reasoning) or None
                row.request_id = turn.request_id
                row.model = turn.served_model or row.model
                row.usage = turn.usage
                row.status = turn.status()
                row.error = turn.failure
            await db2.commit()

    await asyncio.shield(asyncio.ensure_future(write()))


@router.post("/conversations/{conversation_id}/messages")
async def send(
    conversation_id: uuid.UUID,
    body: SendMessage,
    request: Request,
    caller: CallerDep,
    db: DbDep,
    gateway: GatewayDep,
) -> StreamingResponse:
    conversation = await _owned(db, caller, conversation_id)
    if body.model:
        conversation.model = body.model

    next_position = (
        await db.execute(
            select(func.coalesce(func.max(Message.position), -1) + 1).where(
                Message.conversation_id == conversation.id
            )
        )
    ).scalar_one()

    user_message = Message(
        conversation_id=conversation.id,
        position=next_position,
        role=Role.USER,
        content=body.content,
        status=MessageStatus.COMPLETE,
    )
    assistant = Message(
        conversation_id=conversation.id,
        position=next_position + 1,
        role=Role.ASSISTANT,
        content="",
        status=MessageStatus.STREAMING,
        model=conversation.model,
    )
    db.add_all([user_message, assistant])
    if conversation.title == "New chat":
        conversation.title = body.content.strip()[:_TITLE_CHARS] or "New chat"
    conversation.updated_at = utcnow()
    await db.commit()

    history = [
        {"role": message.role.value, "content": message.content}
        for message in sorted(conversation.messages, key=lambda m: m.position)
        if message.status is MessageStatus.COMPLETE and message.content
    ]
    history.append({"role": Role.USER.value, "content": body.content})

    assistant_id = assistant.id
    payload = {"model": conversation.model, "messages": history}
    # Minted here and sent to the gateway, not read back from it. See
    # GatewayClient.stream_chat.
    request_id = uuid.uuid4().hex

    async def stream() -> AsyncIterator[str]:
        # A session of its own. The request-scoped one is closed when this
        # function returns the response, and everything below runs after that.
        factory = request.app.state.session_factory
        turn = _Turn(request_id=request_id)

        yield _sse("message", {"id": str(assistant_id), "position": assistant.position})
        try:
            async for line in gateway.stream_chat(
                access_token=caller.access_token, body=payload, request_id=request_id
            ):
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    # Read on rather than break. Breaking closes the connection
                    # the instant the terminal frame arrives, and the gateway is
                    # at that moment inside the database write that settles the
                    # request — uvicorn cancels its task, the write dies with
                    # CancelledError, and the row stays `in_progress` with a
                    # cost of zero. Measured against the live stack on
                    # 2026-08-28: every chat turn left an unsettled row until
                    # this loop was made to drain.
                    continue
                try:
                    frame = json.loads(data)
                except ValueError:
                    continue
                for event, piece in turn.absorb(frame):
                    yield _sse(event, {"content": piece})
        except (GatewayError, asyncio.CancelledError, GeneratorExit) as exc:
            # A cancelled generator is the *stop* button, and it is not a
            # failure: the tokens so far were generated and billed, so they are
            # kept and the row says `interrupted`. Re-raised below, because
            # swallowing GeneratorExit is an error in its own right.
            if isinstance(exc, GatewayError):
                # The gateway's own words: "you have exceeded your monthly
                # budget" is the message the person needs, and rewriting it to
                # "an error occurred" is how a working system looks broken.
                turn.failure = str(exc)
                yield _sse(
                    "error",
                    {"message": turn.failure, "code": exc.code, "status": exc.status},
                )
            else:
                turn.interrupted = True
                await _persist(factory, assistant_id, turn)
                raise
        except Exception:
            logger.exception("chat stream failed")
            turn.failure = "The chat service could not complete this message."
            yield _sse("error", {"message": turn.failure})

        await _persist(factory, assistant_id, turn)
        yield _sse(
            "done",
            {
                "id": str(assistant_id),
                "request_id": request_id,
                "usage": turn.usage,
                "model": turn.served_model,
            },
        )

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "cache-control": "no-store",
            # Belt and braces with Caddy's `flush_interval -1`: any proxy that
            # honours this one will not buffer either.
            "x-accel-buffering": "no",
        },
    )


@router.get("/conversations/{conversation_id}/messages", response_model=dict)
async def list_messages(
    conversation_id: uuid.UUID, caller: CallerDep, db: DbDep
) -> dict[str, Any]:
    conversation = await _owned(db, caller, conversation_id)
    return {
        "data": [
            MessageOut.model_validate(message).model_dump(mode="json")
            for message in sorted(conversation.messages, key=lambda m: m.position)
        ]
    }
