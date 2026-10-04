"""Reply text on usage rows is kept for a window, then cleared; the row stays."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from conftest import Seeded
from gateway.config import Settings
from gateway.models import UsageRecord, UsageStatus
from gateway.retention import TranscriptRetentionLoop, sweep_assistant_text
from gateway.sse.events import SSEEvent
from gateway.types import utcnow
from helpers import chunk
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_accounting import accounting_for, fetch, load_model

DAY = timedelta(hours=24)


async def make_row(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    seeded: Seeded,
    *,
    text: str,
    age: timedelta,
    finalise: bool = True,
) -> uuid.UUID:
    """A usage row whose last write was `age` ago."""
    model = await load_model(session, seeded.model.id)
    accounting = accounting_for(
        session_factory=session_factory, settings=settings, seeded=seeded, model=model
    )
    record_id = await accounting.begin()
    accounting.observe_event(SSEEvent.from_json(chunk(text)))
    if finalise:
        await accounting.finalise(status=UsageStatus.COMPLETED)
    else:
        await accounting.flush()
    then = utcnow() - age
    values = {"updated_at": then}
    if finalise:
        values["finalised_at"] = then
    async with session_factory() as other:
        await other.execute(update(UsageRecord).where(UsageRecord.id == record_id).values(**values))
        await other.commit()
    return record_id


class TestSweep:
    async def test_old_text_is_cleared_and_the_ledger_row_is_kept(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession],
        settings: Settings, seeded: Seeded,
    ) -> None:
        old = await make_row(session, session_factory, settings, seeded, text="secret", age=2 * DAY)
        before = await fetch(session_factory, old)

        assert await sweep_assistant_text(session_factory, DAY) == 1

        after = await fetch(session_factory, old)
        assert after.assistant_text is None
        assert after.status is UsageStatus.COMPLETED
        assert (after.total_tokens, after.cost) == (before.total_tokens, before.cost)
        # Clearing the transcript is not the row changing.
        assert after.updated_at == before.updated_at
        assert after.finalised_at == before.finalised_at

    async def test_recent_text_is_kept(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession],
        settings: Settings, seeded: Seeded,
    ) -> None:
        fresh = await make_row(
            session, session_factory, settings, seeded, text="keep me", age=timedelta(hours=1)
        )
        assert await sweep_assistant_text(session_factory, DAY) == 0
        assert (await fetch(session_factory, fresh)).assistant_text == "keep me"

    async def test_a_stream_still_being_written_is_never_cleared(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession],
        settings: Settings, seeded: Seeded,
    ) -> None:
        """The recovery path this column exists for: an unfinalised row whose
        last flush is recent is somebody's answer in progress."""
        live = await make_row(
            session, session_factory, settings, seeded, text="partial", age=timedelta(minutes=1),
            finalise=False,
        )
        assert await sweep_assistant_text(session_factory, DAY) == 0
        assert (await fetch(session_factory, live)).assistant_text == "partial"

    async def test_an_abandoned_unfinalised_row_is_cleared_by_its_last_write(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession],
        settings: Settings, seeded: Seeded,
    ) -> None:
        """A gateway that died mid-stream leaves `finalised_at` empty for ever;
        it must not keep its text for ever too."""
        dead = await make_row(
            session, session_factory, settings, seeded, text="orphan", age=3 * DAY, finalise=False
        )
        assert await sweep_assistant_text(session_factory, DAY) == 1
        assert (await fetch(session_factory, dead)).assistant_text is None

    async def test_it_is_idempotent(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession],
        settings: Settings, seeded: Seeded,
    ) -> None:
        await make_row(session, session_factory, settings, seeded, text="x", age=2 * DAY)
        assert await sweep_assistant_text(session_factory, DAY) == 1
        assert await sweep_assistant_text(session_factory, DAY) == 0


class TestLoopAndSettings:
    def test_the_default_window_is_a_day(self) -> None:
        assert Settings().transcript_retention_hours == 24.0

    async def test_the_loop_sweeps_on_start(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession],
        settings: Settings, seeded: Seeded,
    ) -> None:
        old = await make_row(session, session_factory, settings, seeded, text="x", age=2 * DAY)
        loop = TranscriptRetentionLoop(
            session_factory,
            settings.model_copy(update={"transcript_retention_hours": 24.0}),
        )
        loop.start()
        try:
            for _ in range(50):
                if (await fetch(session_factory, old)).assistant_text is None:
                    break
                await asyncio.sleep(0.1)
        finally:
            await loop.stop()
        assert (await fetch(session_factory, old)).assistant_text is None

    def test_a_negative_window_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            Settings(transcript_retention_hours=-1)
