"""The usage-row lifecycle.

Covers the two behaviours that only matter when something goes wrong: that a
stream which never reports usage is recorded as an *estimate* rather than as zero,
and that partial output is on disk before the request finishes.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import orjson
import pytest
from conftest import Seeded
from gateway.accounting import DEFAULT_ESTIMATOR, RequestAccounting, RequestContext
from gateway.config import Settings
from gateway.models import ModelDef, UsageRecord, UsageSource, UsageStatus
from gateway.sse.events import SSEEvent
from helpers import chunk, usage_only_frame, usage_payload
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload


async def load_model(session: AsyncSession, model_id: uuid.UUID) -> ModelDef:
    stmt = select(ModelDef).where(ModelDef.id == model_id).options(selectinload(ModelDef.prices))
    return (await session.execute(stmt)).scalar_one()


def accounting_for(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    seeded: Seeded,
    model: ModelDef | None,
    streamed: bool = True,
    prompt_tokens: int = 0,
) -> RequestAccounting:
    return RequestAccounting(
        context=RequestContext(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            model_name=seeded.model.name,
            currency=settings.billing_currency,
            streamed=streamed,
            user_id=seeded.user.id,
            group_id=seeded.group.id,
            api_key_id=seeded.api_key.id,
            model_id=seeded.model.id,
            estimated_prompt_tokens=prompt_tokens,
            redaction_engine="noop",
        ),
        session_factory=session_factory,
        settings=settings,
        estimator=DEFAULT_ESTIMATOR,
        model=model,
    )


async def fetch(
    session_factory: async_sessionmaker[AsyncSession], record_id: uuid.UUID
) -> UsageRecord:
    async with session_factory() as session:
        return (
            await session.execute(select(UsageRecord).where(UsageRecord.id == record_id))
        ).scalar_one()


class TestLifecycle:
    async def test_begin_creates_an_in_progress_row_before_the_upstream_call(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        seeded: Seeded,
    ) -> None:
        """The row must exist before any tokens are spent."""
        model = await load_model(session, seeded.model.id)
        accounting = accounting_for(
            session_factory=session_factory, settings=settings, seeded=seeded, model=model
        )
        record_id = await accounting.begin()

        row = await fetch(session_factory, record_id)
        assert row.status is UsageStatus.IN_PROGRESS
        assert row.total_tokens == 0
        assert row.group_id == seeded.group.id
        assert row.api_key_id == seeded.api_key.id
        assert row.model_name == seeded.model.name
        assert row.redaction_engine == "noop"

    async def test_upstream_usage_is_recorded_exactly(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        seeded: Seeded,
    ) -> None:
        model = await load_model(session, seeded.model.id)
        accounting = accounting_for(
            session_factory=session_factory, settings=settings, seeded=seeded, model=model
        )
        record_id = await accounting.begin()

        accounting.observe_event(SSEEvent.from_json(chunk("hello")))
        accounting.observe_event(SSEEvent.from_json(chunk(finish_reason="stop")))
        accounting.observe_event(
            SSEEvent.from_json(usage_only_frame(prompt=1_000_000, completion=500_000))
        )
        actuals = await accounting.finalise(status=UsageStatus.COMPLETED)

        row = await fetch(session_factory, record_id)
        assert row.usage_source is UsageSource.UPSTREAM_EXACT
        assert row.prompt_tokens == 1_000_000
        assert row.completion_tokens == 500_000
        assert row.total_tokens == 1_500_000
        assert row.finish_reason == "stop"
        assert row.assistant_text == "hello"
        # 1M in at 1/M plus 0.5M out at 2/M = 1 + 1
        assert actuals.cost == Decimal(2)
        assert row.price_id is not None

    async def test_missing_usage_frame_is_estimated_not_zero(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        seeded: Seeded,
    ) -> None:
        """The LiteLLM #25389 failure mode: a stream with no trailing usage.

        Recording zero would be indistinguishable from a free request and would
        silently understate the group's spend.
        """
        model = await load_model(session, seeded.model.id)
        accounting = accounting_for(
            session_factory=session_factory,
            settings=settings,
            seeded=seeded,
            model=model,
            prompt_tokens=40,
        )
        record_id = await accounting.begin()

        for piece in ["Some ", "generated ", "text that cost real money."]:
            accounting.observe_event(SSEEvent.from_json(chunk(piece)))
        await accounting.finalise(status=UsageStatus.COMPLETED)

        row = await fetch(session_factory, record_id)
        assert row.usage_source is UsageSource.ESTIMATED
        assert row.prompt_tokens == 40
        assert row.completion_tokens > 0
        assert row.total_tokens > 0
        assert row.cost > Decimal(0)

    async def test_no_output_and_no_usage_is_honestly_unavailable(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        seeded: Seeded,
    ) -> None:
        """Nothing happened, so claiming an estimate would be worse than zero."""
        model = await load_model(session, seeded.model.id)
        accounting = accounting_for(
            session_factory=session_factory, settings=settings, seeded=seeded, model=model
        )
        record_id = await accounting.begin()
        await accounting.finalise(status=UsageStatus.UPSTREAM_ERROR)

        row = await fetch(session_factory, record_id)
        assert row.usage_source is UsageSource.UNAVAILABLE
        assert row.total_tokens == 0
        assert row.cost == Decimal(0)

    async def test_zero_token_usage_frame_falls_back_to_estimation(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        seeded: Seeded,
    ) -> None:
        """Some providers send a usage object full of zeroes. Do not believe it."""
        model = await load_model(session, seeded.model.id)
        accounting = accounting_for(
            session_factory=session_factory,
            settings=settings,
            seeded=seeded,
            model=model,
            prompt_tokens=12,
        )
        record_id = await accounting.begin()
        accounting.observe_event(SSEEvent.from_json(chunk("real output here")))
        accounting.observe_event(SSEEvent.from_json(usage_only_frame(prompt=0, completion=0)))
        await accounting.finalise(status=UsageStatus.COMPLETED)

        row = await fetch(session_factory, record_id)
        assert row.usage_source is UsageSource.ESTIMATED
        assert row.total_tokens > 0

    async def test_finalise_is_idempotent(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        seeded: Seeded,
    ) -> None:
        """It is called from both the success path and the disconnect handler."""
        model = await load_model(session, seeded.model.id)
        accounting = accounting_for(
            session_factory=session_factory, settings=settings, seeded=seeded, model=model
        )
        record_id = await accounting.begin()
        accounting.observe_event(SSEEvent.from_json(usage_only_frame(prompt=100, completion=50)))

        first = await accounting.finalise(status=UsageStatus.COMPLETED)
        second = await accounting.finalise(status=UsageStatus.CLIENT_DISCONNECTED)

        assert first.tokens == second.tokens
        row = await fetch(session_factory, record_id)
        # The first terminal status wins; the second call must not overwrite it.
        assert row.status is UsageStatus.COMPLETED

    async def test_unpriced_model_records_usage_with_zero_cost(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        seeded: Seeded,
    ) -> None:
        """An operator who has not priced a model can still serve it."""
        accounting = accounting_for(
            session_factory=session_factory, settings=settings, seeded=seeded, model=None
        )
        record_id = await accounting.begin()
        accounting.observe_event(SSEEvent.from_json(usage_only_frame(prompt=1000, completion=1000)))
        await accounting.finalise(status=UsageStatus.COMPLETED)

        row = await fetch(session_factory, record_id)
        assert row.total_tokens == 2000
        assert row.cost == Decimal(0)
        assert row.price_id is None


class TestTranscriptPersistence:
    async def test_partial_output_is_flushed_while_streaming(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        seeded: Seeded,
    ) -> None:
        """A suspended mobile client must be able to recover the answer."""
        model = await load_model(session, seeded.model.id)
        accounting = accounting_for(
            session_factory=session_factory, settings=settings, seeded=seeded, model=model
        )
        record_id = await accounting.begin()

        accounting.observe_event(SSEEvent.from_json(chunk("first half ")))
        await accounting.maybe_flush()

        # Visible on disk before the stream has finished.
        mid_flight = await fetch(session_factory, record_id)
        assert mid_flight.assistant_text == "first half "
        assert mid_flight.status is UsageStatus.IN_PROGRESS

        accounting.observe_event(SSEEvent.from_json(chunk("second half")))
        await accounting.finalise(status=UsageStatus.COMPLETED)
        final = await fetch(session_factory, record_id)
        assert final.assistant_text == "first half second half"

    async def test_disconnect_keeps_partial_text_and_accrued_usage(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        seeded: Seeded,
    ) -> None:
        """Tokens generated before the client vanished were still paid for."""
        model = await load_model(session, seeded.model.id)
        accounting = accounting_for(
            session_factory=session_factory,
            settings=settings,
            seeded=seeded,
            model=model,
            prompt_tokens=25,
        )
        record_id = await accounting.begin()
        accounting.observe_event(SSEEvent.from_json(chunk("half an answ")))

        actuals = await accounting.finalise(status=UsageStatus.CLIENT_DISCONNECTED)

        row = await fetch(session_factory, record_id)
        assert row.status is UsageStatus.CLIENT_DISCONNECTED
        assert row.assistant_text == "half an answ"
        assert row.usage_source is UsageSource.ESTIMATED
        assert row.total_tokens > 0
        assert actuals.tokens > 0

    async def test_flushing_can_be_disabled(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        seeded: Seeded,
    ) -> None:
        settings.persist_transcripts = False
        model = await load_model(session, seeded.model.id)
        accounting = accounting_for(
            session_factory=session_factory, settings=settings, seeded=seeded, model=model
        )
        record_id = await accounting.begin()
        accounting.observe_event(SSEEvent.from_json(chunk("not flushed")))
        await accounting.maybe_flush()

        assert (await fetch(session_factory, record_id)).assistant_text is None


class TestAccumulation:
    async def test_tool_call_arguments_are_reassembled(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        seeded: Seeded,
    ) -> None:
        """Agent traffic is mostly tool calls; losing them loses the answer."""
        model = await load_model(session, seeded.model.id)
        accounting = accounting_for(
            session_factory=session_factory, settings=settings, seeded=seeded, model=model
        )
        record_id = await accounting.begin()

        for fragment in ['{"query":', '"eupl ', 'licence"}']:
            accounting.observe_payload(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "function": {
                                            "name": "search",
                                            "arguments": fragment,
                                        },
                                    }
                                ]
                            },
                        }
                    ]
                }
            )
        await accounting.finalise(status=UsageStatus.COMPLETED)

        row = await fetch(session_factory, record_id)
        assert row.assistant_text is not None
        stored = orjson.loads(row.assistant_text)
        assert stored["0"]["tool_calls"][0]["arguments"] == '{"query":"eupl licence"}'
        assert stored["0"]["tool_calls"][0]["name"] == "search"

    async def test_multiple_choices_are_stored_losslessly(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        seeded: Seeded,
    ) -> None:
        model = await load_model(session, seeded.model.id)
        accounting = accounting_for(
            session_factory=session_factory, settings=settings, seeded=seeded, model=model
        )
        record_id = await accounting.begin()
        accounting.observe_event(SSEEvent.from_json(chunk("first", index=0)))
        accounting.observe_event(SSEEvent.from_json(chunk("second", index=1)))
        await accounting.finalise(status=UsageStatus.COMPLETED)

        row = await fetch(session_factory, record_id)
        assert row.assistant_text is not None
        stored = orjson.loads(row.assistant_text)
        assert stored["0"]["content"] == "first"
        assert stored["1"]["content"] == "second"

    async def test_non_streamed_body_is_observed(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        seeded: Seeded,
    ) -> None:
        """A complete response carries `message`, not `delta`."""
        model = await load_model(session, seeded.model.id)
        accounting = accounting_for(
            session_factory=session_factory,
            settings=settings,
            seeded=seeded,
            model=model,
            streamed=False,
        )
        record_id = await accounting.begin()
        accounting.observe_payload(
            {
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "complete answer"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": usage_payload(7, 3),
            }
        )
        await accounting.finalise(status=UsageStatus.COMPLETED)

        row = await fetch(session_factory, record_id)
        assert row.assistant_text == "complete answer"
        assert row.usage_source is UsageSource.UPSTREAM_EXACT
        assert row.total_tokens == 10

    async def test_refusal_text_is_captured_as_output(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        seeded: Seeded,
    ) -> None:
        model = await load_model(session, seeded.model.id)
        accounting = accounting_for(
            session_factory=session_factory, settings=settings, seeded=seeded, model=model
        )
        record_id = await accounting.begin()
        accounting.observe_payload(
            {"choices": [{"index": 0, "delta": {"refusal": "I cannot help with that."}}]}
        )
        await accounting.finalise(status=UsageStatus.COMPLETED)

        assert (await fetch(session_factory, record_id)).assistant_text == (
            "I cannot help with that."
        )

    async def test_done_and_keepalive_frames_are_ignored(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        seeded: Seeded,
    ) -> None:
        model = await load_model(session, seeded.model.id)
        accounting = accounting_for(
            session_factory=session_factory, settings=settings, seeded=seeded, model=model
        )
        await accounting.begin()
        accounting.observe_event(SSEEvent.done())
        accounting.observe_event(SSEEvent(comments=[" ping"]))
        counts, source = accounting.resolve_counts()
        assert counts.total == 0
        assert source is UsageSource.UNAVAILABLE


class TestUnmetered:
    """A deployment that keeps no ledger (``accounting.enabled=false``).

    The shape exists for somebody who wants routing, keys and redaction and has
    no interest in what anything cost. What these pin is that "off" means *no
    row*, never a row full of zeros — once written the two are
    indistinguishable, and this project's posture on money is that a gap stays
    visible as a gap.
    """

    async def test_begin_writes_no_row(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        seeded: Seeded,
    ) -> None:
        unmetered = settings.model_copy(deep=True)
        unmetered.accounting.enabled = False
        unmetered.quota.enabled = False

        accounting = accounting_for(
            session_factory=session_factory,
            settings=unmetered,
            seeded=seeded,
            model=None,
        )
        assert await accounting.begin() is None
        assert accounting.record_id is None

        async with session_factory() as check:
            assert (await check.execute(select(UsageRecord))).scalars().all() == []

    async def test_finalise_costs_nothing_and_still_writes_nothing(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        seeded: Seeded,
    ) -> None:
        # Not merely "does not crash". `finalise` must return before the
        # `record_id is None` branch, which logs an error on the assumption
        # that `begin` was skipped by mistake — unmetered, that would be an
        # error line per request for something entirely expected.
        unmetered = settings.model_copy(deep=True)
        unmetered.accounting.enabled = False
        unmetered.quota.enabled = False

        accounting = accounting_for(
            session_factory=session_factory,
            settings=unmetered,
            seeded=seeded,
            model=None,
        )
        await accounting.begin()
        actuals = await accounting.finalise(status=UsageStatus.COMPLETED)

        assert actuals.tokens == Decimal(0)
        assert actuals.cost == Decimal(0)
        async with session_factory() as check:
            assert (await check.execute(select(UsageRecord))).scalars().all() == []

    def test_quotas_without_a_ledger_are_refused_at_startup(self) -> None:
        """Counters rebuild from ``usage_records``; with none they stay at zero
        and every ceiling silently passes. A quota that refuses nobody is worse
        than no quota, because somebody configured it and believes in it."""
        with pytest.raises(ValidationError, match=r"quota\.enabled requires accounting\.enabled"):
            Settings(quota={"enabled": True}, accounting={"enabled": False})

    def test_both_off_together_is_the_supported_shape(self) -> None:
        settings = Settings(quota={"enabled": False}, accounting={"enabled": False})
        assert settings.accounting.enabled is False
        assert settings.quota.enabled is False
