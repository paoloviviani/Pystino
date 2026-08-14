"""Per-request accounting: accumulate while streaming, persist as we go.

The row is created **before** the upstream call and finalised after it, because
the alternative — build everything in memory and insert once at the end — loses
the whole request whenever the client vanishes mid-generation. Mobile clients get
suspended by the OS routinely, so "the client went away" is a normal event, not an
error path.

Two writes therefore matter:

* periodic flushes of the partial assistant text, so a suspended client can
  recover the answer that was already generated and paid for;
* one finalising write with token counts, cost and terminal status.

Each is its own short transaction. A single transaction spanning the life of a
stream would pin a connection for the whole generation and hold back autovacuum
on the busiest table in the schema.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

import orjson
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gateway.accounting.cost import CostBreakdown, TokenCounts, compute_cost, select_price
from gateway.accounting.tokens import TokenEstimator
from gateway.config import Settings
from gateway.models import ModelDef, UsageRecord, UsageSource, UsageStatus
from gateway.quota import QuotaAmounts
from gateway.sse.events import SSEEvent
from gateway.types import utcnow

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ChoiceAccumulator:
    """Reassembles one choice's output from its deltas."""

    content: list[str] = field(default_factory=list)
    # Tool call arguments arrive as fragments keyed by index, exactly like text.
    tool_calls: dict[int, dict[str, Any]] = field(default_factory=dict)
    finish_reason: str | None = None

    def text(self) -> str:
        return "".join(self.content)

    def observe_delta(self, delta: dict[str, Any]) -> None:
        if (piece := delta.get("content")) and isinstance(piece, str):
            self.content.append(piece)
        # Some providers use `refusal` for safety declines; it is still output.
        if (refusal := delta.get("refusal")) and isinstance(refusal, str):
            self.content.append(refusal)

        for call in delta.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            index = call.get("index")
            if not isinstance(index, int):
                continue
            slot = self.tool_calls.setdefault(index, {"id": None, "name": None, "arguments": ""})
            if call_id := call.get("id"):
                slot["id"] = call_id
            function = call.get("function") or {}
            if isinstance(function, dict):
                if name := function.get("name"):
                    slot["name"] = name
                if (arguments := function.get("arguments")) and isinstance(arguments, str):
                    slot["arguments"] = str(slot["arguments"]) + arguments

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"content": self.text()}
        if self.tool_calls:
            payload["tool_calls"] = [self.tool_calls[k] for k in sorted(self.tool_calls)]
        if self.finish_reason:
            payload["finish_reason"] = self.finish_reason
        return payload


@dataclass(slots=True)
class RequestContext:
    """Immutable facts about the request being accounted for."""

    request_id: str
    model_name: str
    currency: str
    streamed: bool
    user_id: uuid.UUID | None = None
    group_id: uuid.UUID | None = None
    api_key_id: uuid.UUID | None = None
    model_id: uuid.UUID | None = None
    # Locally counted prompt size, used for the pre-call reservation and as the
    # fallback when the upstream never reports usage.
    estimated_prompt_tokens: int = 0
    redaction_engine: str | None = None
    redacted_entity_count: int = 0


class RequestAccounting:
    """Owns one usage row from creation to finalisation."""

    def __init__(
        self,
        *,
        context: RequestContext,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        estimator: TokenEstimator,
        model: ModelDef | None = None,
    ) -> None:
        self._ctx = context
        self._session_factory = session_factory
        self._settings = settings
        self._estimator = estimator
        self._model = model

        self.record_id: uuid.UUID | None = None
        self._choices: dict[int, ChoiceAccumulator] = {}
        self._upstream_usage: dict[str, Any] | None = None
        self._started_at = utcnow()
        self._first_token_at: datetime | None = None

        self._flushed_chars = 0
        self._last_flush_at = self._started_at
        self._finalised = False
        self._last_actuals = QuotaAmounts()

    # -- lifecycle ---------------------------------------------------------

    async def begin(self) -> uuid.UUID:
        """Insert the in-progress row. Call before touching the upstream."""
        record = UsageRecord(
            request_id=self._ctx.request_id,
            status=UsageStatus.IN_PROGRESS,
            user_id=self._ctx.user_id,
            group_id=self._ctx.group_id,
            api_key_id=self._ctx.api_key_id,
            model_id=self._ctx.model_id,
            model_name=self._ctx.model_name,
            streamed=self._ctx.streamed,
            currency=self._ctx.currency,
            usage_source=UsageSource.UNAVAILABLE,
            cost=Decimal(0),
            redaction_engine=self._ctx.redaction_engine,
            redacted_entity_count=self._ctx.redacted_entity_count,
        )
        async with self._session_factory() as session:
            session.add(record)
            await session.commit()
        self.record_id = record.id
        return record.id

    # -- observation (synchronous, runs inline between tokens) -------------

    def observe_event(self, event: SSEEvent) -> None:
        """Accumulate one streamed event. Must stay cheap and non-blocking."""
        if event.is_done() or event.is_comment_only():
            return
        payload = event.json()
        if not isinstance(payload, dict):
            return
        self.observe_payload(payload)

    def observe_payload(self, payload: dict[str, Any]) -> None:
        """Accumulate one chat-completion chunk or a full non-streamed body."""
        if self._first_token_at is None:
            self._first_token_at = utcnow()

        if isinstance(usage := payload.get("usage"), dict):
            # Later frames win: a provider that sends running totals should be
            # believed at its final word.
            self._upstream_usage = usage

        for choice in payload.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            index = choice.get("index")
            slot = self._choices.setdefault(
                index if isinstance(index, int) else 0, ChoiceAccumulator()
            )
            if isinstance(delta := choice.get("delta"), dict):
                slot.observe_delta(delta)
            # Non-streamed responses carry a whole message instead of a delta.
            if isinstance(message := choice.get("message"), dict):
                slot.observe_delta(message)
            if reason := choice.get("finish_reason"):
                slot.finish_reason = str(reason)

    # -- persistence -------------------------------------------------------

    def assistant_text(self) -> str | None:
        """The accumulated output, as text when there is one plain choice.

        Anything richer — several choices, or tool calls — is stored as JSON so
        that nothing is lost. A transcript that silently keeps only the first
        choice would be worse than one that is occasionally a JSON blob.
        """
        if not self._choices:
            return None
        if len(self._choices) == 1:
            only = next(iter(self._choices.values()))
            if not only.tool_calls:
                return only.text() or None
        payload = {str(index): slot.as_payload() for index, slot in sorted(self._choices.items())}
        return orjson.dumps(payload).decode()

    def _pending_chars(self) -> int:
        return sum(len(part) for slot in self._choices.values() for part in slot.content)

    async def maybe_flush(self) -> None:
        """Persist partial output if enough has accrued since the last flush."""
        if not self._settings.persist_transcripts or self.record_id is None:
            return
        pending = self._pending_chars()
        if pending == self._flushed_chars:
            return
        elapsed = (utcnow() - self._last_flush_at).total_seconds()
        if (
            pending - self._flushed_chars < self._settings.transcript_flush_chars
            and elapsed < self._settings.transcript_flush_seconds
        ):
            return
        await self.flush()

    async def flush(self) -> None:
        if self.record_id is None:
            return
        text = self.assistant_text()
        try:
            async with self._session_factory() as session:
                await session.execute(
                    update(UsageRecord)
                    .where(UsageRecord.id == self.record_id)
                    .values(assistant_text=text, updated_at=utcnow())
                )
                await session.commit()
        except Exception:
            # A failed transcript flush must never break the stream the user is
            # reading. The finalising write will try again.
            logger.warning("transcript flush failed", exc_info=True)
            return
        self._flushed_chars = self._pending_chars()
        self._last_flush_at = utcnow()

    # -- finalisation ------------------------------------------------------

    def resolve_counts(self) -> tuple[TokenCounts, UsageSource]:
        """Best available token counts, and an honest label for their provenance."""
        if self._upstream_usage:
            counts = TokenCounts.from_usage(self._upstream_usage)
            if counts.total:
                return counts, UsageSource.UPSTREAM_EXACT

        # No usable usage frame. Estimate rather than record zero.
        completion_text = "".join(part for slot in self._choices.values() for part in slot.content)
        completion = self._estimator.count_text(completion_text)
        for slot in self._choices.values():
            for call in slot.tool_calls.values():
                completion += self._estimator.count_text(str(call.get("arguments") or ""))
                completion += self._estimator.count_text(str(call.get("name") or ""))

        if not completion and not self._ctx.estimated_prompt_tokens:
            return TokenCounts(), UsageSource.UNAVAILABLE

        return (
            TokenCounts(prompt=self._ctx.estimated_prompt_tokens, completion=completion),
            UsageSource.ESTIMATED,
        )

    def finish_reason(self) -> str | None:
        for slot in self._choices.values():
            if slot.finish_reason:
                return slot.finish_reason
        return None

    async def finalise(
        self,
        *,
        status: UsageStatus,
        upstream_status: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> QuotaAmounts:
        """Write the final row and return the actuals for quota settlement.

        Idempotent: called from both the success path and the disconnect/error
        handlers, and those can overlap.
        """
        if self._finalised:
            return self._last_actuals
        self._finalised = True

        counts, source = self.resolve_counts()

        price = None
        breakdown = CostBreakdown.zero(self._ctx.currency)
        if self._model is not None:
            price = select_price(list(self._model.prices), at=self._started_at)
            try:
                breakdown = compute_cost(
                    counts,
                    price,
                    billing_currency=self._ctx.currency,
                    model_name=self._ctx.model_name,
                )
            except Exception:
                # A misconfigured price must not lose the usage record. Cost stays
                # zero, price_id stays null, and the anomaly is visible in
                # reporting rather than swallowed.
                logger.error(
                    "cost computation failed for model %s", self._ctx.model_name, exc_info=True
                )
                price = None

        now = utcnow()
        latency_ms = int((now - self._started_at).total_seconds() * 1000)
        ttfb_ms = (
            int((self._first_token_at - self._started_at).total_seconds() * 1000)
            if self._first_token_at is not None
            else None
        )

        actuals = QuotaAmounts(
            requests=Decimal(1),
            tokens=Decimal(counts.total),
            cost=breakdown.total,
        )
        self._last_actuals = actuals

        if self.record_id is None:
            logger.error("finalise called before begin; usage not persisted")
            return actuals

        values: dict[str, Any] = {
            "status": status,
            "prompt_tokens": counts.prompt,
            "completion_tokens": counts.completion,
            "total_tokens": counts.total,
            "cached_prompt_tokens": counts.cached_prompt,
            "reasoning_tokens": counts.reasoning,
            "usage_source": source,
            "cost": breakdown.total,
            "currency": breakdown.currency,
            "price_id": price.id if price is not None else None,
            "finish_reason": self.finish_reason(),
            "upstream_status": upstream_status,
            "latency_ms": latency_ms,
            "ttfb_ms": ttfb_ms,
            "assistant_text": self.assistant_text(),
            "error_code": error_code,
            "error_message": error_message,
            "updated_at": now,
            "finalised_at": now,
        }

        try:
            async with self._session_factory() as session:
                await session.execute(
                    update(UsageRecord).where(UsageRecord.id == self.record_id).values(**values)
                )
                await session.commit()
        except Exception:
            # Losing the ledger write is the worst outcome here, so it is logged
            # loudly. Quota settlement still proceeds with the correct actuals.
            logger.exception("failed to finalise usage record %s", self.record_id)

        return actuals
