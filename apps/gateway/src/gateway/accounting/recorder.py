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
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import orjson
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gateway.accounting.cost import (
    CostBreakdown,
    TokenCounts,
    compute_cost,
    select_price,
)
from gateway.accounting.tokens import TokenEstimator
from gateway.config import Settings
from gateway.models import (
    ApiSurface,
    BillingMode,
    CostSource,
    ModelDef,
    UsageRecord,
    UsageSource,
    UsageStatus,
)
from gateway.protocols import reader_for
from gateway.quota import QuotaAmounts
from gateway.sse.events import SSEEvent
from gateway.types import utcnow

if TYPE_CHECKING:
    from gateway.plugins.base import ProviderPlugin, ServedBy

logger = logging.getLogger(__name__)


def _plugins() -> Any:
    """The plugin registry, imported on first use.

    Deferred because the dependency runs both ways: a plugin returns
    `TokenCounts`, which lives in this package, so importing the registry at
    module scope closes a cycle through `gateway.accounting.__init__`. One
    function call on construction is cheaper than restructuring two packages to
    avoid it.
    """
    from gateway.plugins import registry

    return registry


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
    #: Which endpoint served the request. Selects how response frames are read
    #: — the surfaces disagree about where usage lives and what its keys mean.
    surface: ApiSurface = ApiSurface.CHAT_COMPLETIONS
    #: Which plugin carries this counterparty's quirks (ADR 0032), from
    #: `providers.plugin`. Passed as a plain string rather than read off
    #: `model.provider` here: finalisation runs in its own session, where
    #: touching a relationship on a model loaded elsewhere is a lazy load on a
    #: detached instance. Null resolves to the generic OpenAI-compatible
    #: behaviour, which reports no cost of its own.
    plugin: str | None = None
    #: Whose figure is the charge, from `providers.billing_mode` (ADR 0032).
    billing_mode: str = "own_prices"


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
        self._reader = reader_for(context.surface)
        self._session_factory = session_factory
        self._settings = settings
        self._estimator = estimator
        self._model = model
        # Resolved once, not per frame. An unknown name is a configuration error
        # that should have been caught when the provider was saved; here it must
        # not lose a usage row, so it degrades to generic and says so loudly.
        try:
            self._plugin = _plugins().resolve(context.plugin)
        except _plugins().UnknownPluginError:
            logger.error(
                "provider plugin %r is not installed; recording this request with the "
                "generic reader, which reports no cost and no serving endpoint",
                context.plugin,
            )
            self._plugin = _plugins().resolve(None)

        self.record_id: uuid.UUID | None = None
        self._choices: dict[int, ChoiceAccumulator] = {}
        self._upstream_usage: dict[str, Any] | None = None
        # What the provider says actually served this request. A router with
        # model fallback can substitute a different model, and the request no
        # longer describes the answer (ADR 0028).
        self._upstream_model: str | None = None
        self._upstream_provider: str | None = None
        self._started_at = utcnow()
        self._first_token_at: datetime | None = None
        #: Set by the image route, which counts pictures rather than tokens.
        self._images = 0
        self._image_size: str | None = None

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
            api_surface=self._ctx.surface,
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

    @property
    def plugin(self) -> ProviderPlugin:
        """The counterparty's plugin, for a caller holding facts we cannot see.

        A streaming route has the response headers and the recorder does not:
        frames carry no headers, so the route asks the plugin and hands back the
        answer rather than the recorder growing a second way in.
        """
        plugin: ProviderPlugin = self._plugin
        return plugin

    def observe_served_by(self, served: ServedBy | None) -> None:
        """Record the endpoint that ran the request, first answer wins.

        First wins because a stream repeats it, and the first is the one nothing
        downstream can have rewritten.
        """
        if served is None:
            return
        if self._upstream_provider is None and served.endpoint:
            self._upstream_provider = served.endpoint[:128]
        # Only as a fallback: the body's `model` is the better source where it
        # exists, and is already read above.
        if self._upstream_model is None and served.model:
            self._upstream_model = served.model[:255]

    def observe_payload(
        self, payload: dict[str, Any], *, headers: Mapping[str, str] | None = None
    ) -> None:
        """Accumulate one response frame, in whatever shape this surface uses.

        ``headers`` is optional because streamed frames arrive without them; the
        route passes the response's headers on the first call it makes.
        """
        if self._first_token_at is None:
            self._first_token_at = utcnow()

        if (frame := self._reader.frame(payload)) is not None:
            if isinstance(usage := frame.get("usage"), dict):
                if self._reader.accumulates_usage:
                    # Anthropic sends the input count on one frame and the
                    # output count on another. Merged key by key, later
                    # non-null wins — replacing would discard whichever half
                    # arrived first.
                    merged = dict(self._upstream_usage or {})
                    merged.update({k: v for k, v in usage.items() if v is not None})
                    self._upstream_usage = merged
                else:
                    # One terminal usage frame carrying complete totals; a
                    # provider that sends running totals is believed at its
                    # final word.
                    self._upstream_usage = usage

            # Captured before the rename stage rewrites `model` to our
            # client-facing name on the way out. First frame wins: a stream
            # repeats it, and the first is the one nothing downstream can have
            # touched.
            if self._upstream_model is None and isinstance(served := frame.get("model"), str):
                self._upstream_model = served

        # Which endpoint actually ran this. Asked of the plugin rather than
        # guessed at, because counterparties disagree about where they put it:
        # one names it in the body, and the reference router names it *only* in a
        # response header — which is why this column was null for every request
        # through it until ADR 0032.
        self.observe_served_by(self._plugin.read_served_by(payload, headers or {}))

        for index, delta in self._reader.deltas(payload):
            slot = self._choices.setdefault(index, ChoiceAccumulator())
            if reason := delta.get("__finish_reason__"):
                slot.finish_reason = str(reason)
                continue
            slot.observe_delta(delta)

    def observe_images(self, count: int, size: str | None) -> None:
        """How many pictures came back, and at what size.

        Separate from usage because the models that charge per image are
        exactly the ones that report no usage at all.
        """
        self._images = max(0, count)
        self._image_size = size

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

    def resolve_counts(self, *, failed: bool = False) -> tuple[TokenCounts, UsageSource]:
        """Best available token counts, and an honest label for their provenance."""
        if self._upstream_usage:
            counts = self._reader.counts(self._upstream_usage)
            if counts.total:
                return self._with_images(counts), UsageSource.UPSTREAM_EXACT

        # No usable usage frame. Estimate rather than record zero.
        completion_text = "".join(part for slot in self._choices.values() for part in slot.content)
        completion = self._estimator.count_text(completion_text)
        for slot in self._choices.values():
            for call in slot.tool_calls.values():
                completion += self._estimator.count_text(str(call.get("arguments") or ""))
                completion += self._estimator.count_text(str(call.get("name") or ""))

        if self._images and not completion:
            # An image request that reported no usage. The picture count came
            # from the response and is exact, and it is what the model is
            # billed on — so the row is `upstream_exact`, with zero tokens
            # rather than an estimate of a quantity nobody charges for.
            return self._with_images(TokenCounts()), UsageSource.UPSTREAM_EXACT

        if failed and not completion:
            # The upstream refused before generating anything, and reported no
            # usage. Nothing was consumed, so nothing is charged — estimating the
            # prompt here would bill a caller for the provider's failure, and
            # Cortecs is explicit that you pay only for a successful request.
            # A failure *after* partial output still estimates: those tokens were
            # really generated.
            return TokenCounts(), UsageSource.UNAVAILABLE

        if not completion and not self._ctx.estimated_prompt_tokens:
            return TokenCounts(), UsageSource.UNAVAILABLE

        return (
            self._with_images(
                TokenCounts(prompt=self._ctx.estimated_prompt_tokens, completion=completion)
            ),
            UsageSource.ESTIMATED,
        )

    def _with_images(self, counts: TokenCounts) -> TokenCounts:
        if not self._images:
            return counts
        return TokenCounts(
            prompt=counts.prompt,
            completion=counts.completion,
            cached_prompt=counts.cached_prompt,
            reasoning=counts.reasoning,
            cache_write=counts.cache_write,
            images=self._images,
        )

    def _was_substituted(self) -> bool:
        """Whether the provider served a model other than the one we asked for.

        Compared against the *upstream* name we sent, which is the only
        meaningful comparison: our client-facing name differs from the
        provider's by design on every single request.
        """
        if self._upstream_model is None or self._model is None:
            return False
        return self._upstream_model != self._model.upstream_model

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

        counts, source = self.resolve_counts(failed=status in (UsageStatus.UPSTREAM_ERROR,))

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

        # What the counterparty said it cost. Deliberately outside the try above:
        # a failure to price locally must not also discard their figure, which is
        # the one number still available when our own price row is the thing that
        # is broken.
        reported = self._plugin.read_reported_cost(self._upstream_usage)

        # Both figures are recorded in either mode, so a divergence is always
        # reconstructable (ADR 0032 decision 6). Only which one becomes `cost`
        # depends on the mode.
        computed = breakdown.total
        charged, charged_currency, cost_source = computed, breakdown.currency, CostSource.OWN_PRICES
        if self._ctx.billing_mode == BillingMode.PROVIDER_REPORTED:
            billable = (
                reported is not None
                and reported.authoritative
                # Billing in a unit the quota engine cannot count would let a
                # cost ceiling silently stop applying: counters sum `cost`
                # across rows, and summing dollars into a euro budget is the one
                # arithmetic this refuses to do. Reporting another currency is
                # fine and happens above — `upstream_cost` keeps it — but
                # *billing* in one waits on currency-aware quotas (ADR 0032).
                and reported.currency.upper() == breakdown.currency.upper()
            )
            if billable and reported is not None:
                charged = reported.amount
                charged_currency = reported.currency
                cost_source = CostSource.PROVIDER_REPORTED
            else:
                if reported is not None and reported.authoritative:
                    logger.warning(
                        "provider reported %s %s but this gateway bills %s; charging our own "
                        "price instead. Recording it as a fallback rather than converting.",
                        reported.amount,
                        reported.currency,
                        breakdown.currency,
                    )
                # Named rather than silent. A pass-through deployment quietly
                # billing from a price table nobody maintains is exactly the
                # failure this distinction exists to surface, and reports
                # disclose the count.
                cost_source = CostSource.OWN_PRICES_FALLBACK

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
            "cache_write_tokens": counts.cache_write,
            "reasoning_tokens": counts.reasoning,
            "image_count": counts.images,
            "image_size": self._image_size,
            "usage_source": source,
            "upstream_model": self._upstream_model,
            "upstream_provider": self._upstream_provider,
            "model_substituted": self._was_substituted(),
            "cost": charged,
            "currency": charged_currency,
            "computed_cost": computed,
            "cost_source": cost_source,
            "upstream_cost": reported.amount if reported else None,
            "upstream_cost_currency": reported.currency if reported else None,
            "upstream_cost_details": dict(reported.details)
            if reported and reported.details
            else None,
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
