"""Retention for the reply text kept on usage rows.

`usage_records.assistant_text` exists for one reason: a mobile client suspended
mid-stream can come back and recover the answer it was reading (ADR 0007). That
is a need measured in minutes, but the column has no conversation key and no
expiry, so every reply a deployment ever served stayed in the ledger — text that
may carry personal data, in a table whose rows are otherwise kept for the money.

The sweep clears the text and nothing else. The row, its tokens, its cost and
its timestamps stay: the ledger is the audit trail, the transcript is a
convenience. "Older than the window" is measured from `finalised_at`, or from
`updated_at` for a row that never finalised (a gateway that died mid-stream),
which the streaming flush keeps current — so a stream still being written is
never touched.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gateway.config import Settings
from gateway.loops import stop_between_passes
from gateway.models import UsageRecord
from gateway.types import utcnow

logger = logging.getLogger(__name__)

#: Rows cleared per statement, so a first run over a large ledger is a series of
#: short transactions rather than one that holds every row lock at once.
_BATCH = 1000


async def sweep_assistant_text(
    session_factory: async_sessionmaker[AsyncSession], retention: timedelta
) -> int:
    """Clear `assistant_text` older than `retention`. Returns how many rows.

    Idempotent and safe to run from several workers at once: a row another
    worker already cleared no longer matches. `updated_at` is set to itself so
    that clearing a transcript does not look like the row having changed.
    """
    cutoff = utcnow() - retention
    age = func.coalesce(UsageRecord.finalised_at, UsageRecord.updated_at)
    cleared = 0
    while True:
        async with session_factory() as session:
            ids = (
                (
                    await session.execute(
                        select(UsageRecord.id)
                        .where(UsageRecord.assistant_text.is_not(None), age < cutoff)
                        .limit(_BATCH)
                    )
                )
                .scalars()
                .all()
            )
            if not ids:
                return cleared
            await session.execute(
                update(UsageRecord)
                .where(UsageRecord.id.in_(ids))
                .values(assistant_text=None, updated_at=UsageRecord.updated_at)
            )
            await session.commit()
        cleared += len(ids)


class TranscriptRetentionLoop:
    """Runs the sweep periodically; started in the lifespan, stopped at shutdown,
    the same shape as `erasure.ErasureRetryLoop`."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], settings: Settings
    ) -> None:
        self._session_factory = session_factory
        self._retention = timedelta(hours=settings.transcript_retention_hours)
        self._interval = settings.transcript_sweep_seconds
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        self._stopping.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop between sweeps, never in the middle of one.

        Cancelling the task while a sweep's query was in flight left that
        asyncpg connection checked out of the pool, so the engine's dispose at
        shutdown never closed it: a leaked socket on every shutdown that caught
        a sweep, which the Postgres test leg reported as an unclosed transport.
        The running sweep (a few batched updates) is let finish; only a sweep
        that overruns the grace period is cancelled.
        """
        if self._task is None:
            return
        await stop_between_passes(self._task, self._stopping)
        self._task = None

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                cleared = await sweep_assistant_text(self._session_factory, self._retention)
                if cleared:
                    logger.info("cleared reply text from %d usage rows past retention", cleared)
            except Exception:
                logger.exception("transcript retention sweep failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval)
